//! DSD 原生处理模块。
//!
//! 用途：为 dsd_mode = native / dop 提供 **原始 1-bit DSD 位流**，
//! 绕过 ffmpeg（ffmpeg 会把 DSD 解码成 PCM，丢失原始位流）。
//!
//! 支持格式（按官方规范解析，不依赖第三方库）：
//! - **DSF**（Sony DSD Stream File，.dsf）：
//!     header: "DSD " + 64-bit chunk size，随后若干 chunk。
//!     "fmt " chunk 描述 format id / 声道 / 采样率 / 位序；
//!     "data" chunk 为交错原始 DSD 字节。
//! - **DFF**（Philips DSDIFF，.dff）：
//!     header: "FRM8" + 64-bit size + "DSD "。
//!     "FVER" / "PROP"（内嵌 "SND " 及采样率/声道）/ "DSD "（原始数据）。
//!
//! 输出策略：
//! - Native：DSD 字节按设备原生 DSD 格式（如 DSD_U32_LE）交 ALSA 输出；
//! - DoP：按 DoP v1.0 规范，将每 16 个 DSD bit 打包进一个 24-bit PCM 样本，
//!   配合 0x05 / 0xFA marker，采样率 = DSD 率 / 16。

use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;

/// DSD 原始数据及其元信息。
#[derive(Debug, Clone)]
pub struct DsdData {
    /// 原始 DSD 字节（每字节 8 个 DSD bit，按文件位序）。
    pub bytes: Vec<u8>,
    /// DSD 采样率（bit 率），如 DSD64=2_822_400、DSD128=5_644_800。
    pub dsd_rate: u32,
    /// 声道数。
    pub channels: u32,
    /// 每声道样本数（DSD bit 数）。
    pub frames: u64,
}

impl DsdData {
    /// 用于 DoP 的 PCM 采样率 = DSD 率 / 16（16 个 DSD bit 打包进一个 PCM 样本）。
    pub fn dop_pcm_rate(&self) -> u32 {
        (self.dsd_rate / 16).max(44_100)
    }
}

/// 错误类型（简单字符串，保持与项目其余部分一致风格）。
pub type DsdResult<T> = Result<T, String>;

/// 按扩展名判断是否为 DSD 文件。
pub fn is_dsd_file(path: &str) -> bool {
    matches!(
        Path::new(path)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .as_deref(),
        Some("dsf") | Some("dff")
    )
}

/// 读取 DSD 文件原始位流。按扩展名分派到 DSF / DFF 解析器。
///
/// 注意：本函数会把整个 data 区读入内存，仅适合小文件或元信息探测。
/// 播放大文件请使用 [`DsdReader`] 流式读取。
pub fn read_dsd(path: &str) -> DsdResult<DsdData> {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match ext.as_str() {
        "dsf" => read_dsf(path),
        "dff" => read_dff(path),
        other => Err(format!("不支持的 DSD 扩展名: {other}")),
    }
}

// ============================================================
// 流式 DSD 读取器（大文件播放）
// ============================================================

/// DSD 流式读取器：只解析头部拿到元信息，data 区按块读取。
///
/// 用于播放：无论文件多大（如 1.9GB 的 DSD256），内存占用恒定为一个块。
pub struct DsdReader {
    reader: BufReader<File>,
    /// DSD bit 率（如 DSD64=2822400、DSD256=11289600）。
    pub dsd_rate: u32,
    pub channels: u32,
    /// 每声道样本数（DSD bit 数）。
    pub frames: u64,
    /// data 区剩余可读字节数。
    remaining: u64,
    /// data 区起始文件偏移（seek 用）。
    data_start: u64,
    /// data 区总字节数。
    data_total: u64,
}

impl DsdReader {
    /// 打开 DSD 文件，解析头部并定位到 data 区起点。
    pub fn open(path: &str) -> DsdResult<Self> {
        let ext = Path::new(path)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        match ext.as_str() {
            "dsf" => Self::open_dsf(path),
            "dff" => Self::open_dff(path),
            other => Err(format!("不支持的 DSD 扩展名: {other}")),
        }
    }

    /// 读取下一块原始 DSD 字节，返回读到的字节数（0 = 结束）。
    pub fn read_chunk(&mut self, buf: &mut [u8]) -> DsdResult<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let want = buf.len().min(self.remaining as usize);
        let n = self
            .reader
            .read(&mut buf[..want])
            .map_err(|e| format!("read dsd chunk: {e}"))?;
        self.remaining -= n as u64;
        Ok(n)
    }

    /// 按目标帧（每声道 DSD bit 数）定位到 data 区。
    ///
    /// DSD 字节率固定：每声道每帧 1 bit，所以目标字节偏移 =
    /// frame / 8 * channels。返回实际定位到的帧（对齐到字节）。
    pub fn seek_to_frame(&mut self, frame: u64) -> DsdResult<u64> {
        let ch = self.channels.max(1) as u64;
        // 每帧（每声道 1 bit）对应的总字节数 = frame * ch / 8。
        // 对齐到整字节（每声道 8 帧 = 1 字节）。
        let byte_off = (frame / 8) * ch;
        let byte_off = byte_off.min(self.data_total);
        use std::io::Seek;
        self.reader
            .seek(SeekFrom::Start(self.data_start + byte_off))
            .map_err(|e| format!("dsd seek: {e}"))?;
        self.remaining = self.data_total - byte_off;
        // 返回对齐后的帧。
        Ok((byte_off / ch) * 8)
    }

    fn open_dsf(path: &str) -> DsdResult<Self> {
        let f = File::open(path).map_err(|e| format!("open dsf: {e}"))?;
        let mut r = BufReader::new(f);

        let mut hdr = [0u8; 28];
        r.read_exact(&mut hdr).map_err(|e| format!("read dsf header: {e}"))?;
        if &hdr[0..4] != b"DSD " {
            return Err("非法 DSF：缺少 'DSD ' 标识".into());
        }

        let mut fmt_hdr = [0u8; 12];
        r.read_exact(&mut fmt_hdr).map_err(|e| format!("read fmt hdr: {e}"))?;
        if &fmt_hdr[0..4] != b"fmt " {
            return Err("非法 DSF：缺少 'fmt ' chunk".into());
        }
        let fmt_size = read_u64_le(&fmt_hdr[4..12]) as usize;
        if fmt_size < 52 {
            return Err(format!("非法 DSF：fmt chunk 过小 ({fmt_size})"));
        }
        let mut fmt = vec![0u8; fmt_size - 12];
        r.read_exact(&mut fmt).map_err(|e| format!("read fmt body: {e}"))?;
        let fmt_id = read_u32_le(&fmt[4..8]);
        if fmt_id != 0 {
            return Err(format!("不支持的 DSF format id: {fmt_id}"));
        }
        let channels = read_u32_le(&fmt[12..16]).max(1);
        let dsd_rate = read_u32_le(&fmt[16..20]);
        let sample_count = read_u64_le(&fmt[24..32]);
        if dsd_rate == 0 {
            return Err("非法 DSF：采样率为 0".into());
        }

        let mut data_hdr = [0u8; 12];
        r.read_exact(&mut data_hdr).map_err(|e| format!("read data hdr: {e}"))?;
        if &data_hdr[0..4] != b"data" {
            return Err("非法 DSF：缺少 'data' chunk".into());
        }
        let data_size = read_u64_le(&data_hdr[4..12]);
        // data 区起点 = 当前流位置。
        let data_start = r.stream_position().map_err(|e| format!("dsf pos: {e}"))?;

        Ok(DsdReader {
            reader: r,
            dsd_rate,
            channels,
            frames: sample_count,
            remaining: data_size,
            data_start,
            data_total: data_size,
        })
    }

    fn open_dff(path: &str) -> DsdResult<Self> {
        let f = File::open(path).map_err(|e| format!("open dff: {e}"))?;
        let mut r = BufReader::new(f);

        let mut hdr = [0u8; 12];
        r.read_exact(&mut hdr).map_err(|e| format!("read dff header: {e}"))?;
        if &hdr[0..4] != b"FRM8" {
            return Err("非法 DFF：缺少 'FRM8' 标识".into());
        }
        let mut form_type = [0u8; 4];
        r.read_exact(&mut form_type).map_err(|e| format!("read dff form: {e}"))?;
        if &form_type != b"DSD " {
            return Err("不支持的 DFF：form type 非 'DSD '".into());
        }

        let mut dsd_rate: u32 = 0;
        let mut channels: u32 = 2;
        let mut data_size: u64 = 0;

        // 顺序扫描顶层 chunk，PROP 取元信息，DSD 定位数据起点。
        loop {
            let mut ch_hdr = [0u8; 12];
            match r.read_exact(&mut ch_hdr) {
                Ok(()) => {}
                Err(_) => break,
            }
            let id = &ch_hdr[0..4];
            let size = read_u64_be(&ch_hdr[4..12]) as usize;

            if id == b"DSD " {
                data_size = size as u64;
                break;
            } else if id == b"PROP" {
                let (rate, ch) = parse_dff_prop(&mut r, size)?;
                if rate > 0 {
                    dsd_rate = rate;
                }
                if ch > 0 {
                    channels = ch;
                }
            } else {
                r.seek(SeekFrom::Current(size as i64))
                    .map_err(|e| format!("skip dff chunk: {e}"))?;
            }
            if size % 2 == 1 {
                let _ = r.seek(SeekFrom::Current(1));
            }
        }

        if data_size == 0 {
            return Err("非法 DFF：未找到 'DSD ' 数据块".into());
        }
        if dsd_rate == 0 {
            dsd_rate = 2_822_400;
        }
        let frames = (data_size * 8) / channels.max(1) as u64;
        // DSD chunk 头 12 字节之后即 data 起点。
        let data_start = r.stream_position().map_err(|e| format!("dff pos: {e}"))?;

        Ok(DsdReader {
            reader: r,
            dsd_rate,
            channels,
            frames,
            remaining: data_size,
            data_start,
            data_total: data_size,
        })
    }
}

// ============================================================
// DSF 解析
// ============================================================

fn read_u32_le(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}
fn read_u32_be(b: &[u8]) -> u32 {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]])
}
fn read_u64_le(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}
fn read_u64_be(b: &[u8]) -> u64 {
    u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

/// 解析 DSF（小端）。
fn read_dsf(path: &str) -> DsdResult<DsdData> {
    let f = File::open(path).map_err(|e| format!("open dsf: {e}"))?;
    let mut r = BufReader::new(f);

    // ---- DSD chunk header ----
    let mut hdr = [0u8; 28];
    r.read_exact(&mut hdr).map_err(|e| format!("read dsf header: {e}"))?;
    if &hdr[0..4] != b"DSD " {
        return Err("非法 DSF：缺少 'DSD ' 标识".into());
    }
    // hdr[4..12] = chunk size（整个文件），hdr[12..20] = file size，hdr[20..28] = metadata offset

    // ---- fmt chunk ----
    let mut fmt_hdr = [0u8; 12];
    r.read_exact(&mut fmt_hdr).map_err(|e| format!("read fmt hdr: {e}"))?;
    if &fmt_hdr[0..4] != b"fmt " {
        return Err("非法 DSF：缺少 'fmt ' chunk".into());
    }
    let fmt_size = read_u64_le(&fmt_hdr[4..12]) as usize;
    if fmt_size < 52 {
        return Err(format!("非法 DSF：fmt chunk 过小 ({fmt_size})"));
    }
    let mut fmt = vec![0u8; fmt_size - 12];
    r.read_exact(&mut fmt).map_err(|e| format!("read fmt body: {e}"))?;
    // fmt 布局（相对 fmt body 起点）：
    //   [0..4]   format version (=1)
    //   [4..8]   format id (=0 表示 DSD raw)
    //   [8..12]  channel type
    //   [12..16] channel num
    //   [16..20] sampling frequency (bit rate)
    //   [20..24] bits per sample (=1 或 8)
    //   [24..32] sample count (每声道)
    //   [32..36] block size per channel
    //   [36..40] reserved
    let fmt_id = read_u32_le(&fmt[4..8]);
    if fmt_id != 0 {
        return Err(format!("不支持的 DSF format id: {fmt_id}"));
    }
    let channels = read_u32_le(&fmt[12..16]).max(1);
    let dsd_rate = read_u32_le(&fmt[16..20]);
    let bits_per_sample = read_u32_le(&fmt[20..24]);
    let sample_count = read_u64_le(&fmt[24..32]);
    if dsd_rate == 0 {
        return Err("非法 DSF：采样率为 0".into());
    }
    // bits_per_sample：1 = 每字节仅 1 bit 有效（MSB 起）；8 = 整字节有效。
    let _ = bits_per_sample;

    // ---- data chunk ----
    let mut data_hdr = [0u8; 12];
    r.read_exact(&mut data_hdr).map_err(|e| format!("read data hdr: {e}"))?;
    if &data_hdr[0..4] != b"data" {
        return Err("非法 DSF：缺少 'data' chunk".into());
    }
    let data_size = read_u64_le(&data_hdr[4..12]) as usize;
    let mut bytes = vec![0u8; data_size];
    r.read_exact(&mut bytes).map_err(|e| format!("read dsf data: {e}"))?;

    Ok(DsdData {
        bytes,
        dsd_rate,
        channels,
        frames: sample_count,
    })
}

// ============================================================
// DFF 解析
// ============================================================

/// 解析 DFF（大端 DSDIFF）。
fn read_dff(path: &str) -> DsdResult<DsdData> {
    let f = File::open(path).map_err(|e| format!("open dff: {e}"))?;
    let mut r = BufReader::new(f);

    // ---- FRM8 header ----
    let mut hdr = [0u8; 12];
    r.read_exact(&mut hdr).map_err(|e| format!("read dff header: {e}"))?;
    if &hdr[0..4] != b"FRM8" {
        return Err("非法 DFF：缺少 'FRM8' 标识".into());
    }
    // hdr[4..12] = size（大端）；其后 4 字节为 form type（"DSD "）
    let mut form_type = [0u8; 4];
    r.read_exact(&mut form_type).map_err(|e| format!("read dff form: {e}"))?;
    if &form_type != b"DSD " {
        return Err("不支持的 DFF：form type 非 'DSD '".into());
    }

    let mut dsd_rate: u32 = 0;
    let mut channels: u32 = 2;
    let mut sample_count: u64 = 0;
    let mut bytes: Vec<u8> = Vec::new();

    // 顺序扫描顶层 chunk，直到找到 "DSD " 数据块。
    loop {
        let mut ch_hdr = [0u8; 12];
        match r.read_exact(&mut ch_hdr) {
            Ok(()) => {}
            Err(_) => break, // 文件结束
        }
        let id = &ch_hdr[0..4];
        let size = read_u64_be(&ch_hdr[4..12]) as usize;

        if id == b"DSD " {
            // 数据块：读取全部原始 DSD 字节
            bytes = vec![0u8; size];
            r.read_exact(&mut bytes).map_err(|e| format!("read dff dsd data: {e}"))?;
            break;
        } else if id == b"PROP" {
            // 属性块：内含 "SND " 子块，其中有采样率/声道信息
            let (rate, ch) = parse_dff_prop(&mut r, size)?;
            if rate > 0 {
                dsd_rate = rate;
            }
            if ch > 0 {
                channels = ch;
            }
        } else {
            // 其它块（如 FVER）：跳过
            r.seek(SeekFrom::Current(size as i64))
                .map_err(|e| format!("skip dff chunk: {e}"))?;
        }
        // chunk 若为奇数长度，按规范补 1 字节 padding
        if size % 2 == 1 {
            let _ = r.seek(SeekFrom::Current(1));
        }
    }

    if bytes.is_empty() {
        return Err("非法 DFF：未找到 'DSD ' 数据块".into());
    }
    if dsd_rate == 0 {
        // 兜底：DSDIFF 默认 DSD64
        dsd_rate = 2_822_400;
    }
    if sample_count == 0 {
        // 每声道样本数 = 总字节数 * 8 / 声道数
        sample_count = (bytes.len() as u64 * 8) / channels.max(1) as u64;
    }

    Ok(DsdData {
        bytes,
        dsd_rate,
        channels,
        frames: sample_count,
    })
}

/// 解析 DFF 的 PROP 块，取出采样率与声道数。
fn parse_dff_prop<R: Read>(r: &mut R, size: usize) -> DsdResult<(u32, u32)> {
    let mut prop = vec![0u8; size];
    r.read_exact(&mut prop).map_err(|e| format!("read dff prop: {e}"))?;
    // prop = "SND " + size(8, BE) + 若干子块
    if prop.len() < 12 || &prop[0..4] != b"SND " {
        return Ok((0, 0));
    }
    let mut rate = 0u32;
    let mut channels = 0u32;
    let mut off = 12usize;
    while off + 12 <= prop.len() {
        let id = &prop[off..off + 4];
        let sz = read_u64_be(&prop[off + 4..off + 12]) as usize;
        let body_start = off + 12;
        if body_start + sz > prop.len() {
            break;
        }
        let body = &prop[body_start..body_start + sz];
        if id == b"FS  " && sz >= 4 {
            rate = read_u32_be(&body[0..4]);
        } else if id == b"CHNL" && sz >= 2 {
            channels = u16::from_be_bytes([body[0], body[1]]) as u32;
        }
        off = body_start + sz;
        if sz % 2 == 1 {
            off += 1;
        }
    }
    Ok((rate, channels))
}

// ============================================================
// DoP 封装（DSD over PCM，标准 v1.0）
// ============================================================

/// 将原始 DSD 字节按 DoP 规范打包为 24-bit PCM 样本（以小端 i32 输出）。
///
/// DoP 规则（每个声道独立）：
///   - 每 16 个 DSD bit 组成一组，装入一个 24-bit 样本的高 16 位；
///   - 低 8 位为 marker：交替 0x05 / 0xFA 标识 DSD 数据流；
///   - 输出的 PCM 采样率 = DSD 率 / 16。
///
/// 输入 `src` 为交错 DSD 字节（每字节 8 bit）。
/// 返回交错 i32（低 24 位有效）样本。
pub fn pack_dop(src: &[u8], channels: usize) -> Vec<i32> {
    let ch = channels.max(1);
    let total_bits = src.len() * 8;
    let bits_per_ch = total_bits / ch;
    // 每声道可用的 16-bit 组数
    let groups_per_ch = bits_per_ch / 16;
    let mut out: Vec<i32> = Vec::with_capacity(groups_per_ch * ch);

    for g in 0..groups_per_ch {
        for c in 0..ch {
            // 该声道第 g 组的起始 bit 位置
            let bit_index = g * 16 * ch + c * 16;
            let word = read_dsd_bits(src, bit_index, 16);
            // marker：偶数帧 0x05，奇数帧 0xFA（按每个 PCM 样本帧交替）
            let marker: i32 = if g % 2 == 0 { 0x05 } else { 0xFA };
            let sample = ((word as i32) << 8) | marker;
            out.push(sample);
        }
    }
    out
}

/// 从交错 DSD 字节中读取从 `start_bit` 开始的 `n` 个 bit，返回右对齐整数。
/// DSD 位序：每个字节从 MSB 到 LSB。
fn read_dsd_bits(src: &[u8], start_bit: usize, n: usize) -> u32 {
    let mut v: u32 = 0;
    for i in 0..n {
        let bit_index = start_bit + i;
        let byte = bit_index / 8;
        let bit_in_byte = bit_index % 8;
        let bit = if byte < src.len() {
            (src[byte] >> (7 - bit_in_byte)) & 1
        } else {
            0
        };
        v = (v << 1) | bit as u32;
    }
    v
}

/// 将原始 DSD 字节按 Native 方式打包为 32-bit 样本（每 4 个 DSD 字节一组）。
///
/// 说明：USB DAC 的 native DSD 常见承载是 DSD_U32_LE——每 32-bit 字含 4 个 DSD 字节，
/// 但按字节流顺序排列（非位重排）。此处按最常见约定：直接取 4 个连续 DSD 字节写入。
/// 若具体 DAC 约定不同，可在此调整。
pub fn pack_native_u32(src: &[u8]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::with_capacity(src.len() / 4 + 1);
    let mut i = 0;
    while i + 4 <= src.len() {
        out.push(u32::from_le_bytes([src[i], src[i + 1], src[i + 2], src[i + 3]]));
        i += 4;
    }
    out
}
