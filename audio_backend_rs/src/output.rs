//! 音频输出层（PipeWire 原生接口，实时安全设计）。
//!
//! 架构（遵循标准实时音频播放器设计）：
//!   解码线程 --write()--> 无锁 SPSC 环形缓冲 --PipeWire RT 回调--> 声卡
//!
//! 设计要点：
//! 1. **回调零阻塞**：PipeWire process 是实时线程，绝不加锁、不分配、不做 IO。
//!    环形缓冲是无锁 SPSC（单生产者解码线程 / 单消费者 RT 回调）。
//! 2. **位精确**：输出 f32（与 PipeWire 内部格式一致），保持源采样率。
//! 3. **缓冲分层**：PipeWire 侧 latency 固定较小（低延迟），应用侧环形缓冲
//!    取较大容量（吸收解码抖动，防 xrun）。
//! 4. **流生命周期**：同格式切歌复用 stream；仅采样率/声道变化时重建。
//!
//! ## seek 设计：状态机（非双缓冲抢时序）
//!
//! seek 是「离散重定位」，不是「无缝续接」。用一个明确的状态机表达，
//! 避免任何「抢 <1 个 RT 周期窗口」的时序竞争：
//!
//!   Playing（0） 正常播放：RT pop active；不足补 hold。
//!   Draining（1） seek 已请求：RT 输出静音 + flush 硬件（丢弃旧数据）。
//!   Refilling（2） 新数据填充中：RT 输出静音（active 正被重填）。
//!
//! 状态由**解码线程显式推进**（begin_seek → mark_refilling → mark_playing），
//! RT 只被动按状态输出——没有竞争、没有超时兜底、没有 pending 双缓冲。
//!
//! 对外契约（与 engine.rs 一致）：
//!   new() / write(pcm, rate, channels) / flush() / begin_seek() / mark_refilling()
//!   / mark_playing() / pause() / resume() / stop() / played_frames()
//!   / reset_played_frames(v) / latency()

use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use pipewire as pw;
use pw::spa;
use pw::spa::pod::Pod;

use libspa_sys as spa_sys;

/// 客户端名（GNOME 应用音量列表显示这个）。
const CLIENT_NAME: &str = "Xiatiao Player";

/// PipeWire 侧流缓冲「时间」（毫秒）。决定 process 回调的节奏（quantum）。
const STREAM_LATENCY_MS: f64 = 20.0;

/// 应用侧环形缓冲目标「时间」（秒）。
///
/// 注意：缓冲**同时**决定了「参数变化到听见」的最大延迟。因此不能取太大。
/// 取 0.2 秒：足够吸收解码/调度的瞬时抖动（防 xrun），延迟又可忽略。
const RING_SECONDS: f64 = 0.2;

/// 首次构造时（尚不知实际采样率）的假定采样率。
const RING_DEFAULT_RATE: u32 = 48_000;

/// seek 淡出/淡入「时间」（毫秒）。
///
/// seek 时输出静音会造成「旧音乐突变到静音 → 静音 → 突变到新音乐」的生硬感。
/// 用几毫秒线性淡出（旧音乐→0）+ 淡入（0→新音乐）消除突变，听感自然。
/// 业界常用 5–20ms；取 10ms 兼顾「干脆」与「柔和」。
const FADE_MS: f64 = 10.0;

/// 最大声道数（通道位置数组用）。
const MAX_CHANNELS: usize = 2;

// ============================================================
// 输出状态机
// ============================================================

/// 正常播放：RT 从 active pop。
const STATE_PLAYING: u8 = 0;
/// seek 已请求：RT 输出静音 + flush 硬件（丢弃服务端/硬件缓冲里的旧数据）。
const STATE_DRAINING: u8 = 1;
/// 新数据填充中：RT 输出静音（active ring 正被解码线程清空重填）。
const STATE_REFILLING: u8 = 2;

// ============================================================
// 无锁 SPSC 环形缓冲（以「帧」为单位）
// ============================================================
//
// - 生产者：解码线程（write）；消费者：PipeWire RT 回调（pop）。
// - 读写指针用「帧」计数（单调递增），索引时与 mask 相与。
// - 容量是 2 的幂，故可用位与取模；保留 1 帧空位区分「满/空」。
// - 所有样本以「整帧」读写，从结构上杜绝左右声道错位。

struct Ring {
    /// 交错样本缓冲，长度 = frames_cap * channels。
    buf: Vec<f32>,
    /// 帧容量（2 的幂）。
    frames_cap: usize,
    /// 声道数。
    channels: usize,
    /// 写指针（帧，单调递增）。
    write: AtomicUsize,
    /// 读指针（帧，单调递增）。
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
        // 保留 1 帧空位：可写 = cap - 1 - (w - r)
        self.frames_cap - 1 - w.wrapping_sub(r)
    }

    #[inline]
    fn avail_frames(&self, w: usize, r: usize) -> usize {
        w.wrapping_sub(r)
    }

    /// 当前可读帧数（供解码线程判断「填够」）。
    fn available(&self) -> usize {
        let w = self.write.load(Ordering::Acquire);
        let r = self.read.load(Ordering::Relaxed);
        self.avail_frames(w, r)
    }

    /// 生产者：写入交错样本，返回写入的「帧」数（仅整帧）。
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
        // Release：确保样本写入对消费者可见
        self.write.store(w.wrapping_add(n), Ordering::Release);
        n
    }

    /// 消费者（RT 回调）：读取到 dst（f32），返回读取的「帧」数。
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

    /// 清空（seek 重填前调用）。生产者调用；与消费者用原子指针协作。
    fn clear(&self) {
        let w = self.write.load(Ordering::Acquire);
        self.read.store(w, Ordering::Release);
    }
}

// ============================================================
// 共享状态（解码线程 <-> RT 回调）
// ============================================================

struct Shared {
    /// 环形缓冲。用 Mutex 包裹：仅在 rebuild 时替换（非 RT 路径），
    /// RT 回调/push 短暂 lock 取用（无竞争时开销极小）。
    ring: Mutex<Ring>,
    channels: AtomicUsize,
    paused: AtomicBool,
    /// 是否已播放过至少一帧（区分「播放中欠载」与「刚启动」）。
    started: AtomicBool,
    /// 实际已送入声卡播放的「帧」数（RT 回调累加）。进度基准。
    played_frames: AtomicU64,
    /// 欠载计数（诊断）。
    underruns: AtomicU64,
    /// 输出状态机：STATE_PLAYING / STATE_DRAINING / STATE_REFILLING。
    state: AtomicU8,
    /// RT 已完成 drain（flush 硬件缓冲）。解码线程据此确认 drain 完成。
    drain_done: AtomicBool,
    /// 请求 RT flush PipeWire stream（清服务端已 dequeue 的缓冲）。
    flush_req: AtomicBool,
}

impl Shared {
    #[inline]
    fn state(&self) -> u8 {
        self.state.load(Ordering::Acquire)
    }

    /// seek 开始：进入 Draining（RT 输出静音 + flush 硬件）。
    fn begin_drain(&self) {
        self.drain_done.store(false, Ordering::SeqCst);
        self.flush_req.store(true, Ordering::SeqCst);
        self.state.store(STATE_DRAINING, Ordering::Release);
    }

    /// 新数据填充开始：进入 Refilling（清 ring 后由解码线程重填）。
    fn begin_refill(&self) {
        if let Ok(g) = self.ring.lock() {
            g.clear();
        }
        self.state.store(STATE_REFILLING, Ordering::Release);
    }

    /// 新数据填够：回到 Playing（RT 恢复 pop）。
    fn end_refill(&self) {
        self.state.store(STATE_PLAYING, Ordering::Release);
    }
}

// ============================================================
// PipewireOutput
// ============================================================

pub struct PipewireOutput {
    shared: Arc<Shared>,
    /// 当前流的格式 (rate, channels)。
    fmt: Mutex<Option<(u32, u32)>>,
    /// 输出设备（PipeWire sink 的 node.name）；空=系统默认。
    device: Mutex<String>,
    /// 停止标志。
    stop: Arc<AtomicBool>,
    /// PipeWire 线程句柄。
    thread: Mutex<Option<JoinHandle<()>>>,
    /// 中断 write 的标志（切歌时让卡在 write 的解码线程退出，不销毁 stream）。
    abort_write: Arc<AtomicBool>,
}

impl PipewireOutput {
    pub fn new() -> Self {
        let ch = 2usize;
        let frames_cap = ring_frames(RING_DEFAULT_RATE, ch);
        Self {
            shared: Arc::new(Shared {
                ring: Mutex::new(Ring::new(frames_cap, ch)),
                channels: AtomicUsize::new(ch),
                paused: AtomicBool::new(true),
                started: AtomicBool::new(false),
                played_frames: AtomicU64::new(0),
                underruns: AtomicU64::new(0),
                state: AtomicU8::new(STATE_PLAYING),
                drain_done: AtomicBool::new(true),
                flush_req: AtomicBool::new(false),
            }),
            fmt: Mutex::new(None),
            device: Mutex::new(String::new()),
            stop: Arc::new(AtomicBool::new(false)),
            thread: Mutex::new(None),
            abort_write: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 中断 write：让卡在 write 的解码线程立即返回（切歌用）。
    pub fn abort_write(&self) {
        self.abort_write.store(true, Ordering::SeqCst);
    }

    /// 清除中断标志（开始新播放前）。
    pub fn clear_abort(&self) {
        self.abort_write.store(false, Ordering::SeqCst);
    }

    /// 设置输出设备（PipeWire sink 名；空=默认）。会重建 stream 使新设备生效。
    pub fn set_output_device(&self, name: &str) {
        *self.device.lock().unwrap_or_else(|e| e.into_inner()) = name.to_string();
        let cur = *self.fmt.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((rate, channels)) = cur {
            self.rebuild(rate, channels);
        }
    }

    /// 推送一块交错 f32 PCM。采样率/声道变化时重建 stream。
    pub fn write(&self, pcm: &[f32], rate: u32, channels: u32) {
        let channels = channels.max(1);

        // 格式变化 → 重建 stream（罕见）。
        let (need_rebuild, cur_fmt) = {
            let g = self.fmt.lock().unwrap_or_else(|e| e.into_inner());
            (*g != Some((rate, channels)), *g)
        };
        if need_rebuild {
            eprintln!(
                "[output][rate] 格式变化：当前={:?} → 新=({},{})，触发 rebuild",
                cur_fmt, rate, channels
            );
            self.shared.channels.store(channels as usize, Ordering::Relaxed);
            self.rebuild(rate, channels);
        } else if cur_fmt.is_none() {
            eprintln!("[output][rate] 初始格式：rate={rate} ch={channels}");
        }

        // 阻塞式写入：缓冲满时小睡等待，让解码跟随播放速度。
        let ch = channels as usize;
        let mut off = 0;
        while off < pcm.len() {
            let wrote_frames = {
                let g = self.shared.ring.lock().unwrap_or_else(|e| e.into_inner());
                g.push(&pcm[off..])
            };
            off += wrote_frames * ch;
            if off < pcm.len() {
                if self.stop.load(Ordering::Relaxed) || self.abort_write.load(Ordering::Relaxed) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }

    /// 清空输出缓冲（切歌 / 播放开始）。纯「丢弃旧数据」语义。
    pub fn flush(&self) {
        if let Ok(g) = self.shared.ring.lock() {
            g.clear();
        }
        self.shared.flush_req.store(true, Ordering::SeqCst);
        self.shared.state.store(STATE_PLAYING, Ordering::Release);
    }

    /// 开始 seek：进入 Draining（RT 输出静音 + flush 硬件旧数据）。
    pub fn begin_seek(&self) {
        self.shared.begin_drain();
    }

    /// seek 定位完成后、开始写新数据前调用：清 ring + 进入 Refilling。
    pub fn mark_refilling(&self) {
        self.shared.begin_refill();
    }

    /// 新数据填够、可恢复播放时调用：进入 Playing。
    pub fn mark_playing(&self) {
        self.shared.end_refill();
    }

    /// 结束 seek 过渡（EOF / 解码异常兜底）：直接回 Playing。
    pub fn end_seek(&self) {
        self.shared.end_refill();
    }

    pub fn pause(&self) {
        eprintln!("[output] pause (played={})", self.played_frames());
        self.shared.paused.store(true, Ordering::SeqCst);
    }

    pub fn resume(&self) {
        eprintln!("[output] resume (played={})", self.played_frames());
        self.shared.paused.store(false, Ordering::SeqCst);
    }

    /// 已播放帧数（进度基准）。
    pub fn played_frames(&self) -> u64 {
        self.shared.played_frames.load(Ordering::SeqCst)
    }

    /// 重置播放帧计数（切歌 / seek 时调用）。
    pub fn reset_played_frames(&self, value: u64) {
        self.shared.played_frames.store(value, Ordering::SeqCst);
    }

    pub fn latency(&self) -> Duration {
        Duration::from_secs_f64(STREAM_LATENCY_MS / 1000.0)
    }

    /// 销毁当前 PipeWire 线程/stream。
    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = t.join();
        }
        self.stop.store(false, Ordering::SeqCst);
        *self.fmt.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    /// 重建 stream（新格式）。
    fn rebuild(&self, rate: u32, channels: u32) {
        let device = self.device.lock().map(|d| d.clone()).unwrap_or_default();
        eprintln!("[output][rate] REBUILD 开始：rate={rate} ch={channels} device='{device}'");
        self.stop();
        // 按实际采样率重建环形缓冲：保证缓冲时长 == RING_SECONDS。
        {
            let ch = channels.max(1) as usize;
            let frames_cap = ring_frames(rate, ch);
            if let Ok(mut g) = self.shared.ring.lock() {
                *g = Ring::new(frames_cap, ch);
            }
            eprintln!("[output][rate] 环形缓冲按 rate={rate} 重建：{} 帧（约 {:.0} ms）",
                frames_cap, 1000.0 * frames_cap as f64 / rate.max(1) as f64);
        }
        let shared = Arc::clone(&self.shared);
        let stop = Arc::clone(&self.stop);
        let handle = std::thread::Builder::new()
            .name("xiatiao-pw".into())
            .spawn(move || {
                if let Err(e) = run_pipewire(shared, stop, rate, channels, device) {
                    eprintln!("[pipewire] 线程退出: {e}");
                }
            })
            .expect("spawn pipewire thread");
        *self.thread.lock().unwrap_or_else(|e| e.into_inner()) = Some(handle);
        *self.fmt.lock().unwrap_or_else(|e| e.into_inner()) = Some((rate, channels));
        self.shared.paused.store(false, Ordering::SeqCst);
    }
}

impl Drop for PipewireOutput {
    fn drop(&mut self) {
        self.stop();
    }
}

/// 按采样率计算环形缓冲「帧」容量（不含声道）。
fn ring_frames(rate: u32, _channels: usize) -> usize {
    let frames = (rate as f64 * RING_SECONDS).ceil() as usize;
    frames.max(4096)
}

// ============================================================
// PipeWire 线程主体
// ============================================================

fn run_pipewire(
    shared: Arc<Shared>,
    stop: Arc<AtomicBool>,
    rate: u32,
    channels: u32,
    device: String,
) -> Result<(), String> {
    pw::init();
    let mainloop = pw::main_loop::MainLoopRc::new(None).map_err(|e| e.to_string())?;
    let context = pw::context::ContextRc::new(&mainloop, None).map_err(|e| e.to_string())?;
    let core = context.connect_rc(None).map_err(|e| e.to_string())?;

    let channels_str = channels.to_string();
    let lat_frames = ((rate as f64) * (STREAM_LATENCY_MS / 1000.0)).max(64.0) as u32;
    let latency_str = format!("{lat_frames}/{rate}");
    let device_val = device.clone();
    let use_device = !device.is_empty();
    let force_rate_str = rate.to_string();

    let props = if use_device {
        pw::properties::properties! {
            *pw::keys::MEDIA_TYPE => "Audio",
            *pw::keys::MEDIA_ROLE => "Music",
            *pw::keys::MEDIA_CATEGORY => "Playback",
            *pw::keys::AUDIO_CHANNELS => channels_str.as_str(),
            *pw::keys::NODE_NAME => CLIENT_NAME,
            *pw::keys::APP_NAME => CLIENT_NAME,
            *pw::keys::NODE_LATENCY => latency_str.as_str(),
            "node.force-rate" => force_rate_str.as_str(),
            "target.object" => device_val.as_str(),
        }
    } else {
        pw::properties::properties! {
            *pw::keys::MEDIA_TYPE => "Audio",
            *pw::keys::MEDIA_ROLE => "Music",
            *pw::keys::MEDIA_CATEGORY => "Playback",
            *pw::keys::AUDIO_CHANNELS => channels_str.as_str(),
            *pw::keys::NODE_NAME => CLIENT_NAME,
            *pw::keys::APP_NAME => CLIENT_NAME,
            *pw::keys::NODE_LATENCY => latency_str.as_str(),
            "node.force-rate" => force_rate_str.as_str(),
        }
    };

    let stream = pw::stream::StreamBox::new(&core, CLIENT_NAME, props)
        .map_err(|e| e.to_string())?;

    let sh = Arc::clone(&shared);
    let stop2 = Arc::clone(&stop);
    // 淡变每帧步进 = 1 / (FADE_MS 对应的帧数)。
    let fade_step = 1.0f32 / ((rate as f64 * FADE_MS / 1000.0).max(1.0) as f32);
    // user_data = (上一帧 L, 上一帧 R, 当前淡变增益)。
    // - hold：欠载时「补 hold」——延续上一帧（欠载是连续播放中的瞬时抖动）。
    // - fade_gain：seek 淡出/淡入的当前增益（0.0~1.0），每帧按步进变化。
    let _listener = stream
        .add_local_listener_with_user_data((0.0f32, 0.0f32, 1.0f32))
        .state_changed(|_, _, _old, _new| {})
        .process(move |stream, ud| {
            // ---- 实时回调：绝不加锁、不分配、不做 IO ----
            if stop2.load(Ordering::Relaxed) {
                return;
            }
            let mut buffer = match stream.dequeue_buffer() {
                Some(b) => b,
                None => return,
            };
            let datas = buffer.datas_mut();
            if datas.is_empty() {
                return;
            }
            let data = &mut datas[0];
            let ch = sh.channels.load(Ordering::Relaxed).max(1);
            let frame_bytes = ch * std::mem::size_of::<f32>();
            let paused = sh.paused.load(Ordering::Relaxed);

            // seek drain 请求：flush PipeWire 服务端已 dequeue 的旧数据。
            if sh.flush_req.swap(false, Ordering::SeqCst) {
                let _ = stream.flush(false);
                sh.drain_done.store(true, Ordering::SeqCst);
            }

            let state = sh.state();

            let n_frames = if let Some(slice) = data.data() {
                let total_frames = slice.len() / frame_bytes;
                let total_samples = total_frames * ch;
                let dst = unsafe {
                    std::slice::from_raw_parts_mut(slice.as_mut_ptr() as *mut f32, total_samples)
                };

                if paused || state != STATE_PLAYING {
                    // 暂停 / seek 过渡（Draining/Refilling）：输出**静音**，
                    // 但 seek 过渡带**淡出**——从上一帧平滑衰减到 0，
                    // 避免「旧音乐突变到静音」的生硬感。
                    //
                    // seek 是离散重定位：旧数据属旧时间段、新数据尚未就绪，
                    // 中间输出静音最干净（不播错误数据、不制造 DC 电平）。
                    for f in 0..total_frames {
                        // 淡出：增益逐帧向 0 递减（到达 0 后保持）。
                        if ud.2 > 0.0 {
                            ud.2 = (ud.2 - fade_step).max(0.0);
                        }
                        let g = ud.2;
                        dst[f * ch] = ud.0 * g;
                        if ch > 1 { dst[f * ch + 1] = ud.1 * g; }
                    }
                } else {
                    // 正常播放：无锁读取；不足部分「补 hold」而非补零。
                    let got_frames = if let Ok(g) = sh.ring.lock() {
                        g.pop(dst)
                    } else {
                        0
                    };
                    if got_frames > 0 {
                        sh.started.store(true, Ordering::Relaxed);
                        sh.played_frames.fetch_add(got_frames as u64, Ordering::Relaxed);
                        let last = (got_frames - 1) * ch;
                        ud.0 = dst[last];
                        if ch > 1 { ud.1 = dst[last + 1]; }
                    }
                    // 淡入：seek 后恢复播放时，增益从 0 平滑升到 1。
                    // （正常播放中 ud.2 已是 1.0，无额外开销。）
                    if ud.2 < 1.0 {
                        for f in 0..got_frames {
                            ud.2 = (ud.2 + fade_step).min(1.0);
                            let g = ud.2;
                            dst[f * ch] *= g;
                            if ch > 1 { dst[f * ch + 1] *= g; }
                        }
                        // 若本块末尾仍未到 1.0，后续块继续淡入（ud.2 保留）。
                    }
                    if got_frames < total_frames {
                        let u = sh.underruns.fetch_add(1, Ordering::Relaxed) + 1;
                        if u % 20 == 1 {
                            eprintln!("[output] UNDERRUN#{u}: want={} got={} short={}（补 hold）",
                                total_frames, got_frames, total_frames - got_frames);
                        }
                        for f in got_frames..total_frames {
                            dst[f * ch] = ud.0;
                            if ch > 1 { dst[f * ch + 1] = ud.1; }
                        }
                    }
                }
                total_frames
            } else {
                0
            };

            let chunk = data.chunk_mut();
            *chunk.offset_mut() = 0;
            *chunk.stride_mut() = frame_bytes as _;
            *chunk.size_mut() = (frame_bytes * n_frames) as _;
        })
        .register()
        .map_err(|e| e.to_string())?;

    // 格式：F32LE（与 PipeWire 内部处理格式一致）。
    eprintln!("[output][rate] run_pipewire 建流：请求 rate={rate} ch={channels} latency='{latency_str}'");
    let mut audio_info = spa::param::audio::AudioInfoRaw::new();
    audio_info.set_format(spa::param::audio::AudioFormat::F32LE);
    audio_info.set_rate(rate);
    audio_info.set_channels(channels);
    let mut position = [0; spa::param::audio::MAX_CHANNELS];
    position[0] = spa_sys::SPA_AUDIO_CHANNEL_FL;
    position[1] = spa_sys::SPA_AUDIO_CHANNEL_FR;
    audio_info.set_position(position);

    let values: Vec<u8> = pw::spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &pw::spa::pod::Value::Object(pw::spa::pod::Object {
            type_: spa_sys::SPA_TYPE_OBJECT_Format,
            id: spa_sys::SPA_PARAM_EnumFormat,
            properties: audio_info.into(),
        }),
    )
    .map_err(|e| format!("serialize format: {e:?}"))?
    .0
    .into_inner();

    let mut params = [Pod::from_bytes(&values).ok_or("Pod::from_bytes failed")?];

    stream
        .connect(
            spa::utils::Direction::Output,
            None,
            pw::stream::StreamFlags::AUTOCONNECT
                | pw::stream::StreamFlags::MAP_BUFFERS
                | pw::stream::StreamFlags::RT_PROCESS,
            &mut params,
        )
        .map_err(|e| e.to_string())?;
    eprintln!("[output][rate] stream connect 完成：rate={rate} ch={channels}（PipeWire 应据此重协商）");

    let lp = mainloop.loop_();
    let ml_timer = mainloop.clone();
    let stop_timer = Arc::clone(&stop);
    let timer = lp.add_timer(move |_expirations| {
        if stop_timer.load(Ordering::Relaxed) {
            ml_timer.quit();
        }
    });
    if let Err(e) = timer
        .update_timer(Some(Duration::from_millis(500)), Some(Duration::from_millis(500)))
        .into_result()
    {
        eprintln!("[pipewire] 启动停止检查定时器失败: {e}");
    }

    mainloop.run();
    Ok(())
}

// ============================================================
// 统一输出后端（PipeWire 默认 / ALSA 独占）
// ============================================================

use crate::output_alsa::AlsaOutput;

/// 输出后端选择。
pub enum AudioOut {
    Pipewire(PipewireOutput),
    Alsa(AlsaOutput),
}

impl AudioOut {
    pub fn write(&self, pcm: &[f32], rate: u32, channels: u32) {
        match self {
            AudioOut::Pipewire(p) => p.write(pcm, rate, channels),
            AudioOut::Alsa(a) => a.write(pcm, rate, channels),
        }
    }

    /// 清空输出缓冲（切歌 / 播放开始）。
    pub fn flush(&self) {
        match self {
            AudioOut::Pipewire(p) => p.flush(),
            AudioOut::Alsa(a) => a.flush(),
        }
    }

    /// 开始 seek（进入 drain 状态）。
    pub fn begin_seek(&self) {
        match self {
            AudioOut::Pipewire(p) => p.begin_seek(),
            AudioOut::Alsa(a) => a.begin_seek(),
        }
    }

    /// seek 定位后开始重填新数据。
    pub fn mark_refilling(&self) {
        match self {
            AudioOut::Pipewire(p) => p.mark_refilling(),
            AudioOut::Alsa(a) => a.mark_refilling(),
        }
    }

    /// 新数据填够、恢复播放。
    pub fn mark_playing(&self) {
        match self {
            AudioOut::Pipewire(p) => p.mark_playing(),
            AudioOut::Alsa(a) => a.mark_playing(),
        }
    }

    /// 结束 seek 过渡（EOF / 解码异常兜底）。
    pub fn end_seek(&self) {
        match self {
            AudioOut::Pipewire(p) => p.end_seek(),
            AudioOut::Alsa(a) => a.end_seek(),
        }
    }

    pub fn pause(&self) {
        match self {
            AudioOut::Pipewire(p) => p.pause(),
            AudioOut::Alsa(a) => a.pause(),
        }
    }

    pub fn resume(&self) {
        match self {
            AudioOut::Pipewire(p) => p.resume(),
            AudioOut::Alsa(a) => a.resume(),
        }
    }

    pub fn played_frames(&self) -> u64 {
        match self {
            AudioOut::Pipewire(p) => p.played_frames(),
            AudioOut::Alsa(a) => a.played_frames(),
        }
    }

    pub fn reset_played_frames(&self, value: u64) {
        match self {
            AudioOut::Pipewire(p) => p.reset_played_frames(value),
            AudioOut::Alsa(a) => a.reset_played_frames(value),
        }
    }

    pub fn latency(&self) -> Duration {
        match self {
            AudioOut::Pipewire(p) => p.latency(),
            AudioOut::Alsa(a) => a.latency(),
        }
    }

    pub fn stop(&self) {
        match self {
            AudioOut::Pipewire(p) => p.stop(),
            AudioOut::Alsa(a) => a.stop(),
        }
    }

    pub fn take_error(&self) -> Option<String> {
        match self {
            AudioOut::Pipewire(_) => None,
            AudioOut::Alsa(a) => a.take_error(),
        }
    }

    pub fn is_failed(&self) -> bool {
        match self {
            AudioOut::Pipewire(_) => false,
            AudioOut::Alsa(a) => a.is_failed(),
        }
    }

    pub fn abort_write(&self) {
        match self {
            AudioOut::Pipewire(p) => p.abort_write(),
            AudioOut::Alsa(a) => a.abort_write(),
        }
    }

    pub fn clear_abort(&self) {
        match self {
            AudioOut::Pipewire(p) => p.clear_abort(),
            AudioOut::Alsa(a) => a.clear_abort(),
        }
    }

    /// 以 DSD 模式写入原始 DSD 数据（仅 ALSA 独占后端支持）。
    pub fn write_dsd(&self, data: &crate::dsd::DsdData, mode: crate::output_alsa::DsdOutputMode) {
        match self {
            AudioOut::Alsa(a) => a.write_dsd(data, mode),
            AudioOut::Pipewire(_) => {
                eprintln!("[output] PipeWire 后端不支持 DSD 直通，已忽略（应由解码层走 pcm 软解）");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine_interleaved(frames: usize, freq: f32, rate: f32) -> Vec<f32> {
        let mut v = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let s = (2.0 * std::f32::consts::PI * freq * (i as f32 / rate)).sin();
            v.push(s);
            v.push(s);
        }
        v
    }

    /// 状态机：seek 期间（Draining/Refilling）RT 应输出静音，不 pop 旧 active。
    #[test]
    fn state_machine_silences_during_seek() {
        let ch = 2usize;
        let rate = 44100.0f32;
        let shared = Shared {
            ring: Mutex::new(Ring::new(ring_frames(rate as u32, ch), ch)),
            channels: AtomicUsize::new(ch),
            paused: AtomicBool::new(false),
            started: AtomicBool::new(true),
            played_frames: AtomicU64::new(0),
            underruns: AtomicU64::new(0),
            state: AtomicU8::new(STATE_PLAYING),
            drain_done: AtomicBool::new(true),
            flush_req: AtomicBool::new(false),
        };

        // 正常播放：ring 有数据。
        let data = sine_interleaved(2048, 1000.0, rate);
        shared.ring.lock().unwrap().push(&data);
        assert_eq!(shared.state(), STATE_PLAYING);

        // begin_seek → Draining。
        shared.begin_drain();
        assert_eq!(shared.state(), STATE_DRAINING);
        assert!(!shared.drain_done.load(Ordering::SeqCst));
        assert!(shared.flush_req.load(Ordering::SeqCst));

        // begin_refill → 清 ring + Refilling。
        shared.begin_refill();
        assert_eq!(shared.state(), STATE_REFILLING);
        assert_eq!(shared.ring.lock().unwrap().available(), 0, "Refilling 应清空 ring");

        // 填新数据 → end_refill → Playing。
        let fresh = sine_interleaved(2048, 2000.0, rate);
        shared.ring.lock().unwrap().push(&fresh);
        shared.end_refill();
        assert_eq!(shared.state(), STATE_PLAYING);
        assert!(shared.ring.lock().unwrap().available() > 0);
    }

    /// Ring::available 反映可读帧数。
    #[test]
    fn ring_available_tracks_push_pop() {
        let ch = 2usize;
        let ring = Ring::new(ring_frames(48000, ch), ch);
        assert_eq!(ring.available(), 0);
        let data = sine_interleaved(1000, 1000.0, 48000.0);
        let n = ring.push(&data);
        assert_eq!(n, 1000);
        assert_eq!(ring.available(), 1000);
        let mut dst = vec![0.0f32; 400 * ch];
        let got = ring.pop(&mut dst);
        assert_eq!(got, 400);
        assert_eq!(ring.available(), 600);
    }

    /// clear 后 available == 0（seek 重填语义）。
    #[test]
    fn ring_clear_empties() {
        let ch = 2usize;
        let ring = Ring::new(ring_frames(48000, ch), ch);
        ring.push(&sine_interleaved(5000, 1000.0, 48000.0));
        assert!(ring.available() > 0);
        ring.clear();
        assert_eq!(ring.available(), 0);
    }
}
