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

/// 一条已打开的网络响应体 + 它下一个字节在音频中的逻辑位置。
///
/// `pos` 是正确性的关键：缓存命中时 `HttpMediaSource::pos` 会跳进，
/// 而响应体自己的光标不会跟着走。若不记录并据此对齐，后续从网络读到的
/// 字节会被当成「当前位置」的数据写回缓存、送给解码器，造成
/// 「声音像不同片段拼起来」。
struct Body {
    reader: Box<dyn Read + Send>,
    pos: u64,
}

/// 基于 HTTP Range 的只读媒体源。
pub(crate) struct HttpMediaSource {
    agent: ureq::Agent,
    url: String,
    pos: u64,
    len: u64,
    body: Mutex<Option<Body>>,
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
        // 探测专用 agent：超时要**长于**服务端自己的上游超时。
        //
        // 代理型后端（如 Subsonic 适配器）拿不到缓存直链时会同步去上游
        // 换链，其自身超时常见为 15s；若这里只等 8s，慢一次就必然探测
        // 失败、被迫回退 ffmpeg（而 ffmpeg 又走同一条慢路径），用户听到的
        // 就是「卡住没声音」。实测 192.168.1.139:4535 冷启动 5~43s。
        let probe_agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(10))
            .timeout_read(Duration::from_secs(20))
            .timeout_write(Duration::from_secs(20))
            .build();

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
        let resp = probe_agent
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
        // 响应体的逻辑起点：正常 206 从 pos 开始；若服务端忽略 Range 返回
        // 200（整文件），则从 0 开始，多出来的前缀由 align_body 前跳丢弃。
        let start = if status == 200 { 0 } else { self.pos };
        let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(Body {
            reader: Box::new(resp.into_reader()),
            pos: start,
        });
        Ok(())
    }

    /// 保证已打开的网络流光标与 `self.pos` 一致。
    ///
    /// 缓存命中会让 `self.pos` 跳进，而响应体光标不动；不对齐就会把
    /// 「网络流当前位置」的字节当成 `self.pos` 处的数据。能前跳就前跳
    /// （不重连），需要倒退（不可能）就丢弃，下次 read 重新发 Range。
    fn align_body(&mut self) -> io::Result<()> {
        let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
        let Some(mut body) = guard.take() else {
            return Ok(());
        };
        if body.pos == self.pos {
            *guard = Some(body);
            return Ok(());
        }
        if body.pos > self.pos {
            // 网络流不能倒流：丢弃（已 take 走，调用方会重新 open_from_pos）。
            return Ok(());
        }
        let mut skip = self.pos - body.pos;
        let mut scratch = vec![0u8; 65536];
        while skip > 0 {
            let want = skip.min(scratch.len() as u64) as usize;
            match body.reader.read(&mut scratch[..want]) {
                // 读空/出错：丢弃这条流，交由调用方重开。
                Ok(0) => return Ok(()),
                Ok(n) => {
                    skip -= n as u64;
                    body.pos += n as u64;
                }
                Err(_) => return Ok(()),
            }
        }
        *guard = Some(body);
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
        // 先对齐光标：缓存命中过若干字节后，网络流仍停在旧位置，
        // 不对齐就会把旧位置的字节当成当前位置的数据（听感为「片段拼接」）。
        self.align_body()?;
        {
            let guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
            if guard.is_none() {
                drop(guard);
                self.open_from_pos()?;
            }
        }
        let start = self.pos;
        let mut guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
        let body = guard.as_mut().unwrap();
        let n = match body.reader.read(buf) {
            Ok(0) => {
                // 未到文件末尾却读到 0：这条响应体异常结束。丢弃死连接并
                // 报错（而不是返回 Ok(0)）——返回 0 会被解码器当成 EOF，
                // 导致本曲提前「播完」；报错则走上层重试，按当前 pos 续传。
                *guard = None;
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!("http body ended early at {start}/{}", self.len),
                ));
            }
            Ok(n) => n,
            Err(e) => {
                // 连接失效（对端关闭、空闲超时、网络中断）：同样丢弃，
                // 使下次 read 从当前 pos 重新请求续传。
                *guard = None;
                return Err(e);
            }
        };
        body.pos += n as u64;
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
