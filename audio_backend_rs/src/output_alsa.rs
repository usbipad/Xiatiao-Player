//! ALSA 硬件直连输出层（绕过音频服务，独占 hw: 设备）。
//!
//! 与 output.rs（PipeWire）的关系：
//!   - 默认输出设备为空 → 走 PipeWire（output.rs）；
//!   - 指定了 ALSA 设备 → 走本模块，独占打开 `hw:` 设备，直接写硬件。
//!
//! 架构沿用标准实时播放器设计：
//!   解码线程 --write()--> 无锁 SPSC 环形缓冲 --ALSA 写线程--> 声卡
//!
//! 对外契约与 PipewireOutput 完全一致：
//!   new(device) / write(pcm, rate, channels) / flush() / pause() / resume()
//!   / stop() / played_frames() / reset_played_frames(v) / latency()
//!
//! DSD 支持：
//!   - 通过 `write_native_group` / `write_dop_group` 接收**已按声道解交错**的
//!     DSD 字节（调用方负责处理 DSF 块交错 / DFF 字节交错），写线程内打包；
//!   - Native（DSD_U32_LE）：每 4 字节 → 1 个 32-bit 采样，设备率 = DSD 率/32；
//!   - DoP：每 2 字节 → 1 个 24-bit 采样（S32_LE 或 S24_3LE 承载），率 = DSD 率/16；
//!   - Native 需要设备支持 DSD_U32_LE，否则返回错误由上层回退。

use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use alsa::pcm::{Access, Format, HwParams, PCM};
use alsa::{Direction, ValueOr};


/// ALSA 缓冲周期（微秒）。标准低延迟取值。
const PERIOD_US: u32 = 20_000;
/// ALSA 缓冲总量（微秒）。
const BUFFER_US: u32 = 100_000;
/// 应用侧环形缓冲「时间」（秒），与 PipeWire 层一致（0.2s）。
const RING_SECONDS: f64 = 0.2;
/// 假定采样率（构造时未知，实际 rebuild 时按真实采样率重建）。
const RING_DEFAULT_RATE: u32 = 48_000;

/// seek 淡出/淡入「时间」（毫秒），与 PipeWire 层一致（消除过渡生硬感）。
const FADE_MS: f64 = 10.0;

// ============================================================
// 设备枚举
// ============================================================

/// 单个 ALSA 硬件设备描述。
#[derive(Debug, Clone)]
pub struct AlsaDevice {
    /// ALSA 设备名（如 "hw:CARD=Amplif,DEV=0"）。
    pub id: String,
    /// 可读描述（如 "Meizu HiFi DAC Headphone Amplif — USB Audio"）。
    pub description: String,
}

/// 枚举系统中所有 ALSA 播放硬件设备（跨版本兼容）。
///
/// 策略（三层，逐层回退，任何环境都不崩）：
///   1. **事实来源**：ALSA `Card::iter()` + `Ctl::from_card` + `DeviceIter`，
///      拿到真实存在的声卡与 pcm 设备编号（依赖 alsa-lib，跨版本稳定）。
///   2. **可读名**：从 `/proc/asound/card{idx}/pcm{dev}p/info` 读取设备
///      的 `id` / `name`（内核 ABI，长期稳定），拼成友好名；
///      读不到则回退到卡名（`Card::get_name`）。
///   3. **过滤**：跳过 Deep Buffer 这类纯技术冗余设备。
///
/// 生成的设备名统一为 `hw:CARD=<id>,DEV=<n>`（独占直连、零转换）。
pub fn list_devices() -> Vec<AlsaDevice> {
    // 主枚举源：/proc/asound/devices（权威列表，跨内核版本稳定）。
    // 它直接给出真实存在的 (card, device) 对，避免 alsa-lib 的
    // DeviceIter 在部分驱动下返回「幽灵编号」（如不存在的 dev 6/7）。
    // 若该文件不可读，回退到 alsa-lib 枚举。
    if let Some(pairs) = read_proc_playback_devices() {
        return build_from_pairs(&pairs);
    }
    list_devices_via_alsa()
}

/// 从 /proc/asound/devices 解析出所有真实存在的播放设备 (card, device) 对。
/// 返回 None 表示该文件不可读（调用方回退到 ALSA API）。
fn read_proc_playback_devices() -> Option<Vec<(i32, i32)>> {
    let text = std::fs::read_to_string("/proc/asound/devices").ok()?;
    let mut out = Vec::new();
    for line in text.lines() {
        // 形如："  2: [ 0- 0]: digital audio playback"
        if !line.contains("playback") {
            continue;
        }
        // 提取 "[ card- dev]"。
        let lb = match line.find('[') {
            Some(i) => i,
            None => continue,
        };
        let rb = match line[lb..].find(']') {
            Some(i) => lb + i,
            None => continue,
        };
        let inner = &line[lb + 1..rb]; // " 0- 0"
        let mut parts = inner.split('-');
        let card = parts.next().and_then(|s| s.trim().parse::<i32>().ok());
        let dev = parts.next().and_then(|s| s.trim().parse::<i32>().ok());
        if let (Some(c), Some(d)) = (card, dev) {
            out.push((c, d));
        }
    }
    Some(out)
}

/// 由 (card, device) 对构建设备列表（读 /proc/asound 取名字）。
fn build_from_pairs(pairs: &[(i32, i32)]) -> Vec<AlsaDevice> {
    let mut out: Vec<AlsaDevice> = Vec::new();
    for &(idx, dev) in pairs {
        // 卡 id（与 hw:CARD= 一致）。
        let card_id = read_card_id(idx).unwrap_or_else(|| idx.to_string());
        // 卡的描述名：优先可读描述，否则卡 id。
        let card_label = read_card_label(idx).unwrap_or_else(|| card_id.clone());
        // 设备可读名。
        let dev_label = read_pcm_label(idx, dev as u32);
        // 过滤 Deep Buffer 等纯技术冗余。
        if let Some(ref l) = dev_label {
            if l.contains("Deep Buffer") {
                continue;
            }
        }
        let id = format!("hw:CARD={card_id},DEV={dev}");
        let description = build_device_label(&card_label, dev_label.as_deref(), dev as u32);
        out.push(AlsaDevice { id, description });
    }
    out
}

/// 回退路径：用 alsa-lib API 枚举（当 /proc/asound/devices 不可读时）。
fn list_devices_via_alsa() -> Vec<AlsaDevice> {
    let mut out: Vec<AlsaDevice> = Vec::new();
    for card in alsa::card::Iter::new().filter_map(|c| c.ok()) {
        let idx = card.get_index();
        let card_id = read_card_id(idx)
            .or_else(|| card.get_name().ok())
            .unwrap_or_else(|| idx.to_string());
        let card_label = read_card_label(idx).unwrap_or_else(|| card_id.clone());
        let ctl = match alsa::ctl::Ctl::from_card(&card, false) {
            Ok(c) => c,
            Err(_) => continue,
        };
        for dev in alsa::ctl::DeviceIter::new(&ctl) {
            if dev < 0 {
                continue;
            }
            let dev_label = read_pcm_label(idx, dev as u32);
            if let Some(ref l) = dev_label {
                if l.contains("Deep Buffer") {
                    continue;
                }
            }
            let id = format!("hw:CARD={card_id},DEV={dev}");
            let description = build_device_label(&card_label, dev_label.as_deref(), dev as u32);
            out.push(AlsaDevice { id, description });
        }
    }
    out
}

/// 拼装设备显示名。
///
/// 优先级：
///   1. 若卡描述比设备 label 更有辨识度（如 USB DAC：卡名 "Meizu HiFi DAC..."
///      优于通用 label "USB Audio"），用卡描述；
///   2. 否则用设备 label（如 "HDMI 1"）；
///   3. 都没有则用卡名；dev>0 时附注编号。
fn build_device_label(card_label: &str, dev_label: Option<&str>, dev: u32) -> String {
    // 通用无辨识度的 label，不值得用。
    let generic = |s: &str| {
        s.is_empty()
            || s.eq_ignore_ascii_case("USB Audio")
            || s.eq_ignore_ascii_case("USB Stream Output")
    };
    match dev_label {
        Some(l) if !generic(l) && l != card_label => l.to_string(),
        _ => {
            if dev == 0 {
                card_label.to_string()
            } else {
                format!("{card_label} — 设备 {dev}")
            }
        }
    }
}

/// 读取 /proc/asound/cardN/id（卡稳定 id）。失败返回 None。
fn read_card_id(index: i32) -> Option<String> {
    let p = format!("/proc/asound/card{index}/id");
    std::fs::read_to_string(p).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

/// 读取 /proc/asound/cards 中该卡的可读描述（方括号后、冒号后的部分）。
/// 失败返回 None。
fn read_card_label(index: i32) -> Option<String> {
    let text = std::fs::read_to_string("/proc/asound/cards").ok()?;
    for line in text.lines() {
        // 形如：" 0 [Amplif         ]: USB-Audio - Meizu HiFi DAC ..."
        let t = line.trim_start();
        let mut it = t.splitn(2, |c: char| c == ' ' || c == '[');
        let num = it.next()?.trim();
        if num.parse::<i32>().ok() != Some(index) {
            continue;
        }
        // 取冒号后的描述部分。
        if let Some(colon) = line.find(':') {
            let desc = line[colon + 1..].trim();
            // 形如 "USB-Audio - Meizu HiFi DAC Headphone Amplif"，取 " - " 之后。
            if let Some(pos) = desc.find(" - ") {
                return Some(desc[pos + 3..].trim().to_string());
            }
            return Some(desc.to_string());
        }
    }
    None
}

/// 读取 /proc/asound/cardN/pcmMp/info 的设备可读名。
///
/// 优先 `name:`（如 "HDMI 1"），为空则 `id:`（如 "USB Audio"、"HDA Analog (*)"）。
/// 失败返回 None。
fn read_pcm_label(index: i32, dev: u32) -> Option<String> {
    let p = format!("/proc/asound/card{index}/pcm{dev}p/info");
    let text = std::fs::read_to_string(p).ok()?;
    let mut id = String::new();
    let mut name = String::new();
    let mut stream = String::new();
    for line in text.lines() {
        if let Some(v) = line.strip_prefix("id:") {
            id = v.trim().to_string();
        } else if let Some(v) = line.strip_prefix("name:") {
            name = v.trim().to_string();
        } else if let Some(v) = line.strip_prefix("stream:") {
            stream = v.trim().to_string();
        }
    }
    // 仅播放设备（防御：某些系统 info 可能混入 capture）。
    if !stream.is_empty() && stream != "PLAYBACK" {
        return None;
    }
    let raw = if !name.is_empty() { name } else { id };
    if raw.is_empty() {
        return None;
    }
    Some(clean_label(&raw))
}

/// 清理设备名：去掉 " (*)" 之类的内核标记。
fn clean_label(s: &str) -> String {
    s.replace(" (*)", "").replace("(*)", "").trim().to_string()
}

// ============================================================
// 无锁 SPSC 环形缓冲（与 output.rs 同构，仅管 f32）
// ============================================================

struct Ring {
    buf: Vec<f32>,
    frames_cap: usize,
    channels: usize,
    write: AtomicUsize,
    read: AtomicUsize,
}

impl Ring {
    fn new(frames_cap: usize, channels: usize) -> Self {
        let ch = channels.max(1);
        let frames_cap = frames_cap.max(4096).next_power_of_two();
        Self {
            buf: vec![0.0; frames_cap * ch],
            frames_cap,
            channels: ch,
            write: AtomicUsize::new(0),
            read: AtomicUsize::new(0),
        }
    }

    #[inline]
    fn free_frames(&self, w: usize, r: usize) -> usize {
        self.frames_cap - 1 - w.wrapping_sub(r)
    }

    #[inline]
    fn avail_frames(&self, w: usize, r: usize) -> usize {
        w.wrapping_sub(r)
    }

    fn push(&self, samples: &[f32]) -> usize {
        let ch = self.channels;
        let frames = samples.len() / ch;
        if frames == 0 {
            return 0;
        }
        let w = self.write.load(Ordering::Relaxed);
        let r = self.read.load(Ordering::Acquire);
        let free = self.free_frames(w, r);
        let n = frames.min(free);
        if n == 0 {
            return 0;
        }
        let mask = self.frames_cap - 1;
        let p = self.buf.as_ptr() as *mut f32;
        for f in 0..n {
            let src = &samples[f * ch..f * ch + ch];
            let off = ((w + f) & mask) * ch;
            unsafe {
                std::ptr::copy_nonoverlapping(src.as_ptr(), p.add(off), ch);
            }
        }
        self.write.store(w.wrapping_add(n), Ordering::Release);
        n
    }

    fn pop(&self, dst: &mut [f32]) -> usize {
        let ch = self.channels;
        let total_frames = dst.len() / ch;
        if total_frames == 0 {
            return 0;
        }
        let r = self.read.load(Ordering::Relaxed);
        let w = self.write.load(Ordering::Acquire);
        let avail = self.avail_frames(w, r);
        let n = total_frames.min(avail);
        let mask = self.frames_cap - 1;
        let p = self.buf.as_ptr();
        for f in 0..n {
            let off = ((r + f) & mask) * ch;
            let dst_off = f * ch;
            unsafe {
                std::ptr::copy_nonoverlapping(p.add(off), dst.as_mut_ptr().add(dst_off), ch);
            }
        }
        if n > 0 {
            self.read.store(r.wrapping_add(n), Ordering::Release);
        }
        n
    }

    fn clear(&self) {
        let w = self.write.load(Ordering::Acquire);
        self.read.store(w, Ordering::Release);
    }
}

// ============================================================
// 共享状态
// ============================================================

/// 输出状态机（与 PipeWire 层一致）。
const STATE_PLAYING: u8 = 0;
const STATE_DRAINING: u8 = 1;
const STATE_REFILLING: u8 = 2;

struct Shared {
    ring: Mutex<Ring>,
    channels: AtomicUsize,
    paused: AtomicBool,
    played_frames: AtomicU64,
    underruns: AtomicU64,
    /// 输出状态机（Playing/Draining/Refilling）。
    state: AtomicU8,
}

impl Shared {
    #[inline]
    fn state(&self) -> u8 {
        self.state.load(Ordering::Acquire)
    }
}

// ============================================================
// AlsaOutput
// ============================================================

pub struct AlsaOutput {
    /// 设备名（hw:CARD=...,DEV=...）。
    device: String,
    shared: Arc<Shared>,
    fmt: Mutex<Option<(u32, u32)>>,
    stop: Arc<AtomicBool>,
    thread: Mutex<Option<JoinHandle<()>>>,
    /// 是否以 DSD 模式运行（决定写线程如何解读数据）。
    dsd_mode: Mutex<Option<DsdOutputMode>>,
    /// 写线程失败时写入的错误信息（供 engine 读取转发给 UI）。
    error: Arc<Mutex<Option<String>>>,
    /// 写线程是否已失败退出（write 据此立即返回，避免无限等待死锁）。
    failed: Arc<AtomicBool>,
    /// 上一次 rebuild 使用的 DSD 模式（用于判断 PCM/DSD 切换是否需要重建）。
    last_dsd: Mutex<Option<DsdOutputMode>>,
    /// 中断 write 的标志（切歌时让卡在 write 的解码线程退出）。
    abort_write: Arc<AtomicBool>,
}

/// DSD 输出模式（native / dop）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DsdOutputMode {
    Native,
    Dop,
}

impl AlsaOutput {
    pub fn new(device: &str) -> Self {
        let ch = 2usize;
        let frames_cap = ring_frames(RING_DEFAULT_RATE, ch);
        Self {
            device: device.to_string(),
            shared: Arc::new(Shared {
                ring: Mutex::new(Ring::new(frames_cap, ch)),
                channels: AtomicUsize::new(ch),
                paused: AtomicBool::new(true),
                played_frames: AtomicU64::new(0),
                underruns: AtomicU64::new(0),
                state: AtomicU8::new(STATE_PLAYING),
            }),
            fmt: Mutex::new(None),
            stop: Arc::new(AtomicBool::new(false)),
            thread: Mutex::new(None),
            dsd_mode: Mutex::new(None),
            error: Arc::new(Mutex::new(None)),
            failed: Arc::new(AtomicBool::new(false)),
            last_dsd: Mutex::new(None),
            abort_write: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 中断 write：让卡在 write 的解码线程立即返回（切歌用）。
    pub fn abort_write(&self) {
        self.abort_write.store(true, Ordering::SeqCst);
    }

    /// 清除中断标志。
    pub fn clear_abort(&self) {
        self.abort_write.store(false, Ordering::SeqCst);
    }

    pub fn device(&self) -> &str {
        &self.device
    }

    /// 取出并清空最近一次写线程错误（供 engine 转发给 UI）。
    pub fn take_error(&self) -> Option<String> {
        self.error.lock().ok().and_then(|mut s| s.take())
    }

    /// 写线程是否已失败退出（供解码主循环检测，及时中止并回退）。
    pub fn is_failed(&self) -> bool {
        self.failed.load(Ordering::SeqCst)
    }

    /// 写入一块交错 f32 PCM（普通 PCM 路径）。采样率/声道变化时重建。
    pub fn write(&self, pcm: &[f32], rate: u32, channels: u32) {
        // 普通 PCM：dsd=None（确保不会残留 DSD 标志）。
        self.write_inner(pcm, rate, channels, None);
    }

    /// 内部写入：显式指定 DSD 模式（PCM 传 None，DSD 传 Some）。
    ///
    /// 关键：DSD 模式由调用方显式传入，而非从 self.dsd_mode 读取，
    /// 避免上一次 DSD 播放的残留值污染后续普通 PCM 播放。
    fn write_inner(&self, pcm: &[f32], rate: u32, channels: u32, dsd: Option<DsdOutputMode>) {
        let channels = channels.max(1);
        // 记录当前 DSD 模式（供写线程/日志），并判断是否需要重建。
        *self.dsd_mode.lock().unwrap_or_else(|e| e.into_inner()) = dsd;
        let cur_dsd_for_cmp = dsd;
        let (need_rebuild, _cur) = {
            let g = self.fmt.lock().unwrap_or_else(|e| e.into_inner());
            // 若 DSD 模式变化，也需要重建。
            let prev_dsd = *self.last_dsd.lock().unwrap_or_else(|e| e.into_inner());
            (*g != Some((rate, channels)) || prev_dsd != cur_dsd_for_cmp, *g)
        };
        if need_rebuild {
            self.shared.channels.store(channels as usize, Ordering::Relaxed);
            self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).clear();
            self.rebuild(rate, channels, dsd);
            *self.last_dsd.lock().unwrap_or_else(|e| e.into_inner()) = dsd;
        }

        let ch = channels as usize;
        let mut off = 0;
        while off < pcm.len() {
            // 写线程已失败退出（如 ALSA 打开失败）：立即返回，
            // 否则会因无人消费环形缓冲而无限等待，导致 join 死锁。
            if self.failed.load(Ordering::Relaxed) {
                return;
            }
            // 切歌中断：立即返回，避免卡住 join。
            if self.abort_write.load(Ordering::Relaxed) {
                return;
            }
            let wrote = self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).push(&pcm[off..]);
            off += wrote * ch;
            if off < pcm.len() {
                if self.stop.load(Ordering::Relaxed) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }

    /// 以 Native（DSD_U32_LE）模式写入一组**已按声道解交错**的 DSD 字节。
    ///
    /// - `ch_bytes[c]` 为声道 c 的连续 DSD 字节（MSB-first），长度需一致且为 4 的倍数；
    /// - 每个 32-bit ALSA 采样承载 4 个连续 DSD 字节（DSD_U32_LE，按字节流顺序）；
    /// - ALSA 设备采样率 = `dsd_rate / 32`（一个采样 = 32 个 DSD bit）。
    pub fn write_native_group(&self, ch_bytes: &[Vec<u8>], dsd_rate: u32) {
        let ch = ch_bytes.len().max(1);
        // 复用 dsd.rs 中经单测的纯打包逻辑。
        let words = crate::dsd::pack_native_group(ch_bytes);
        let pcm: Vec<f32> = words.into_iter().map(f32::from_bits).collect();
        let alsa_rate = (dsd_rate / 32).max(1);
        self.write_inner(&pcm, alsa_rate, ch as u32, Some(DsdOutputMode::Native));
    }

    /// 以 DoP v1.0 模式写入一组**已按声道解交错**的 DSD 字节。
    ///
    /// - `ch_bytes[c]` 为声道 c 的连续 DSD 字节，长度需一致且为 2 的倍数；
    /// - 每 16 个 DSD bit（2 字节）打包为 1 个 24-bit PCM 样本的高 16 位，
    ///   低 8 位为 marker（每帧在 0x05 / 0xFA 间交替，所有声道同帧同 marker）；
    /// - 承载格式由写线程选择（S32_LE 或 S24_3LE）；样本按高 24 位左对齐存储；
    /// - ALSA 设备采样率 = `pcm_rate`（= DSD 率 / 16）。
    pub fn write_dop_group(&self, ch_bytes: &[Vec<u8>], pcm_rate: u32, marker_phase: &mut usize) {
        let ch = ch_bytes.len().max(1);
        // 复用 dsd.rs 中经单测的纯打包逻辑（位精确 + marker 交替）。
        let samples = crate::dsd::pack_dop_group(ch_bytes, marker_phase);
        let pcm: Vec<f32> = samples.into_iter().map(f32::from_bits).collect();
        self.write_inner(&pcm, pcm_rate, ch as u32, Some(DsdOutputMode::Dop));
    }

    /// 清空输出缓冲（切歌 / 播放开始）。
    pub fn flush(&self) {
        self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).clear();
        self.shared.state.store(STATE_PLAYING, Ordering::Release);
    }

    /// 开始 seek：进入 Draining（写线程输出静音 + 淡出，直至重填完成）。
    pub fn begin_seek(&self) {
        self.shared.state.store(STATE_DRAINING, Ordering::Release);
    }

    /// seek 定位后、开始写新数据前：清 ring + 进入 Refilling。
    pub fn mark_refilling(&self) {
        self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).clear();
        self.shared.state.store(STATE_REFILLING, Ordering::Release);
    }

    /// 新数据填够、恢复播放。
    pub fn mark_playing(&self) {
        self.shared.state.store(STATE_PLAYING, Ordering::Release);
    }

    /// 结束 seek 过渡（EOF / 解码异常兜底）：直接回 Playing。
    pub fn end_seek(&self) {
        self.shared.state.store(STATE_PLAYING, Ordering::Release);
    }

    pub fn pause(&self) {
        self.shared.paused.store(true, Ordering::SeqCst);
    }

    pub fn resume(&self) {
        self.shared.paused.store(false, Ordering::SeqCst);
    }

    pub fn played_frames(&self) -> u64 {
        self.shared.played_frames.load(Ordering::SeqCst)
    }

    pub fn reset_played_frames(&self, value: u64) {
        self.shared.played_frames.store(value, Ordering::SeqCst);
    }

    pub fn latency(&self) -> Duration {
        Duration::from_micros(BUFFER_US as u64)
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = t.join();
        }
        self.stop.store(false, Ordering::SeqCst);
        *self.fmt.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    /// 重建 ALSA 流（新格式 / 新 DSD 模式）。
    fn rebuild(&self, rate: u32, channels: u32, dsd: Option<DsdOutputMode>) {
        self.stop();
        {
            let ch = channels.max(1) as usize;
            let frames_cap = ring_frames(rate, ch);
            let mut g = self.shared.ring.lock().unwrap_or_else(|e| e.into_inner());
            *g = Ring::new(frames_cap, ch);
        }
        let shared = Arc::clone(&self.shared);
        let stop = Arc::clone(&self.stop);
        let device = self.device.clone();
        let error_slot = Arc::clone(&self.error);
        let failed_flag = Arc::clone(&self.failed);
        // 新一次 rebuild：清除上一次的失败标志。
        self.failed.store(false, Ordering::SeqCst);
        let handle = std::thread::Builder::new()
            .name("xiatiao-alsa".into())
            .spawn(move || {
                if let Err(e) = run_alsa(shared, stop, &device, rate, channels, dsd) {
                    eprintln!("[alsa] 写线程退出: {e}");
                    // 置失败标志：让解码线程的 write 立即返回，避免死锁。
                    failed_flag.store(true, Ordering::SeqCst);
                    // 记录错误，供 engine 转发给 UI。
                    if let Ok(mut slot) = error_slot.lock() {
                        *slot = Some(e);
                    }
                }
            })
            .expect("spawn alsa thread");
        *self.thread.lock().unwrap_or_else(|e| e.into_inner()) = Some(handle);
        *self.fmt.lock().unwrap_or_else(|e| e.into_inner()) = Some((rate, channels));
        self.shared.paused.store(false, Ordering::SeqCst);
    }
}

impl Drop for AlsaOutput {
    fn drop(&mut self) {
        self.stop();
    }
}

fn ring_frames(rate: u32, _channels: usize) -> usize {
    let frames = (rate as f64 * RING_SECONDS).ceil() as usize;
    frames.max(4096)
}

// ============================================================
// ALSA 写线程主体
// ============================================================

fn run_alsa(
    shared: Arc<Shared>,
    stop: Arc<AtomicBool>,
    device: &str,
    rate: u32,
    channels: u32,
    dsd: Option<DsdOutputMode>,
) -> Result<(), String> {
    // 独占打开 hw 设备（非阻塞）。
    // 策略（C）：先直接打开；若失败（通常因 PipeWire 占用该硬件），
    // 自动释放对应的 PipeWire sink 后重试；仍失败才报错。
    // 阻塞模式打开（与 aplay 一致）：writei 阻塞等缓冲空位。
    // 写线程每写一个 period（约 20ms）就检查一次 stop，故 stop 时
    // 最多等一个 period 即可退出，不会死锁。
    let pcm = match PCM::new(device, Direction::Playback, false) {
        Ok(p) => p,
        Err(first_err) => {
            eprintln!("[alsa] 首次打开 {device} 失败: {first_err}，尝试释放 PipeWire 占用后重试");
            release_pipewire_for(device);
            // 给 PipeWire 一点时间真正释放硬件。
            std::thread::sleep(Duration::from_millis(300));
            PCM::new(device, Direction::Playback, false).map_err(|e| {
                format!(
                    "无法独占音频设备（{device}）。该设备可能正被其他程序占用。\n\
                     请关闭其他正在播放声音的应用，或在系统声音设置中把输出切换到别的设备后重试。\n\
                     （底层错误：{e}）"
                )
            })?
        }
    };

    // ---- 硬件参数配置 ----
    // 按硬件实际能力选择承载格式：优先 F32LE；多数 USB DAC 只支持整数
    // PCM（S16/S24_3/S32），此时用 S32_LE 承载，并在写入时把 f32 转 i32。
    let mut chosen = Format::FloatLE;
    {
        let hwp = HwParams::any(&pcm).map_err(|e| format!("HwParams: {e}"))?;
        hwp.set_channels(channels).map_err(|e| format!("set_channels: {e}"))?;
        // set_rate：hw: 设备不支持重采样。若设备是固定采样率（如某些内置
        // 声卡固定 48000），请求其它采样率会失败。此时给出清晰错误，
        // 由上层回退到 PipeWire（它负责重采样，保证音调正确）。
        if let Err(e) = hwp.set_rate(rate, ValueOr::Nearest) {
            let rmin = hwp.get_rate_min().unwrap_or(0);
            let rmax = hwp.get_rate_max().unwrap_or(0);
            let hint = if rmin == rmax {
                format!("该设备为固定采样率 {} Hz", rmin)
            } else {
                format!("该设备支持 {}–{} Hz", rmin, rmax)
            };
            return Err(format!(
                "无法以 {rate} Hz 独占打开设备（{hint}），且独占模式不做重采样。\n\
                 请选择其它输出设备，或改用“自动（系统默认）”（由系统负责重采样）。\n\
                 （底层错误：{e}）"
            ));
        }

        // Native DSD 模式：优先用原生 DSD 格式打开设备。
        // 依次尝试 DSD_U32_LE / DSD_U16_LE / DSD_U8，任一成功即用。
        if matches!(dsd, Some(DsdOutputMode::Native)) {
            // Native 直通统一采用 DSD_U32_LE：每个 32-bit 采样承载 4 个 DSD 字节，
            // 设备采样率 = DSD bit 率 / 32（见 write_native_group）。
            if hwp.set_format(Format::DSDU32LE).is_err() {
                return Err(
                    "设备不支持原生 DSD 格式（DSD_U32_LE），无法 Native 直通。\n\
                     请改用“DoP”或“转 PCM（软解）”模式播放。"
                        .into(),
                );
            }
            chosen = Format::DSDU32LE;
        } else if matches!(dsd, Some(DsdOutputMode::Dop)) {
            // DoP 承载：优先 S32_LE（24-bit 样本放高 24 位），
            // 回退 S24_3LE（紧凑 3 字节容器，部分 DoP DAC 只支持它）。
            if hwp.set_format(Format::S32LE).is_ok() {
                chosen = Format::S32LE;
            } else if hwp.set_format(Format::S243LE).is_ok() {
                chosen = Format::S243LE;
            } else {
                return Err(
                    "设备不支持 DoP 承载格式（S32_LE / S24_3LE）。\n\
                     该设备可能不支持 DoP；请改用“转 PCM（软解）”模式播放。"
                        .to_string(),
                );
            }
        } else if hwp.set_format(Format::FloatLE).is_ok() {
            chosen = Format::FloatLE;
        } else {
            hwp.set_format(Format::S32LE).map_err(|e| format!("set_format: {e}"))?;
            chosen = Format::S32LE;
        }
        let _ = hwp.set_access(Access::RWInterleaved);
        let _ = hwp.set_period_size_near(
            (rate as f64 * PERIOD_US as f64 / 1_000_000.0) as alsa::pcm::Frames,
            ValueOr::Nearest,
        );
        let _ = hwp.set_buffer_size_near(
            (rate as f64 * BUFFER_US as f64 / 1_000_000.0) as alsa::pcm::Frames,
        );
        pcm.hw_params(&hwp).map_err(|e| format!("hw_params: {e}"))?;
    }

    let actual_rate = pcm.hw_params_current().and_then(|h| h.get_rate()).unwrap_or(rate);
    let actual_ch = pcm.hw_params_current().and_then(|h| h.get_channels()).unwrap_or(channels);
    eprintln!(
        "[alsa] 独占打开 {device}: rate={actual_rate} ch={actual_ch} fmt={chosen:?} dsd={dsd:?}"
    );

    pcm.prepare().map_err(|e| format!("prepare: {e}"))?;

    // 按实际格式选择 IO 句柄。
    let io_f32 = if chosen == Format::FloatLE {
        Some(pcm.io_f32().map_err(|e| format!("io_f32: {e}"))?)
    } else {
        None
    };
    // S32LE 与 DSD_U32_LE 都用 i32 IO（DSD 按 32-bit 字承载）。
    let use_i32_io = chosen == Format::S32LE || chosen == Format::DSDU32LE;
    let io_i32 = if use_i32_io {
        Some(pcm.io_i32().map_err(|e| format!("io_i32: {e}"))?)
    } else {
        None
    };
    // S24_3LE：紧凑 3 字节容器（DoP 回退承载），用字节 IO 写入。
    let io_bytes = if chosen == Format::S243LE {
        Some(pcm.io_bytes())
    } else {
        None
    };
    // 是否位精确写入（不做缩放）：Native（DSD_U32）与 DoP 都需要。
    // DoP 虽用 S32 承载，但 24-bit 样本必须位精确（含 marker），
    // 否则归一化会破坏 marker 导致白噪音。
    let is_dsd = chosen == Format::DSDU32LE || matches!(dsd, Some(DsdOutputMode::Dop));

    // 写线程：从环形缓冲取数据，按 period 写入 ALSA。
    let period = pcm
        .hw_params_current()
        .and_then(|h| h.get_period_size())
        .unwrap_or(1024)
        .max(64) as usize;
    let mut scratch: Vec<f32> = vec![0.0; period * actual_ch as usize];
    let mut conv: Vec<i32> = vec![0; period * actual_ch as usize];
    // 淡变每帧步进 = 1 / (FADE_MS 对应帧数)。
    let fade_step = 1.0f32 / ((actual_rate as f64 * FADE_MS / 1000.0).max(1.0) as f32);
    // 当前淡变增益（0.0~1.0）：seek 过渡时淡出到 0，恢复时淡入到 1。
    let mut fade_gain = 1.0f32;
    // 上一帧输出（淡出期间「延续」的最后一帧）。
    let mut last_l = 0.0f32;
    let mut last_r = 0.0f32;

    while !stop.load(Ordering::SeqCst) {
        let paused = shared.paused.load(Ordering::Relaxed);
        let ch = shared.channels.load(Ordering::Relaxed).max(1);
        // seek 过渡（Draining/Refilling）或暂停：输出静音，不 pop 旧数据。
        let seek_transition = shared.state() != STATE_PLAYING;
        let got = if paused || seek_transition {
            0
        } else {
            shared.ring.lock().unwrap_or_else(|e| e.into_inner()).pop(&mut scratch)
        };

        if got == 0 {
            // 无数据（暂停 / seek 过渡 / 欠载）：保持时钟。
            let n = scratch.len();
            if seek_transition || paused {
                // seek / 暂停：**淡出**——从上一帧按递减增益衰减到 0
                //（避免「旧音乐突变到静音」的生硬感），之后保持静音。
                for i in (0..n).step_by(ch) {
                    fade_gain = (fade_gain - fade_step).max(0.0);
                    let g = fade_gain;
                    scratch[i] = last_l * g;
                    if ch > 1 { scratch[i + 1] = last_r * g; }
                }
            } else {
                // 欠载：补 hold（延续上一帧），不用淡变。
                for i in (0..n).step_by(ch) {
                    scratch[i] = last_l;
                    if ch > 1 { scratch[i + 1] = last_r; }
                }
            }
            let _ = write_alsa(&io_f32, &io_i32, &io_bytes, &scratch, &mut conv, 0, is_dsd);
            std::thread::sleep(Duration::from_millis(2));
            continue;
        }

        // 恢复播放：若在淡入中（fade_gain < 1），本块逐帧升到 1。
        if fade_gain < 1.0 {
            for i in (0..got * ch).step_by(ch) {
                fade_gain = (fade_gain + fade_step).min(1.0);
                scratch[i] *= fade_gain;
                if ch > 1 { scratch[i + 1] *= fade_gain; }
            }
        }
        // 记录本块最后一帧（供 seek 淡出时「延续」）。
        let last = (got - 1) * ch;
        last_l = scratch[last];
        if ch > 1 { last_r = scratch[last + 1]; }

        let n_samples = got * ch;
        match write_alsa(&io_f32, &io_i32, &io_bytes, &scratch[..n_samples], &mut conv, n_samples, is_dsd) {
            Ok(()) => {
                shared.played_frames.fetch_add(got as u64, Ordering::Relaxed);
            }
            Err(e) => {
                // EPIPE（xrun）时恢复。
                let u = shared.underruns.fetch_add(1, Ordering::Relaxed) + 1;
                if u % 20 == 1 {
                    eprintln!("[alsa] writei 失败/xrun#{u}: {e}");
                }
                let _ = pcm.recover(e.errno(), true);
            }
        }
    }

    let _ = pcm.drain();
    Ok(())
}

/// 按实际格式把 f32 样本写入 ALSA。
///
/// - F32LE：直接 writei f32；
/// - S32LE：按 2^31 缩放转 i32，再 writei；
/// - DSD（DSD_U32）：f32 是位模式承载，**原样取位**转 i32，不做缩放。
///
/// 返回 alsa::Error 以保留 errno，供上层 xrun 恢复判断。
fn write_alsa(
    io_f32: &Option<alsa::pcm::IO<f32>>,
    io_i32: &Option<alsa::pcm::IO<i32>>,
    io_bytes: &Option<alsa::pcm::IO<u8>>,
    src: &[f32],
    conv: &mut [i32],
    n: usize,
    is_dsd: bool,
) -> Result<(), alsa::Error> {
    if let Some(io) = io_f32 {
        let s = if n == 0 { src } else { &src[..n] };
        io.writei(s)?;
        return Ok(());
    }
    if let Some(io) = io_i32 {
        let s = if n == 0 { src } else { &src[..n] };
        if is_dsd {
            // DSD：f32 是 u32 位模式承载，原样取位，绝不缩放。
            for (i, &v) in s.iter().enumerate() {
                conv[i] = v.to_bits() as i32;
            }
        } else {
            // 普通 PCM：钳制到 [-1, 1]，线性映射到 i32 全域。
            for (i, &v) in s.iter().enumerate() {
                let clamped = v.clamp(-1.0, 1.0);
                conv[i] = (clamped * 2_147_483_647.0) as i32;
            }
        }
        io.writei(&conv[..s.len()])?;
        return Ok(());
    }
    if let Some(io) = io_bytes {
        // S24_3LE：每样本 3 字节小端。样本值存于 f32 位模式的高 24 位。
        let s = if n == 0 { src } else { &src[..n] };
        let mut bbuf: Vec<u8> = Vec::with_capacity(s.len() * 3);
        for &v in s {
            let v24 = (v.to_bits() >> 8) & 0x00FF_FFFF;
            bbuf.push((v24 & 0xFF) as u8);
            bbuf.push(((v24 >> 8) & 0xFF) as u8);
            bbuf.push(((v24 >> 16) & 0xFF) as u8);
        }
        io.writei(&bbuf)?;
        return Ok(());
    }
    // 理论上不会发生（chosen 必为 F32 / S32 / S24_3 / DSD）。
    eprintln!("[alsa] 无可用的 ALSA IO 句柄（不应发生）");
    Ok(())
}

/// 查询指定 ALSA 设备支持的最大采样率（用于 DoP 规格预检）。
///
/// 打开设备并读取 HwParams 的采样率范围，返回上限；失败返回 None。
/// 用非阻塞方式打开（不独占），探测完即释放。
pub fn device_max_rate(device: &str) -> Option<u32> {
    let pcm = PCM::new(device, Direction::Playback, true).ok()?;
    let hwp = HwParams::any(&pcm).ok()?;
    hwp.get_rate_max().ok()
}

/// 探测设备是否支持原生 DSD 格式（DSD_U32_LE / DSD_U16_LE / DSD_U8）。
///
/// 依次尝试各 DSD 格式，任一支持即返回 true。失败返回 false。
pub fn device_supports_dsd(device: &str, dsd_rate: u32, channels: u32) -> bool {
    // Native 直通统一采用 DSD_U32_LE，设备采样率 = DSD bit 率 / 32。
    // （DSD_U16/U8 承载未实现，不再宣称支持，避免"探测通过却写不出"。）
    let rate = (dsd_rate / 32).max(1);
    device_supports_format(device, Format::DSDU32LE, rate, channels)
}

/// 测试指定 ALSA 设备是否支持某种格式 / 采样率（用于 Native DSD 能力探测）。
pub fn device_supports_format(device: &str, format: Format, rate: u32, channels: u32) -> bool {
    let pcm = match PCM::new(device, Direction::Playback, true) {
        Ok(p) => p,
        Err(_) => return false,
    };
    let hwp = match HwParams::any(&pcm) {
        Ok(h) => h,
        Err(_) => return false,
    };
    if hwp.set_channels(channels).is_err() {
        return false;
    }
    hwp.test_format(format).is_ok() && hwp.test_rate(rate).is_ok()
}

/// 释放占用指定 ALSA 硬件的 PipeWire sink，使 ALSA 可以独占打开。
///
/// 步骤：
///   1. 从 device（`hw:CARD=<name>,DEV=<n>`）解析卡名与设备号；
///   2. 遍历 `pactl list sinks`，找出 `alsa.card_name`（或 `api.alsa.card.name`）
///      等于该卡名、且 `alsa.device` 等于该设备号的 sink；
///   3. 对该 sink 执行 `pactl suspend-sink <name> 1`，令 PipeWire 释放硬件。
///
/// 找不到 pactl、或没有匹配 sink 时静默返回（由调用方重试打开并最终报错）。
fn release_pipewire_for(device: &str) {
    let (card, dev) = match parse_hw_device(device) {
        Some(v) => v,
        None => {
            eprintln!("[alsa] 无法解析设备名 {device}，跳过 PipeWire 释放");
            return;
        }
    };

    // 读取所有 sink 的原始信息。
    let out = match std::process::Command::new("pactl")
        .args(["list", "sinks"])
        .output()
    {
        Ok(o) => o,
        Err(e) => {
            eprintln!("[alsa] 调用 pactl 失败: {e}（无法自动释放 PipeWire）");
            return;
        }
    };
    let text = String::from_utf8_lossy(&out.stdout);

    // 逐个 sink 块解析属性，匹配目标卡/设备。
    let mut cur_name: Option<String> = None;
    let mut cur_card_name: Option<String> = None;
    let mut cur_api_card_name: Option<String> = None;
    let mut cur_device: Option<String> = None;

    let flush = |name: &Option<String>,
                 card_name: &Option<String>,
                 api_card_name: &Option<String>,
                 sink_dev: &Option<String>| {
        if let Some(n) = name {
            // 卡名比较用规范化（去连字符/空格、转小写），因为 ALSA 卡 id
            // （如 "sofhdadsp"）与 PipeWire 的 alsa.card_name（如 "sof-hda-dsp"）
            // 可能带/不带连字符，直接相等比较会漏匹配。
            let norm = |s: &str| s.to_ascii_lowercase().replace(['-', ' ', '_'], "");
            let want = norm(&card);
            let card_match = card_name.as_deref().map(norm).as_deref() == Some(want.as_str())
                || api_card_name.as_deref().map(norm).as_deref() == Some(want.as_str());
            let dev_match = sink_dev.as_deref() == Some(dev.as_str());
            if card_match && dev_match {
                eprintln!("[alsa] 释放 PipeWire sink: {n}");
                let _ = std::process::Command::new("pactl")
                    .args(["suspend-sink", n, "1"])
                    .output();
            }
        }
    };

    for line in text.lines() {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("Name: ") {
            // 遇到新 sink：先处理上一个。
            flush(&cur_name, &cur_card_name, &cur_api_card_name, &cur_device);
            cur_name = Some(rest.trim().to_string());
            cur_card_name = None;
            cur_api_card_name = None;
            cur_device = None;
        } else if let Some(rest) = t.strip_prefix("alsa.card_name = ") {
            cur_card_name = Some(rest.trim().trim_matches('"').to_string());
        } else if let Some(rest) = t.strip_prefix("api.alsa.card.name = ") {
            cur_api_card_name = Some(rest.trim().trim_matches('"').to_string());
        } else if let Some(rest) = t.strip_prefix("alsa.device = ") {
            cur_device = Some(rest.trim().trim_matches('"').to_string());
        }
    }
    // 处理最后一个 sink。
    flush(&cur_name, &cur_card_name, &cur_api_card_name, &cur_device);
}

/// 解析 `hw:CARD=<name>,DEV=<n>`，返回 (卡名, 设备号字符串)。
fn parse_hw_device(device: &str) -> Option<(String, String)> {
    let s = device.strip_prefix("hw:").unwrap_or(device);
    let mut card = None;
    let mut dev = None;
    for part in s.split(',') {
        let part = part.trim();
        if let Some(v) = part.strip_prefix("CARD=") {
            card = Some(v.to_string());
        } else if let Some(v) = part.strip_prefix("DEV=") {
            dev = Some(v.to_string());
        }
    }
    match (card, dev) {
        (Some(c), Some(d)) => Some((c, d)),
        _ => None,
    }
}


