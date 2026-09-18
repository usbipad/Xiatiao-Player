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

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
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

/// 应用侧环形缓冲目标「时间」（秒）。缓冲上限越大，越能吸收解码抖动。
/// 取 2 秒：足够覆盖解码/DSP/调度的瞬时抖动，内存开销可接受。
const RING_SECONDS: f64 = 2.0;

/// 预分配环形缓冲时假定的最高采样率。与 engine 的采样率上限一致（768kHz），
/// 保证任何被允许送入输出的采样率下缓冲都够大。
const RING_MAX_RATE: u32 = 768_000;

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
    ring: Ring,
    channels: AtomicUsize,
    paused: AtomicBool,
    /// 是否已播放过至少一帧（用于区分「播放中欠载」与「刚启动」）。
    started: AtomicBool,
    /// 实际已送入声卡播放的「帧」数（由 RT 回调累加）。
    /// 用于精确进度：它反映真正播放到的位置，而非解码/写入位置。
    played_frames: AtomicU64,
    /// 欠载计数（诊断）。
    underruns: AtomicU64,
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
}

impl PipewireOutput {
    pub fn new() -> Self {
        let ch = 2usize;
        let frames_cap = ring_frames(RING_MAX_RATE, ch);
        Self {
            shared: Arc::new(Shared {
                ring: Ring::new(frames_cap, ch),
                channels: AtomicUsize::new(ch),
                paused: AtomicBool::new(true),
                started: AtomicBool::new(false),
                played_frames: AtomicU64::new(0),
                underruns: AtomicU64::new(0),
            }),
            fmt: Mutex::new(None),
            device: Mutex::new(String::new()),
            stop: Arc::new(AtomicBool::new(false)),
            thread: Mutex::new(None),
        }
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
        let need_rebuild = {
            let g = self.fmt.lock().unwrap_or_else(|e| e.into_inner());
            *g != Some((rate, channels))
        };
        if need_rebuild {
            self.shared.channels.store(channels as usize, Ordering::Relaxed);
            self.shared.ring.clear();
            self.rebuild(rate, channels);
        }

        // 阻塞式写入：缓冲满时小睡等待，让解码跟随播放速度。
        // 注意：这里在**解码线程**（非 RT），sleep 是允许的。
        let ch = channels as usize;
        let mut off = 0;
        while off < pcm.len() {
            let wrote_frames = self.shared.ring.push(&pcm[off..]);
            off += wrote_frames * ch;
            if off < pcm.len() {
                if self.stop.load(Ordering::Relaxed) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }

    /// 清空缓冲（切歌 / seek）。
    pub fn flush(&self) {
        self.shared.ring.clear();
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
        eprintln!("[output] REBUILD stream: rate={rate} ch={channels} device='{device}'");
        self.stop();
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
        }
    };

    let stream = pw::stream::StreamBox::new(&core, CLIENT_NAME, props)
        .map_err(|e| e.to_string())?;

    let sh = Arc::clone(&shared);
    let stop2 = Arc::clone(&stop);
    let _listener = stream
        .add_local_listener_with_user_data(())
        .state_changed(|_, _, _old, _new| {})
        .process(move |stream, _| {
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

            let n_frames = if let Some(slice) = data.data() {
                let total_frames = slice.len() / frame_bytes;
                let total_samples = total_frames * ch;
                // 把字节缓冲视为 f32 切片（F32LE，对齐由 PipeWire 保证）。
                let dst = unsafe {
                    std::slice::from_raw_parts_mut(slice.as_mut_ptr() as *mut f32, total_samples)
                };
                if paused {
                    dst.fill(0.0);
                } else {
                    // 无锁读取；不足部分平滑补零。
                    let got_frames = sh.ring.pop(dst);
                    if got_frames > 0 {
                        sh.started.store(true, Ordering::Relaxed);
                        // 累加实际播放帧数（进度基准）。
                        sh.played_frames.fetch_add(got_frames as u64, Ordering::Relaxed);
                    }
                    if got_frames < total_frames {
                        // 欠载：记录（限流，避免刷屏）
                        let u = sh.underruns.fetch_add(1, Ordering::Relaxed) + 1;
                        if u % 20 == 1 {
                            eprintln!("[output] UNDERRUN#{u}: want={} got={} short={}",
                                total_frames, got_frames, total_frames - got_frames);
                        }
                        for s in dst[got_frames * ch..].iter_mut() {
                            *s = 0.0;
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
