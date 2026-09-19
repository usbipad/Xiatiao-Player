#!/usr/bin/env python3
"""DSD 软解 seek 边界诊断：seek 到接近文件末尾，验证不误判 EOF 跳歌。

文件时长约 265.9s。测 seek 到 263s（剩余 ~2.9s），应正常播完再 EOF。
"""
import json
import os
import socket
import subprocess
import sys
import time

SOCK = "/tmp/xiatiao-diag-dsd-edge.sock"
BIN = "audio_backend_rs/target/release/xiatiao-audio-backend"
DSF = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)


def main():
    seek_to = float(sys.argv[1]) if len(sys.argv) > 1 else 263.0
    if not os.path.exists(DSF):
        print(f"测试文件不存在: {DSF}")
        sys.exit(1)
    if os.path.exists(SOCK):
        os.remove(SOCK)

    env = dict(os.environ)
    env["XIATIAO_BACKEND_SOCKET"] = SOCK
    proc = subprocess.Popen(
        [BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    for _ in range(50):
        if os.path.exists(SOCK):
            break
        time.sleep(0.1)
    if not os.path.exists(SOCK):
        print("后端未启动")
        proc.kill()
        sys.exit(1)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.settimeout(0.1)

    def send(obj):
        s.sendall((json.dumps(obj) + "\n").encode())

    buf = b""

    def drain(dur):
        nonlocal buf
        evs = []
        t0 = time.time()
        while time.time() - t0 < dur:
            try:
                data = s.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            evs.append((time.time(), json.loads(line)))
                        except json.JSONDecodeError:
                            pass
            except socket.timeout:
                pass
        return evs

    send({"cmd": "set_volume", "value": 0.02})
    drain(0.3)
    send({"cmd": "set_dsd_mode", "mode": "pcm"})
    drain(0.3)

    print(f"=== 播放 DSD 并 seek 到 {seek_to}s ===")
    send({"cmd": "play", "path": DSF})
    drain(2.0)
    send({"cmd": "seek", "seconds": seek_to})
    evs = drain(8.0)
    ps = [(t, e["sec"]) for t, e in evs if e.get("event") == "position"]
    eof = [(t, e) for t, e in evs if e.get("event") == "end_of_stream"]
    if ps:
        print(f"  position: {ps[0][1]:.2f} -> {ps[-1][1]:.2f}")
        # 判定是否 seek 后立即 EOF（异常）还是正常推进
        first = ps[0][1]
        print(f"  seek 后首位置: {first:.2f}（应接近 {seek_to}）")
    print(f"  end_of_stream（8s 内）: {'有' if eof else '无'}")
    if eof:
        print("  → 若首位置≈目标且无异常提前，'有' 属正常（文件确实播完）")

    # 再等一段，看是否自然播完
    evs2 = drain(4.0)
    eof2 = [e for _, e in evs2 if e.get("event") == "end_of_stream"]
    print(f"  继续 4s 内 end_of_stream: {'有' if eof2 else '无'}")

    send({"cmd": "shutdown"})
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read().decode(errors="replace")
    print("\n=== 后端 stderr（末尾 15 行）===")
    print("\n".join(err.splitlines()[-15:]))


if __name__ == "__main__":
    main()
