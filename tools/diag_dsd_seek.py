#!/usr/bin/env python3
"""DSD 软解（ffmpeg）路径 seek 诊断。

启动独立后端 -> set_dsd_mode pcm -> 低音量播放 DSD ->
测播放中 position 速率 -> seek 60s -> 测 seek 后速率 / 是否跳歌或静音。
"""
import json
import os
import socket
import subprocess
import sys
import time

SOCK = "/tmp/xiatiao-diag-dsd.sock"
BIN = "audio_backend_rs/target/release/xiatiao-audio-backend"
DSF = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)


def main():
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
        print("后端未启动（socket 未出现）")
        proc.kill()
        sys.exit(1)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.settimeout(0.1)

    def send(obj):
        s.sendall((json.dumps(obj) + "\n").encode())

    buf = b""

    def drain(dur):
        """收集 dur 秒内的事件，返回 [(t, ev)]。"""
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

    def positions(evs):
        return [(t, e["sec"]) for t, e in evs if e.get("event") == "position"]

    def rate(evs):
        """由 position 事件估算推进速率（秒/秒）。"""
        ps = positions(evs)
        if len(ps) < 2:
            return None, ps
        (t0, p0), (t1, p1) = ps[0], ps[-1]
        dt = t1 - t0
        if dt <= 0:
            return None, ps
        return (p1 - p0) / dt, ps

    print("=== 初始化 ===")
    send({"cmd": "set_volume", "value": 0.02})
    drain(0.3)
    send({"cmd": "set_dsd_mode", "mode": "pcm"})
    drain(0.3)

    print("=== 播放 DSD（pcm 软解）===")
    send({"cmd": "play", "path": DSF})
    play_evs = drain(6.0)
    r, ps = rate(play_evs)
    print(f"播放中速率: {r}" if r else "播放中 position 事件不足")
    if ps:
        print(f"  position 首/尾: {ps[0][1]:.2f} -> {ps[-1][1]:.2f}")
    states = [e.get("state") for _, e in play_evs if e.get("event") == "state"]
    print(f"  state 变化: {states}")

    print("=== seek 到 60s ===")
    send({"cmd": "seek", "seconds": 60.0})
    seek_evs = drain(6.0)
    r2, ps2 = rate(seek_evs)
    print(f"seek 后速率: {r2}" if r2 else "seek 后 position 事件不足")
    if ps2:
        print(f"  position 首/尾: {ps2[0][1]:.2f} -> {ps2[-1][1]:.2f}")
    states2 = [e.get("state") for _, e in seek_evs if e.get("event") == "state"]
    print(f"  state 变化: {states2}")
    eof = [e for _, e in seek_evs if e.get("event") == "end_of_stream"]
    print(f"  end_of_stream: {'有（疑似跳下一首）' if eof else '无'}")

    send({"cmd": "shutdown"})
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read().decode(errors="replace")
    print("\n=== 后端 stderr ===")
    print(err)


if __name__ == "__main__":
    main()
