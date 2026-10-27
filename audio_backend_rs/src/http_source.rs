//! HTTP Range MediaSource：让 symphonia 直接解码网络音频流。
//!
//! 相比 ffmpeg 子进程方案：
//!   - 无子进程，切歌/seek 不再卡在 join 上；
//!   - seek 按字节 Range 直接跳读，不必从头读并丢弃；
//!   - 支持 http 与 https（ureq + rustls）。
//!
//! symphonia 0.6 要求 MediaSource: Read + Seek + Send + Sync。
//! ureq 的响应 reader 不保证 Sync，故用 Mutex 包住：解码线程是唯一
//! 实际访问者，Mutex 只用于满足类型约束，不构成热路径竞争。
//!
//! ## 磁盘缓存（P3）
//!
//! 读取顺序是「先缓存、后网络」，网络读到的字节同时落盘：
//!
//! ```text
//! read(pos) ──> 缓存区间覆盖 pos？ ──是──> 直接读文件（0 网络）
//!                    │否
//!                    └──> Range 请求 ──> 写入缓存 ──> 返回
//! ```
//!
//! seek 只改 `pos`，**不清缓存**——这正是它相对「连接复用」的价值：
//! 播放器 seek 时会放弃当前响应体，Go/ureq 的连接只能关闭，回到已下载
//! 位置一定得重新建连；而本地文件没有这个问题。

use std::io::{self, Read, Seek, SeekFrom};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::stream_cache::StreamCache;

/// 基于 HTTP Range 的只读媒体源。
pub(crate) struct HttpMediaSource {
    agent: ureq::Agent,
    url: String,
    pos: u64,
    len: u64,
    body: Mutex<Option<Box<dyn Read + Send>>>,
    /// 外部停止标志：为真时 read 立即返回 0，让解码线程尽快退出。
    stop: Arc<AtomicBool>,
    /// 磁盘缓存（None = 未启用 / 不可用）。
    cache: Option<StreamCache>,
}

impl HttpMediaSource {
    /// 打开 URL：探测是否支持 Range，并取得总长度。
    ///
    /// `cache_key` 非空时启用磁盘缓存；若该曲目已完整缓存，则**完全不联网**。
    pub(crate) fn open(
        url: &str,
        stop: Arc<AtomicBool>,
        cache_key: Option<&str>,
    ) -> io::Result<Self> {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(10))
            // 单次 read 超时：服务器「连接活着但不给数据」时（CDN 限速/挂起）
            // 及时超时，交由 decode 重试逻辑重连（可能命中新的 CDN 节点），
            // 避免解码线程无限阻塞在 socket 上导致声音停止。
            .timeout_read(Duration::from_secs(8))
            .build();

        // ---- 完全命中：不联网 ----
        if let Some(key) = cache_key {
            if let Some(cache) = StreamCache::open_complete(key) {
                let len = cache.len();
                crate::logts!("[cache] 全部命中，直读本地：{key}（{:.1} MiB）",
                    len as f64 / 1048576.0);
                return Ok(Self {
                    agent,
                    url: url.to_string(),
                    pos: 0,
                    len,
                    body: Mutex::new(None),
                    stop,
                    cache: Some(cache),
                });
            }
        }

        // ---- 部分命中：沿用 sidecar 里的长度，跳过探测请求 ----
        if let Some(cache) = cache_key.and_then(StreamCache::open_partial) {
            let len = cache.len();
            crate::logts!(
                "[cache] 已有 {:.1}/{:.1} MiB，跳过探测直接续播：{key}",
                cache.covered() as f64 / 1048576.0,
                len as f64 / 1048576.0,
                key = cache_key.unwrap_or_default()
            );
            return Ok(Self {
                agent,
                url: url.to_string(),
                pos: 0,
                len,
                body: Mutex::new(None),
                stop,
                cache: Some(cache),
            });
        }

        // 探测：Range: bytes=0-0 拿 Content-Range 总长。
        let resp = agent
            .get(url)
            .set("Range", "bytes=0-0")
            .call()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("http probe: {e}")))?;
        let status = resp.status();
        let len = resp
            .header("Content-Range")
            .and_then(|cr| cr.rsplit('/').next())
            .and_then(|s| s.trim().parse::<u64>().ok())
            .or_else(|| resp.header("Content-Length").and_then(|s| s.parse::<u64>().ok()))
            .unwrap_or(0);
        // 仅 206（支持 Range）且长度已知时才启用；否则交由上层回退 ffmpeg。
        if status != 206 || len == 0 {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!("range unsupported: status={status} len={len}"),
            ));
        }
        let cache = cache_key.and_then(|k| StreamCache::open(k, len));
        if let Some(c) = cache.as_ref() {
            let cov = c.covered();
            if cov > 0 {
                crate::logts!("[cache] 已有 {:.1}/{:.1} MiB，继续续传",
                    cov as f64 / 1048576.0, len as f64 / 1048576.0);
            }
        }
        Ok(Self {
            agent,
            url: url.to_string(),
            pos: 0,
            len,
            body: Mutex::new(None),
            stop,
            cache,
        })
    }

    /// 内容总长度（字节）。
    pub(crate) fn len(&self) -> u64 {
        self.len
    }

    /// 从当前 pos 发起一个 Range 请求，替换 body。
    fn open_from_pos(&mut self) -> io::Result<()> {
        let range = format!("bytes={}-", self.pos);
        let resp = self
            .agent
            .get(&self.url)
            .set("Range", &range)
            .call()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("http range {range}: {e}")))?;
        let status = resp.status();
        if status != 206 && status != 200 {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!("http range unexpected status {status}"),
            ));
        }
        // 长度校准：跳过探测时用的是 sidecar 里的旧长度，源站换过文件
        // 就对不上。以服务端为准，同时作废这份已经不匹配的缓存。
        if let Some(total) = resp
            .header("Content-Range")
            .and_then(|cr| cr.rsplit('/').next())
            .and_then(|s| s.trim().parse::<u64>().ok())
        {
            if total != self.len {
                crate::logts!("[cache] 源站长度 {total} 与缓存 {} 不符，作废本地缓存", self.len);
                self.len = total;
                self.cache = None;
            }
        }
        let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(Box::new(resp.into_reader()));
        Ok(())
    }
}

impl Read for HttpMediaSource {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.stop.load(Ordering::SeqCst) {
            return Ok(0);
        }
        // 已到文件末尾：直接 EOF，不要发 `bytes=<len>-` 请求。
        //
        // 不判这一下的后果：解码器在末尾还会再读几次，每次都发一个
        // 「起点=总长度」的 Range，服务端返回空 body → 读回 0 → 上层
        // 再试一次，形成一串无意义的请求（实测每次播放尾部多出 4 个）。
        if self.pos >= self.len {
            return Ok(0);
        }
        // ---- 1) 先看缓存 ----
        // 命中即返回，不碰网络。seek 回到已下载位置时走的就是这条路径，
        // 也是「重听已缓存曲目」完全不联网的原因。
        if let Some(c) = self.cache.as_mut() {
            if let Some(n) = c.try_read(self.pos, buf) {
                self.pos += n as u64;
                return Ok(n);
            }
        }
        // ---- 2) 缓存未覆盖：走网络 ----
        {
            let guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
            if guard.is_none() {
                drop(guard);
                self.open_from_pos()?;
            }
        }
        let start = self.pos;
        let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
        let n = match guard.as_mut().unwrap().read(buf) {
            Ok(0) => {
                // 读到 0 表示这条响应体已耗尽或被对端关闭。丢弃死连接：
                // 下次 read 会按当前 pos 重新发起 Range 请求续传，而不是
                // 一直对着已关闭的连接读到 0/错误、卡在重试循环里。
                *guard = None;
                return Ok(0);
            }
            Ok(n) => n,
            Err(e) => {
                // 连接失效（对端关闭、空闲超时、网络中断）：同样丢弃，
                // 使下次 read 从当前 pos 重新请求续传。
                *guard = None;
                return Err(e);
            }
        };
        drop(guard);
        // ---- 3) 落盘（边拉边存）----
        if let Some(c) = self.cache.as_mut() {
            c.store(start, &buf[..n]);
        }
        self.pos += n as u64;
        Ok(n)
    }
}

impl Seek for HttpMediaSource {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        let target = match pos {
            SeekFrom::Start(n) => n as i64,
            SeekFrom::Current(n) => self.pos as i64 + n,
            SeekFrom::End(n) => self.len as i64 + n,
        };
        if target < 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "negative seek"));
        }
        let target = target as u64;
        if target != self.pos {
            // 丢弃旧响应体；下次 read 时按新位置重新请求。
            //
            // 注意：**不清缓存**。缓存保存的是「已下载的字节区间」，
            // 与当前位置无关；seek 回来后命中缓存即可零网络读取。
            let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
            *guard = None;
            drop(guard);
            self.pos = target;
        }
        Ok(self.pos)
    }
}

impl symphonia::core::io::MediaSource for HttpMediaSource {
    fn is_seekable(&self) -> bool {
        true
    }
    fn byte_len(&self) -> Option<u64> {
        Some(self.len)
    }
}
