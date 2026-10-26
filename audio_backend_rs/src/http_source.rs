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

use std::io::{self, Read, Seek, SeekFrom};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// 基于 HTTP Range 的只读媒体源。
pub(crate) struct HttpMediaSource {
    agent: ureq::Agent,
    url: String,
    pos: u64,
    len: u64,
    body: Mutex<Option<Box<dyn Read + Send>>>,
    /// 外部停止标志：为真时 read 立即返回 0，让解码线程尽快退出。
    stop: Arc<AtomicBool>,
}

impl HttpMediaSource {
    /// 打开 URL：探测是否支持 Range，并取得总长度。
    pub(crate) fn open(url: &str, stop: Arc<AtomicBool>) -> io::Result<Self> {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(10))
            // 单次 read 超时：服务器「连接活着但不给数据」时（CDN 限速/挂起）
            // 及时超时，交由 decode 重试逻辑重连（可能命中新的 CDN 节点），
            // 避免解码线程无限阻塞在 socket 上导致声音停止。
            .timeout_read(Duration::from_secs(8))
            .build();
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
        Ok(Self {
            agent,
            url: url.to_string(),
            pos: 0,
            len,
            body: Mutex::new(None),
            stop,
        })
    }

    /// 内容总长度（字节）。
    pub(crate) fn len(&self) -> u64 {
        self.len
    }

    /// 从当前 pos 发起一个 Range 请求，替换 body。
    fn open_from_pos(&self) -> io::Result<()> {
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
        {
            let guard = self.body.lock().unwrap_or_else(|e| e.into_inner());
            if guard.is_none() {
                drop(guard);
                self.open_from_pos()?;
            }
        }
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
