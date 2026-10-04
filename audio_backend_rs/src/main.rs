//! xiatiao 音频后端（Rust，独立进程）。
//!
//! 通信：Unix domain socket + JSON Lines。
//! 请求 {"cmd":"..."}\n / 事件 {"event":"..."}\n
//!
//! 本进程支持主动推送（position / state / end_of_stream），
//! 因此写端在多个线程间共享。

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

mod bbe;
mod camilla_engine;
mod oversample;
mod tube;
mod dsp;
mod engine;
mod shared;
mod decode;
mod decode_ffmpeg;
mod deps;
mod output;
mod output_alsa;
mod dsd;
mod reverb;
mod ir_resample;
mod logging;
mod ipc;
mod protocol;
mod viz;

/// 可跨线程写入的 socket 写端。
type SharedWriter = Arc<Mutex<UnixStream>>;

fn main() {
    // 父进程（GUI 应用）退出时，让本进程自动收到 SIGTERM —— 避免应用
    // 非正常退出（崩溃 / 被 kill）时后端残留成孤儿进程。
    #[cfg(target_os = "linux")]
    {
        use nix::sys::prctl;
        use nix::sys::signal::Signal;
        let _ = prctl::set_pdeathsig(Signal::SIGTERM);
    }

    // 启动时探测外部依赖（便于打包后诊断）
    deps::probe_all();

    // 默认 socket 路径必须与 Python 侧 core/rust_backend.py::DEFAULT_SOCKET 一致。
    // 跨语言无法共享常量，改动任一侧时务必同步另一侧（可用
    // XIATIAO_BACKEND_SOCKET 环境变量覆盖）。
    let sock_path = std::env::var("XIATIAO_BACKEND_SOCKET")
        .unwrap_or_else(|_| "/tmp/xiatiao-audio-backend.sock".to_string());

    let _ = std::fs::remove_file(&sock_path);

    let listener = match UnixListener::bind(&sock_path) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("bind {sock_path} failed: {e}");
            std::process::exit(1);
        }
    };
    eprintln!("xiatiao-audio-backend listening on {sock_path}");

    let engine = Arc::new(Mutex::new(engine::Engine::new()));

    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let eng = Arc::clone(&engine);
                thread::spawn(move || handle_client(s, eng));
            }
            Err(e) => eprintln!("accept failed: {e}"),
        }
    }
}

fn handle_client(stream: UnixStream, engine: Arc<Mutex<engine::Engine>>) {
    let reader = BufReader::new(match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    });
    let writer: SharedWriter = Arc::new(Mutex::new(stream));

    // 推送线程：定期把 position / state / eof 主动发给客户端
    let push_alive = Arc::new(AtomicBool::new(true));
    {
        let eng = Arc::clone(&engine);
        let w = Arc::clone(&writer);
        let alive = Arc::clone(&push_alive);
        thread::spawn(move || {
            let mut last_state = String::new();
            let mut last_pos = -1.0f64;
            let mut last_dur = -1.0f64;
            let mut eof_sent = false;
            let mut last_gen = u64::MAX;
            while alive.load(Ordering::SeqCst) {
                thread::sleep(Duration::from_millis(100));
                let (state, pos, is_eof, dur, gen) = match eng.lock() {
                    Ok(e) => (e.state_str().to_string(), e.position(), e.is_eof(), e.duration_secs(), e.play_gen()),
                    Err(_) => break,
                };
                // 代次变化（切歌/切音质）：重置位置去重基准，让新代次的
                // 首个位置（即使与旧值接近）也能发出。
                if gen != last_gen {
                    last_gen = gen;
                    last_pos = -1.0;
                }
                if dur > 0.0 && (dur - last_dur).abs() >= 0.5 {
                    last_dur = dur;
                    if send_event(&w, &protocol::Event::Duration { sec: dur }).is_err() {
                        break;
                    }
                }
                if state != last_state {
                    last_state = state.clone();
                    if send_event(&w, &protocol::Event::State { state }) .is_err() {
                        break;
                    }
                }
                // 位置：只在播放中且变化时发
                if (pos - last_pos).abs() >= 0.1 {
                    last_pos = pos;
                    if send_event(&w, &protocol::Event::Position { sec: pos, gen }).is_err() {
                        break;
                    }
                }
                // 解码线程报告的错误 → 发给客户端（UI 提示）
                if let Some(err) = eng.lock().ok().and_then(|e| e.take_error()) {
                    if send_event(&w, &protocol::Event::Error { message: err }).is_err() {
                        break;
                    }
                }
                // 解码线程探测到的音频技术信息 → 发给客户端（显示格式/采样率/位深/码率）
                if let Some(info) = eng.lock().ok().and_then(|e| e.take_audio_info()) {
                    if send_event(&w, &protocol::Event::AudioInfo { info }).is_err() {
                        break;
                    }
                }
                if is_eof && !eof_sent {
                    eof_sent = true;
                    if send_event(&w, &protocol::Event::EndOfStream).is_err() {
                        break;
                    }
                }
                if !is_eof {
                    eof_sent = false;
                }
            }
        });
    }

    for line in reader.lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: protocol::Request = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                let _ = send_event(
                    &writer,
                    &protocol::Event::Error { message: format!("bad json: {e}") },
                );
                continue;
            }
        };
        eprintln!("[IPC] recv: {}", line.trim());
        let resp = engine
            .lock()
            .map(|mut e| e.handle(req))
            .unwrap_or_else(|_| protocol::Event::error("engine lock poisoned"));
        eprintln!("[IPC] resp: {}", serde_json::to_string(&resp).unwrap_or_default());
        let _ = send_event(&writer, &resp);
    }

    push_alive.store(false, Ordering::SeqCst);
}

/// 安全地发送一条事件。
fn send_event(w: &SharedWriter, evt: &protocol::Event) -> std::io::Result<()> {
    let mut guard = match w.lock() {
        Ok(g) => g,
        Err(_) => return Ok(()),
    };
    let line = serde_json::to_string(evt).unwrap_or_else(|_| "{}".to_string());
    guard.write_all(line.as_bytes())?;
    guard.write_all(b"\n")?;
    guard.flush()
}
