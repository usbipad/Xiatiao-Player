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
mod output;
mod reverb;
mod ipc;
mod protocol;
mod viz;

/// 可跨线程写入的 socket 写端。
type SharedWriter = Arc<Mutex<UnixStream>>;

fn main() {
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
            while alive.load(Ordering::SeqCst) {
                thread::sleep(Duration::from_millis(100));
                let (state, pos, is_eof, dur) = match eng.lock() {
                    Ok(e) => (e.state_str().to_string(), e.position(), e.is_eof(), e.duration_secs()),
                    Err(_) => break,
                };
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
                    if send_event(&w, &protocol::Event::Position { sec: pos }).is_err() {
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
