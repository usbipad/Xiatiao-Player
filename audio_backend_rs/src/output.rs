//! 音频输出层（PipeWire 原生接口，实时安全设计）。
//!
//! 架构（遵循标准实时音频播放器设计）：
//!   解码线程 --write()--> 无锁 SPSC 环形缓冲 --PipeWire RT 回调--> 声卡
//!
//! 设计要点：
//! 1. **回调零阻塞**：PipeWire process 是实时线程，绝不加锁、不分配、不做 IO。
//!    因此环形缓冲是无锁 SPSC（单生产者解码线程 / 单消费者 RT 回调），
//!    用原子读写指针 + Acquire/Release 内存序同步。
//! 2. **位精确**：输出 f32（与 PipeWire 内部格式一致），保持源采样率，
//!    不重采样；采样率转换交给 PipeWire/声卡。
//! 3. **缓冲分层**：PipeWire 侧 latency 取固定较小时间（低延迟），
//!    应用侧环形缓冲取较大容量（吸收解码抖动，防 xrun）。
//! 4. **流生命周期**：同格式切歌复用 stream；仅采样率/声道变化时重建。
//!
//! 对外契约（与 engine.rs 一致）：
//!   new() / write(pcm, rate, channels) / flush() / pause() / resume()
//!   / stop() / latency()

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
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
/// 固定为时间常量，按流采样率换算成帧数，保证任何采样率下回调间隔一致。
const STREAM_LATENCY_MS: f64 = 20.0;

/// 应用侧环形缓冲目标「时间」（秒）。
///
/// 注意：缓冲**同时**决定了「参数变化到听见」的最大延迟——缓冲里积压的
/// 数据必须先播完，新的 DSP/音效才生效。因此**不能取太大**。
/// 取 0.1 秒：足够吸收解码/调度的瞬时抖动（防 xrun），延迟又可忽略。
/// （此前取 2.0 秒，配合按 768kHz 预分配，导致 48kHz 下实际积压 ~32 秒，
///  表现为「切音效/调 DSP 要等十几秒才生效」。）
const RING_SECONDS: f64 = 0.2;

/// 首次构造时（尚不知实际采样率）的假定采样率。
/// 真正的缓冲会在 rebuild(rate) 时**按实际采样率**重建，
/// 保证任何采样率下缓冲时长都等于 RING_SECONDS。
const RING_DEFAULT_RATE: u32 = 48_000;

/// 最大声道数（通道位置数组用）。
const MAX_CHANNELS: usize = 2;

// ============================================================
// 无锁 SPSC 环形缓冲（以「帧」为单位）
// ============================================================
//
// - 生产者：解码线程（write）；消费者：PipeWire RT 回调（pop）。
// - 读写指针用「帧」计数（单调递增），索引时与 mask 相与。
// - 容量是 2 的幂，故可用位与取模；保留 1 帧空位区分「满/空」。
// - 所有样本以「整帧」读写，从结构上杜绝左右声道错位。
// - 回调只做内存拷贝，无锁、无分配。

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

    /// 生产者：写入交错样本，返回写入的「帧」数（仅整帧）。
    /// 无锁 SPSC：生产者只写 [write, write+n) 区间，消费者只读 [read, read+n)，
    /// 区间不重叠，故用原始指针访问是安全的。
    fn push(&self, samples: &[f32]) -> usize {
        let ch = self.channels;
        let frames = samples.len() / ch;
        if frames == 0 {
            return 0;
        }
        let w = self.write.load(Ordering::Relaxed);
        // 与消费者同步：读取消费进度
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

    /// 消费者（RT 回调）：读取到 dst（f32），返回读取的「帧」数（仅整帧）。
    /// 无锁、无分配，仅拷贝。
    fn pop(&self, dst: &mut [f32]) -> usize {
        let ch = self.channels;
        let total_frames = dst.len() / ch;
        if total_frames == 0 {
            return 0;
        }
        let r = self.read.load(Ordering::Relaxed);
        // 与生产者同步：读取写入进度
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
            // Release：确保读取进度对生产者可见
            self.read.store(r.wrapping_add(n), Ordering::Release);
        }
        n
    }

    /// 清空（切歌 / seek）。生产者调用；与消费者用原子指针协作。
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
    /// 这样可**按实际采样率**重建容量，避免「按最高采样率预分配」
    /// 导致低采样率下缓冲时长远超预期（曾因此引入 ~32 秒延迟）。
    ring: Mutex<Ring>,
    channels: AtomicUsize,
    paused: AtomicBool,
    /// 是否已播放过至少一帧（用于区分「播放中欠载」与「刚启动」）。
    started: AtomicBool,
    /// 实际已送入声卡播放的「帧」数（由 RT 回调累加）。
    /// 用于精确进度：它反映真正播放到的位置，而非解码/写入位置。
    played_frames: AtomicU64,
    /// 欠载计数（诊断）。
    underruns: AtomicU64,
    /// seek/切歌：请求在 RT 回调里 flush PipeWire stream（清服务端缓冲）。
    /// flush() 只清软件 ring，清不掉 PipeWire 已 dequeue 的硬件缓冲（~node.latency，
    /// 20ms）——这正是 seek 后「混入 20-50ms 旧声音」的根因。RT 回调检查此标志，
    /// 置位时调 pw stream.flush(false) 丢弃服务端缓冲。
    flush_req: AtomicBool,
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
                flush_req: AtomicBool::new(false),
            }),
            fmt: Mutex::new(None),
            device: Mutex::new(String::new()),
            stop: Arc::new(AtomicBool::new(false)),
            thread: Mutex::new(None),
            abort_write: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 中断 write：让卡在 write 的解码线程立即返回（切歌用，不销毁 stream）。
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
        // 若已有活动流，重建以应用新设备。
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
            self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).clear();
            self.rebuild(rate, channels);
        } else {
            // 仅在 rate 首次出现时打印，避免刷屏
            if cur_fmt.is_none() {
                eprintln!("[output][rate] 初始格式：rate={rate} ch={channels}");
            }
        }

        // 阻塞式写入：缓冲满时小睡等待，让解码跟随播放速度。
        // 注意：这里在**解码线程**（非 RT），sleep 是允许的。
        let ch = channels as usize;
        let mut off = 0;
        while off < pcm.len() {
            let wrote_frames = self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).push(&pcm[off..]);
            off += wrote_frames * ch;
            if off < pcm.len() {
                if self.stop.load(Ordering::Relaxed)
                    || self.abort_write.load(Ordering::Relaxed)
                {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }

    /// 清空缓冲（切歌 / seek）。
    ///
    /// 清软件 ring + 置 flush_req（让 RT 回调 flush PipeWire 服务端缓冲，
    /// 清掉已 dequeue 的 ~20ms 硬件缓冲，避免 seek 后混入旧声音）。
    pub fn flush(&self) {
        self.shared.ring.lock().unwrap_or_else(|e| e.into_inner()).clear();
        self.shared.flush_req.store(true, Ordering::SeqCst);
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
        eprintln!("[output][rate] 旧 stream 已停止（线程已 join）");
        // 按实际采样率重建环形缓冲：保证缓冲时长 == RING_SECONDS，
        // 不随采样率变化（这是消除「参数变化延迟」的关键）。
        {
            let ch = channels.max(1) as usize;
            let frames_cap = ring_frames(rate, ch);
            let mut g = self.shared.ring.lock().unwrap_or_else(|e| e.into_inner());
            *g = Ring::new(frames_cap, ch);
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
/// 注意：Ring::new 内部会 × channels 得到样本存储量。
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
    // node.latency = 帧数/采样率，是「时间」。按固定时间（20ms）换算成与
    // 当前 rate 匹配的帧数，保证任何采样率下缓冲「时间」一致。
    let lat_frames = ((rate as f64) * (STREAM_LATENCY_MS / 1000.0)).max(64.0) as u32;
    let latency_str = format!("{lat_frames}/{rate}");
    // 目标设备名（非空时用于把流路由到指定 sink）。
    let device_val = device.clone();
    // 设备非空时，额外加 target.object 属性。
    let use_device = !device.is_empty();

    // node.force-rate：请求 PipeWire 把 graph 采样率切到本流采样率，
    // 使 DAC 跟随源采样率（而非固定 48k 重采样）。这是「采样率跟随」的关键。
    let force_rate_str = rate.to_string();
    // 统一构造属性（有设备时加 target.object）。
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
    // user_data = 上一帧输出的 (L, R)，用于欠载/过渡时「补 hold」——
    // 延续上一帧而非补零（补零=突兀静音；hold=自然延续，业界标准做法）。
    let _listener = stream
        .add_local_listener_with_user_data((0.0f32, 0.0f32))
        .state_changed(|_, _, _old, _new| {})
        .process(move |stream, hold| {
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
            // seek/切歌：丢弃 PipeWire 服务端缓冲（清掉已 dequeue 的旧数据），
            // **但不再填静音**——直接继续从 ring 取新位置数据。
            // 旧实现 flush 后填满静音，导致：seek 后 RT 取到空 ring 就播
            // 整块静音（12288 帧 = 279ms @44.1k），听觉上「点到后要等」。
            // 改为：flush 后立即 pop ring（seek 时解码线程已开始填新数据，
            // 若暂无则取多少算多少，不补满静音）。
            if sh.flush_req.swap(false, Ordering::SeqCst) {
                let _ = stream.flush(false);
                // 不 return：继续走下方 pop ring 的正常流程。
            }

            let n_frames = if let Some(slice) = data.data() {
                let total_frames = slice.len() / frame_bytes;
                let total_samples = total_frames * ch;
                // 把字节缓冲视为 f32 切片（F32LE，对齐由 PipeWire 保证）。
                let dst = unsafe {
                    std::slice::from_raw_parts_mut(slice.as_mut_ptr() as *mut f32, total_samples)
                };
                if paused {
                    // 暂停：填 hold（延续上一帧），避免突然静音/爆点。
                    for f in 0..total_frames {
                        dst[f * ch] = hold.0;
                        if ch > 1 { dst[f * ch + 1] = hold.1; }
                    }
                } else {
                    // 无锁读取；不足部分「补 hold」而非补零。
                    let got_frames = sh.ring.lock().unwrap_or_else(|e| e.into_inner()).pop(dst);
                    if got_frames > 0 {
                        sh.started.store(true, Ordering::Relaxed);
                        // 累加实际播放帧数（进度基准）。
                        sh.played_frames.fetch_add(got_frames as u64, Ordering::Relaxed);
                        // 记录最后一帧，供下次 hold 用。
                        let last = (got_frames - 1) * ch;
                        hold.0 = dst[last];
                        if ch > 1 { hold.1 = dst[last + 1]; }
                    }
                    if got_frames < total_frames {
                        // 欠载：记录（限流，避免刷屏）
                        let u = sh.underruns.fetch_add(1, Ordering::Relaxed) + 1;
                        if u % 20 == 1 {
                            eprintln!("[output] UNDERRUN#{u}: want={} got={} short={}（补 hold）",
                                total_frames, got_frames, total_frames - got_frames);
                        }
                        // 标准做法：不足部分用 hold（延续上一帧），不补零。
                        for f in got_frames..total_frames {
                            dst[f * ch] = hold.0;
                            if ch > 1 { dst[f * ch + 1] = hold.1; }
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

    // 低频定时器：周期检查 stop，为 true 则退出主循环（同线程 quit）。
    // 不使用 add_idle（会忙循环占满 CPU）。
    let lp = mainloop.loop_();
    let ml_timer = mainloop.clone();
    let stop_timer = Arc::clone(&stop);
    let timer = lp.add_timer(move |_expirations| {
        if stop_timer.load(Ordering::Relaxed) {
            ml_timer.quit();
        }
    });
    if let Err(e) = timer
        .update_timer(
            Some(Duration::from_millis(500)),
            Some(Duration::from_millis(500)),
        )
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
//
// Engine 与解码层通过本枚举写入，无需关心底层是 PipeWire 还是 ALSA：
//   - 输出设备为空（默认）→ PipeWire（系统默认音频接口）；
//   - 指定了 ALSA hw 设备 → ALSA 独占直连。
//
// 设计：不使用 trait 对象，避免改动解码函数签名时的借用复杂度；
// 用枚举转发，语义清晰，性能无损耗。

use crate::output_alsa::AlsaOutput;

/// 输出后端选择。
pub enum AudioOut {
    /// PipeWire（默认，走系统音频服务）。
    Pipewire(PipewireOutput),
    /// ALSA 独占硬件设备。
    Alsa(AlsaOutput),
}

impl AudioOut {
    /// 写入一块交错 f32 PCM。
    pub fn write(&self, pcm: &[f32], rate: u32, channels: u32) {
        match self {
            AudioOut::Pipewire(p) => p.write(pcm, rate, channels),
            AudioOut::Alsa(a) => a.write(pcm, rate, channels),
        }
    }

    /// 清空输出缓冲（切歌 / seek）。
    pub fn flush(&self) {
        match self {
            AudioOut::Pipewire(p) => p.flush(),
            AudioOut::Alsa(a) => a.flush(),
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

    /// 取出并清空后端产生的运行时错误（目前仅 ALSA 独占后端的写线程会写入）。
    pub fn take_error(&self) -> Option<String> {
        match self {
            AudioOut::Pipewire(_) => None,
            AudioOut::Alsa(a) => a.take_error(),
        }
    }

    /// 后端是否已失败退出（ALSA 写线程失败时为 true）。
    pub fn is_failed(&self) -> bool {
        match self {
            AudioOut::Pipewire(_) => false,
            AudioOut::Alsa(a) => a.is_failed(),
        }
    }

    /// 中断 write（切歌时让卡在 write 的解码线程退出，不销毁 stream）。
    pub fn abort_write(&self) {
        match self {
            AudioOut::Pipewire(p) => p.abort_write(),
            AudioOut::Alsa(a) => a.abort_write(),
        }
    }

    /// 清除中断标志（开始新播放前）。
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
            // PipeWire 后端不支持原生 DSD 直通：此处不应被调用（由解码层分流）。
            AudioOut::Pipewire(_) => {
                eprintln!("[output] PipeWire 后端不支持 DSD 直通，已忽略（应由解码层走 pcm 软解）");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 生成连续的正弦样本（模拟正常播放数据流），用于验证 seek 后是否产生静音空洞。
    fn sine_interleaved(frames: usize, freq: f32, rate: f32) -> Vec<f32> {
        let mut v = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let s = (2.0 * std::f32::consts::PI * freq * (i as f32 / rate)).sin();
            v.push(s);
            v.push(s);
        }
        v
    }

    /// 复现核心问题：
    /// 模拟 RT 回调「每个 buffer 固定取 N 帧」。seek（clear）后，
    /// 如果解码线程尚未写入新数据，RT 取到 0 帧 → 整块填静音 → 听觉空洞。
    #[test]
    fn seek_without_prefill_causes_silence() {
        let ch = 2usize;
        let rate = 44100.0f32;
        let buffer_frames = 6144usize; // 实测 PipeWire 一次给 6144 帧
        let ring = Ring::new(ring_frames(rate as u32, ch), ch);

        // 正常播放：ring 填满
        let data = sine_interleaved(buffer_frames * 2, 1000.0, rate);
        ring.push(&data);

        // ---- seek：清空 ring ----
        ring.clear();

        // seek 后 RT 立刻取一个 buffer（此时解码线程还没写入新数据）
        let mut dst = vec![0.0f32; buffer_frames * ch];
        let got = ring.pop(&mut dst);
        // 不足部分补零（与 RT 回调一致）
        let silent = dst[got * ch..].iter().all(|&s| s == 0.0);
        println!("[复现] seek 后 RT 取到 {got} 帧 / 需 {buffer_frames} 帧，补静音={silent}");
        assert_eq!(got, 0, "seek 后 ring 应为空（复现空洞）");
        assert!(silent, "seek 后应补静音（复现听觉空洞）");
    }

    /// 验证修复：seek 后「预填充」ring，RT 取到时即有数据，无静音空洞。
    #[test]
    fn seek_with_prefill_no_silence() {
        let ch = 2usize;
        let rate = 44100.0f32;
        let buffer_frames = 6144usize;
        let ring = Ring::new(ring_frames(rate as u32, ch), ch);

        let data = sine_interleaved(buffer_frames * 2, 1000.0, rate);
        ring.push(&data);
        ring.clear();

        // ---- 修复：seek 后解码线程先预填充一个 buffer 的新数据 ----
        let fresh = sine_interleaved(buffer_frames, 1000.0, rate);
        ring.push(&fresh);

        // RT 取
        let mut dst = vec![0.0f32; buffer_frames * ch];
        let got = ring.pop(&mut dst);
        let all_sound = dst.iter().any(|&s| s.abs() > 1e-6);
        println!("[修复] seek 后 RT 取到 {got} 帧 / 需 {buffer_frames} 帧，有声音={all_sound}");
        assert_eq!(got, buffer_frames, "预填充后应取满一个 buffer");
        assert!(all_sound, "预填充后无静音空洞");
    }
}
