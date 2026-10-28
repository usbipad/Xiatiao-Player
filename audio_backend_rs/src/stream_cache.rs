//! 在线流的磁盘缓存（P3）。
//!
//! 目的：把「已经下载过的字节」留在本地，这样
//!   - 同一首歌再听一遍：完全不联网；
//!   - 往回拖进度条（拖到已下载区间）：零延迟，不再等 CDN 握手。
//!
//! 为什么必须靠磁盘而不是连接复用：播放器 seek 时会**主动放弃**当前
//! 响应体，Go 的 Transport 只在 body 读到 EOF 才回收连接，所以每次
//! seek 都必然重新建连（实测拖 4 次 = 4 条新连接）。只有让字节留在
//! 本地，才能绕开这一次网络往返。
//!
//! ## 结构
//!
//! ```text
//! $XDG_CACHE_HOME/xiatiao/stream/
//!   <key>.data   稀疏文件，按绝对偏移 pwrite（未写入区域不占磁盘）
//!   <key>.json   已覆盖区间 [[start,end), ...] + 总长度 + LRU 时间戳
//! ```
//!
//! 区间用**字节**而非固定块：symphonia 的 read 长度不规则，按块对齐
//! 反而要在边界上做补零/裁剪的家务；字节区间天然精确，顺序下载时
//! 合并成一条 `[0, n)`，序列化开销可忽略。
//!
//! ## 配额
//!
//! 默认 2 GiB（`XIATIAO_STREAM_CACHE_MB` 可覆盖，0 = 禁用缓存）。
//! 超限时按 sidecar 里的 `last_used` 淘汰最久未用的曲目。

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

/// 默认配额：2 GiB。
///
/// 参考：24/96 FLAC 单曲 100–200 MB，2 GiB 约合 10–20 首。
const DEFAULT_QUOTA_BYTES: u64 = 2 * 1024 * 1024 * 1024;

/// 覆盖区间增长超过该值就落盘一次 sidecar。
///
/// 粒度选择的权衡：
///   - 每次 write 都落盘 → 顺序播放时每秒数百次 fsync，浪费；
///   - 只在退出（Drop）落盘 → 进程被 SIGTERM/SIGKILL 时**不会执行
///     Drop**，整首歌的进度全部丢失（实测踩到过：播完 1.8 MiB 只记下
///     112 KiB）。后端确实会被外部 SIGTERM 结束，所以不能只靠 Drop。
/// 256 KiB 在 24/96 下约 0.5 秒、128 kbps 下约 16 秒，进程被强杀最多
/// 丢这么一点，重播时补下即可。
const SAVE_INTERVAL_BYTES: u64 = 256 * 1024;

/// 缓存格式版本。改动**字节与偏移的对应关系**时必须递增，让旧缓存自动作废。
///
/// v1 → v2：v1 存在「缓存命中后网络流光标未跟进」的缺陷，会把网络当前位置
/// 的字节写到错误偏移，听感为「不同片段拼接」。v1 写下的数据不可信，
/// 必须整份丢弃重下。
const CACHE_VERSION: u32 = 2;

/// 缓存目录：`$XDG_CACHE_HOME/xiatiao/stream`（与 Python 侧
/// `config.settings.xdg_cache_dir() + "/stream"` 保持一致）。
pub(crate) fn cache_dir() -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| {
            let mut p = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
            p.push(".cache");
            p
        });
    base.join("xiatiao").join("stream")
}

/// 当前配额（字节）。`XIATIAO_STREAM_CACHE_MB=0` 关闭缓存。
fn quota_bytes() -> u64 {
    match std::env::var("XIATIAO_STREAM_CACHE_MB") {
        Ok(v) => match v.trim().parse::<u64>() {
            Ok(mb) => mb.saturating_mul(1024 * 1024),
            Err(_) => DEFAULT_QUOTA_BYTES,
        },
        Err(_) => DEFAULT_QUOTA_BYTES,
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 缓存键 → 安全文件名。
///
/// 键里可能含 `:` `/` 等（如 `subsonic:<id>:lossless`），直接当文件名
/// 会跨目录或非法，故统一转成十六进制——可逆、无歧义、无长度膨胀。
fn file_stem(key: &str) -> String {
    let mut out = String::with_capacity(key.len() * 2);
    for b in key.as_bytes() {
        out.push_str(&format!("{b:02x}"));
    }
    // 键很长时哈希截断，避免超过 NAME_MAX(255)。
    if out.len() > 200 {
        // 简单 FNV-1a，够用且无依赖。
        let mut h: u64 = 0xcbf29ce484222325;
        for b in key.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        out.truncate(160);
        out.push_str(&format!("-{h:016x}"));
    }
    out
}

/// sidecar 内容。
#[derive(serde::Serialize, serde::Deserialize)]
struct Meta {
    /// 格式版本；旧 sidecar 没有该字段，serde 默认 0 → 自动判为过期。
    #[serde(default)]
    v: u32,
    key: String,
    len: u64,
    /// 已覆盖区间 `[start, end)`，按 start 升序且互不重叠。
    ranges: Vec<(u64, u64)>,
    last_used: u64,
}

impl Meta {
    fn fully_covered(&self) -> bool {
        self.ranges.len() == 1 && self.ranges[0] == (0, self.len) && self.len > 0
    }
}

/// 单曲的磁盘缓存句柄。
pub(crate) struct StreamCache {
    key: String,
    data_path: PathBuf,
    meta_path: PathBuf,
    len: u64,
    file: File,
    ranges: Vec<(u64, u64)>,
    /// 上次落盘时的总覆盖字节数（用于节流）。
    saved_cover: u64,
}

impl StreamCache {
    /// 若该曲目已**完整**缓存，直接返回句柄（调用方据此完全不联网）。
    pub(crate) fn open_complete(key: &str) -> Option<Self> {
        if quota_bytes() == 0 {
            return None;
        }
        let meta = Self::load_meta(key)?;
        if !meta.fully_covered() {
            return None;
        }
        let data_path = cache_dir().join(format!("{}.data", file_stem(key)));
        let file = OpenOptions::new().read(true).open(&data_path).ok()?;
        // 文件长度不足说明 sidecar 与数据不一致（被外部删改），放弃。
        let actual = file.metadata().ok()?.len();
        if actual < meta.len {
            return None;
        }
        let mut me = Self {
            key: key.to_string(),
            data_path,
            meta_path: cache_dir().join(format!("{}.json", file_stem(key))),
            len: meta.len,
            file,
            ranges: meta.ranges,
            saved_cover: meta.len,
        };
        me.save_meta();
        Some(me)
    }

    /// 若该曲目已有**部分**缓存，直接返回句柄（长度取自 sidecar）。
    ///
    /// 目的是省掉播放前的 `bytes=0-0` 探测请求：断点续播时首播那
    /// 1 秒多的探测延迟可以完全去掉。长度若已过期，第一次网络读到的
    /// `Content-Range` 会把它纠正过来（见 `HttpMediaSource::open_from_pos`）。
    pub(crate) fn open_partial(key: &str) -> Option<Self> {
        if quota_bytes() == 0 {
            return None;
        }
        let meta = Self::load_meta(key)?;
        if meta.len == 0 || meta.ranges.is_empty() || meta.fully_covered() {
            return None;
        }
        let data_path = cache_dir().join(format!("{}.data", file_stem(key)));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&data_path)
            .ok()?;
        // 数据文件比 sidecar 记录的短 → 被外部截断，放弃。
        let actual = file.metadata().ok()?.len();
        if actual < meta.len {
            return None;
        }
        let saved_cover = meta.ranges.iter().map(|(a, b)| b - a).sum::<u64>();
        let mut me = Self {
            key: key.to_string(),
            data_path,
            meta_path: cache_dir().join(format!("{}.json", file_stem(key))),
            len: meta.len,
            file,
            ranges: meta.ranges,
            saved_cover,
        };
        me.save_meta();
        Some(me)
    }

    /// 打开（或新建）该曲目的缓存。`len` 为音源总字节数。
    ///
    /// 长度与旧 sidecar 不符（换音质/源站换了文件）时丢弃旧缓存重来。
    pub(crate) fn open(key: &str, len: u64) -> Option<Self> {
        let quota = quota_bytes();
        if quota == 0 || len == 0 {
            return None;
        }
        let dir = cache_dir();
        fs::create_dir_all(&dir).ok()?;
        let stem = file_stem(key);
        let data_path = dir.join(format!("{stem}.data"));
        let meta_path = dir.join(format!("{stem}.json"));

        // 旧缓存版本/长度不一致 → 弃用并删除文件。
        if let Some(old) = Self::load_meta_any(key) {
            if old.v != CACHE_VERSION || old.len != len {
                let _ = fs::remove_file(&data_path);
                let _ = fs::remove_file(&meta_path);
            }
        }

        // 超配额先淘汰（跳过自己）。
        evict_if_needed(quota.saturating_sub(len), key);

        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&data_path)
            .ok()?;
        // 预置为稀疏文件：未写入的尾部不占磁盘。
        if file.metadata().ok().map(|m| m.len()).unwrap_or(0) != len {
            file.set_len(len).ok()?;
        }

        let (ranges, saved_cover) = match Self::load_meta(key) {
            Some(m) if m.len == len => {
                let cov = m.ranges.iter().map(|(a, b)| b - a).sum::<u64>();
                (m.ranges, cov)
            }
            _ => (Vec::new(), 0),
        };

        let mut me = Self {
            key: key.to_string(),
            data_path,
            meta_path,
            len,
            file,
            ranges,
            saved_cover,
        };
        me.save_meta(); // 刷新 last_used，避免刚打开就被淘汰
        Some(me)
    }

    /// 读取 sidecar，**不做版本校验**（供清理旧版本用）。
    fn load_meta_any(key: &str) -> Option<Meta> {
        let p = cache_dir().join(format!("{}.json", file_stem(key)));
        let raw = fs::read_to_string(p).ok()?;
        serde_json::from_str::<Meta>(&raw).ok()
    }

    /// 读取 sidecar；版本不符视为不存在（旧格式数据不再可信）。
    fn load_meta(key: &str) -> Option<Meta> {
        Self::load_meta_any(key).filter(|m| m.v == CACHE_VERSION)
    }

    fn save_meta(&mut self) {
        let meta = Meta {
            v: CACHE_VERSION,
            key: self.key.clone(),
            len: self.len,
            ranges: self.ranges.clone(),
            last_used: now_secs(),
        };
        let Ok(raw) = serde_json::to_string(&meta) else {
            return;
        };
        // 原子替换：先写临时文件再 rename，避免读到半截 JSON。
        let tmp = self.meta_path.with_extension("json.tmp");
        if fs::write(&tmp, raw).is_ok() {
            let _ = fs::rename(&tmp, &self.meta_path);
        }
        self.saved_cover = self.ranges.iter().map(|(a, b)| b - a).sum();
    }

    pub(crate) fn len(&self) -> u64 {
        self.len
    }

    /// `pos` 处是否已被缓存覆盖；是则读取并返回字节数。
    ///
    /// 只有「整段都在同一个已覆盖区间内」才返回；跨区间缺口交给网络
    /// 补（顺序播放时缺口总在末尾，不存在中间断裂）。
    pub(crate) fn try_read(&mut self, pos: u64, buf: &mut [u8]) -> Option<usize> {
        if buf.is_empty() {
            return None;
        }
        let idx = self
            .ranges
            .iter()
            .position(|(s, e)| *s <= pos && pos < *e)?;
        let end = self.ranges[idx].1;
        let avail = (end - pos).min(buf.len() as u64) as usize;
        self.file.seek(SeekFrom::Start(pos)).ok()?;
        self.file.read_exact(&mut buf[..avail]).ok()?;
        Some(avail)
    }

    /// 把刚从网络读到的字节落盘。
    pub(crate) fn store(&mut self, pos: u64, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        // 截断到文件边界，防止越界写。
        let max = self.len.saturating_sub(pos) as usize;
        let data = if data.len() > max { &data[..max] } else { data };
        if data.is_empty() {
            return;
        }
        if self.file.seek(SeekFrom::Start(pos)).is_err() {
            return;
        }
        if self.file.write_all(data).is_err() {
            return;
        }
        self.add_range(pos, pos + data.len() as u64);
        let covered: u64 = self.ranges.iter().map(|(a, b)| b - a).sum();
        if covered != self.saved_cover {
            let full = self.ranges.len() == 1 && self.ranges[0] == (0, self.len);
            if full || covered.saturating_sub(self.saved_cover) >= SAVE_INTERVAL_BYTES {
                self.save_meta();
            }
        }
    }

    fn add_range(&mut self, start: u64, end: u64) {
        if start >= end {
            return;
        }
        let mut all = Vec::with_capacity(self.ranges.len() + 1);
        all.extend_from_slice(&self.ranges);
        all.push((start, end));
        all.sort_unstable();
        let mut merged: Vec<(u64, u64)> = Vec::with_capacity(all.len());
        for (s, e) in all {
            if let Some(last) = merged.last_mut() {
                if s <= last.1 {
                    if e > last.1 {
                        last.1 = e;
                    }
                    continue;
                }
            }
            merged.push((s, e));
        }
        let full_before = self.ranges.len() == 1 && self.ranges[0] == (0, self.len);
        self.ranges = merged;
        if !full_before && self.ranges.len() == 1 && self.ranges[0] == (0, self.len) {
            crate::logts!("[cache] 已完整缓存：{}（{:.1} MiB）",
                self.key, self.len as f64 / 1048576.0);
            self.save_meta();
        }
    }

    /// 已覆盖的总字节数（诊断用）。
    pub(crate) fn covered(&self) -> u64 {
        self.ranges.iter().map(|(a, b)| b - a).sum()
    }

    /// 返回从 `from` 起的首个未覆盖缺口 `[start, end)`（end 可能等于 len）。
    /// 已完全覆盖（from 到 len 之间无缺口）时返回 None。
    ///
    /// 依赖 `ranges` 按 start 升序且互不重叠（由 add_range 维护）。
    /// 供主动预取线程按序补齐整首曲目使用。
    pub(crate) fn next_gap(&self, from: u64) -> Option<(u64, u64)> {
        gap_after(&self.ranges, self.len, from)
    }
}

/// 在已覆盖区间 `ranges`（按 start 升序、互不重叠）中，找 `from` 之后的
/// 首个缺口 `[start, end)`；`end` 可能等于 `len`。全覆盖时返回 None。
///
/// 抽成自由函数以便单元测试（不依赖真实磁盘文件）。
fn gap_after(ranges: &[(u64, u64)], len: u64, from: u64) -> Option<(u64, u64)> {
    let mut cursor = from;
    for &(s, e) in ranges {
        if e <= cursor {
            continue;
        }
        if s > cursor {
            return Some((cursor, s));
        }
        cursor = cursor.max(e);
    }
    if cursor < len {
        Some((cursor, len))
    } else {
        None
    }
}

impl Drop for StreamCache {
    fn drop(&mut self) {
        if self.covered() > 0 {
            self.save_meta();
        }
        let _ = self.data_path.exists();
    }
}

/// 超配额时淘汰最久未使用的曲目（按 sidecar 的 last_used）。
fn evict_if_needed(quota: u64, keep_key: &str) {
    let dir = cache_dir();
    let Ok(rd) = fs::read_dir(&dir) else {
        return;
    };
    let keep_stem = file_stem(keep_key);
    let mut entries: Vec<(u64, u64, PathBuf, PathBuf)> = Vec::new(); // (last_used, size, json, data)
    let mut total: u64 = 0;
    for e in rd.flatten() {
        let p = e.path();
        if p.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if stem == keep_stem {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&p) else { continue };
        let Ok(meta) = serde_json::from_str::<Meta>(&raw) else {
            // sidecar 坏了：连数据一起清掉。
            let _ = fs::remove_file(&p);
            let _ = fs::remove_file(p.with_extension("data"));
            continue;
        };
        let data = p.with_extension("data");
        let size = fs::metadata(&data).map(|m| m.len()).unwrap_or(0);
        // 稀疏文件按「逻辑长度」计费偏高，但保守估计不会撑爆磁盘配额。
        total = total.saturating_add(size);
        entries.push((meta.last_used, size, p.clone(), data));
    }
    if total <= quota {
        return;
    }
    entries.sort_unstable_by_key(|(ts, _, _, _)| *ts);
    for (_, size, meta_path, data_path) in entries {
        if total <= quota {
            break;
        }
        let _ = fs::remove_file(&meta_path);
        let _ = fs::remove_file(&data_path);
        total = total.saturating_sub(size);
        crate::logts!("[cache] 淘汰一条缓存（释放 {:.1} MiB）", size as f64 / 1048576.0);
    }
}

/// 清空整个缓存目录（供 Python 侧「清空缓存」之外的后端自清理入口）。
#[allow(dead_code)]
pub(crate) fn clear_all() -> u64 {
    let dir = cache_dir();
    let mut freed = 0u64;
    if let Ok(rd) = fs::read_dir(&dir) {
        for e in rd.flatten() {
            let p = e.path();
            let size = fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
            if fs::remove_file(&p).is_ok() {
                freed = freed.saturating_add(size);
            }
        }
    }
    freed
}

#[cfg(test)]
mod tests {
    use super::gap_after;

    /// 全空：从头开始补整首。
    #[test]
    fn gap_empty_cache_is_whole_file() {
        assert_eq!(gap_after(&[], 1000, 0), Some((0, 1000)));
    }

    /// 已完整覆盖：无缺口。
    #[test]
    fn gap_full_coverage_is_none() {
        assert_eq!(gap_after(&[(0, 1000)], 1000, 0), None);
    }

    /// 已覆盖前缀：缺口从已覆盖末尾开始。
    #[test]
    fn gap_after_covered_prefix() {
        assert_eq!(gap_after(&[(0, 400)], 1000, 0), Some((400, 1000)));
    }

    /// 起点落在已覆盖区间内部：需前跳到区间末尾再找缺口。
    #[test]
    fn gap_from_inside_covered_range() {
        assert_eq!(gap_after(&[(0, 400), (600, 800)], 1000, 200), Some((400, 600)));
    }

    /// 起点落在缺口内部：缺口保留剩余部分（不回头补已跳过的字节）。
    #[test]
    fn gap_from_inside_hole() {
        assert_eq!(gap_after(&[(0, 100), (500, 1000)], 1000, 300), Some((300, 500)));
    }

    /// 起点已越过全部覆盖区间：补到文件末尾。
    #[test]
    fn gap_after_all_ranges() {
        assert_eq!(gap_after(&[(0, 100)], 1000, 300), Some((300, 1000)));
    }

    /// 起点已在 EOF：无缺口。
    #[test]
    fn gap_at_eof_is_none() {
        assert_eq!(gap_after(&[(0, 1000)], 1000, 1000), None);
    }

    /// 多个缺口：按序返回第一个。
    #[test]
    fn gap_returns_first_hole_only() {
        assert_eq!(
            gap_after(&[(0, 100), (200, 300), (400, 500)], 1000, 0),
            Some((100, 200))
        );
    }
}
