//! 可视化旁路：把播放 PCM 降采样为单声道 16kHz，**均匀地**写入命名管道（FIFO）。
//!
//! 设计背景：
//! - 上层（解码 packet / ffmpeg 读块）一次给一大块 PCM（如 4096 样本 ≈ 90ms），
//!   若直接整块写入 FIFO，Python 侧会「每 90ms 收到一大坨」，频谱呈脉冲式跳动
//!   （走一下、冻 90ms、再走）。
//! - 本模块用一个独立写入线程，把重采样后的 16kHz 样本**按固定节拍**
//!   （每 ~16ms 写一小块）均匀写入 FIFO，使消费端读到的数据流平滑。
//!
//! 设计原则（与实时音频工程一致）：
//! - 音频回调线程里只做「重采样 + 入队」，**绝不阻塞播放**；
//! - 队列有界，满了直接丢帧（可视化丢几帧无所谓）；
//! - FIFO 以 O_NONBLOCK 打开，写不进去（无人读 / 缓冲满）直接丢帧。

use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Receiver, Sender, TrySendError};
use nix::fcntl::{open, OFlag};
use nix::sys::stat::Mode;

/// 旁路输出采样率：16kHz 足够画到 ~8kHz 频谱。
pub const VIZ_RATE: u32 = 16_000;

/// 重试打开 FIFO 的最小间隔。
const RETRY_INTERVAL: Duration = Duration::from_millis(500);

/// 写入线程的节拍：每 tick 写一小块。16ms ≈ 60Hz，与渲染节拍匹配。
const WRITE_TICK: Duration = Duration::from_millis(16);
/// 每 tick 写入的最大样本数（16kHz × 16ms = 256）。
const WRITE_SAMPLES_PER_TICK: usize = (VIZ_RATE as usize * 16) / 1000;
/// 队列容量（样本数）：约 500ms 的 16kHz 数据，够吸收抖动，又不占太多内存。
const QUEUE_CAP: usize = (VIZ_RATE as usize) / 2;

/// 可视化旁路写入端（音频线程持有）。
///
/// feed() 只做重采样 + 入队；实际写 FIFO 由内部写入线程按节拍完成。
pub struct VizTap {
    /// 重采样后的 16kHz 单声道样本发送端（有界队列）。
    tx: Sender<f32>,
    /// 重采样比例：源样本 / 输出样本。
    ratio: f64,
    /// 下一个输出样本在「源样本坐标系」中的位置。
    pos: f64,
    /// 上一批最后一个源样本值（跨块插值用）。
    prev: f32,
    /// 是否已有 prev。
    has_prev: bool,
    /// 写入线程句柄（Drop 时停止）。
    writer: Option<JoinHandle<()>>,
    /// 线程停止标志。
    stop: Arc<std::sync::atomic::AtomicBool>,
}

impl VizTap {
    /// 创建写入端并启动写入线程。
    pub fn open(fifo_path: &str, in_rate: u32) -> Self {
        let ratio = (in_rate as f64 / VIZ_RATE as f64).max(1.0);
        let (tx, rx) = bounded::<f32>(QUEUE_CAP);
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let writer = start_writer(PathBuf::from(fifo_path), rx, Arc::clone(&stop));
        VizTap {
            tx,
            ratio,
            pos: 0.0,
            prev: 0.0,
            has_prev: false,
            writer: Some(writer),
            stop,
        }
    }

    /// 音频线程调用：交错 PCM → 取左声道 → 线性插值重采样到 16kHz → 入队。
    ///
    /// 只做搬运，不阻塞：队列满则丢帧（可视化丢帧无影响）。
    pub fn feed(&mut self, pcm: &[f32], channels: usize) {
        if channels == 0 {
            return;
        }
        let frames = pcm.len() / channels;
        if frames == 0 {
            return;
        }
        let ratio = self.ratio;
        let samp = |i: usize| -> f32 { pcm[i * channels] };
        let mut pos = self.pos;
        let frames_f = frames as f64;
        while pos >= frames_f {
            pos -= frames_f;
        }
        while pos < frames_f {
            let i = pos.floor() as usize;
            let frac = (pos - i as f64) as f32;
            let s0 = if i == 0 && self.has_prev { self.prev } else { samp(i) };
            let s1 = if i + 1 < frames { samp(i + 1) } else { s0 };
            let s = s0 + (s1 - s0) * frac;
            // 入队（满则丢，不阻塞音频线程）
            match self.tx.try_send(s) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => {}
                Err(TrySendError::Disconnected(_)) => break,
            }
            pos += ratio;
        }
        self.pos = pos - frames_f;
        if self.pos < 0.0 {
            self.pos = 0.0;
        }
        self.prev = samp(frames - 1);
        self.has_prev = true;
    }
}

impl Drop for VizTap {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        if let Some(h) = self.writer.take() {
            let _ = h.join();
        }
    }
}

/// 启动写入线程：从队列取样本，按固定节拍（每 ~16ms）写一小块到 FIFO。
fn start_writer(path: PathBuf, rx: Receiver<f32>, stop: Arc<std::sync::atomic::AtomicBool>) -> JoinHandle<()> {
    thread::Builder::new()
        .name("viz-writer".to_string())
        .spawn(move || {
            // 懒打开 + 周期重试（读端就绪前先缓存；这里不缓存，直接等）
            let mut file: Option<std::fs::File> = None;
            let mut last_try = Instant::now() - RETRY_INTERVAL;
            // 每 tick 收集的样本缓冲
            let mut acc: Vec<f32> = Vec::with_capacity(WRITE_SAMPLES_PER_TICK);
            // 下一 tick 的截止时间
            let mut next_tick = Instant::now();

            while !stop.load(std::sync::atomic::Ordering::SeqCst) {
                // 懒打开 FIFO（O_NONBLOCK 写端）
                if file.is_none() && last_try.elapsed() >= RETRY_INTERVAL {
                    last_try = Instant::now();
                    if let Ok(fd) = open(path.as_path(), OFlag::O_WRONLY | OFlag::O_NONBLOCK, Mode::empty()) {
                        file = Some(std::fs::File::from(fd));
                    }
                }

                // 本 tick 尽量收集最多 WRITE_SAMPLES_PER_TICK 个样本
                acc.clear();
                while acc.len() < WRITE_SAMPLES_PER_TICK {
                    match rx.try_recv() {
                        Ok(s) => acc.push(s),
                        Err(_) => break,
                    }
                }

                // 写入 FIFO
                if let Some(f) = file.as_mut() {
                    if !acc.is_empty() {
                        let mut bytes = Vec::with_capacity(acc.len() * 4);
                        for s in &acc {
                            bytes.extend_from_slice(&s.to_le_bytes());
                        }
                        let _ = f.write_all(&bytes); // 失败（FIFO 满/断）丢弃
                    }
                }

                // 按节拍对齐：睡到下一 tick。
                // 关键：落后时「补偿追赶」，而非「重置丢弃」——
                // 旧实现落后就 next_tick = now + tick，等于丢掉这一拍，
                // 表现为偶尔 16ms→32ms（律动偶尔卡一下）。
                // 现在：落后 1~4 拍不 sleep，立即写下一拍把时间补回来；
                // 落后超过 4 拍（严重卡顿）才重置基准，避免爆发式追赶。
                next_tick += WRITE_TICK;
                let now = Instant::now();
                if next_tick > now {
                    thread::sleep(next_tick - now);
                } else if now - next_tick > WRITE_TICK * 4 {
                    next_tick = now;
                }
                // 落后 1~4 拍：不 sleep，直接进入下一轮（追赶）
            }
        })
        .expect("spawn viz-writer")
}
