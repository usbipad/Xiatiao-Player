#!/usr/bin/env python3
"""DSD 软解（ffmpeg）+ Camilla 音效路径 seek 诊断。

验证：开音效 seek 是否加速 / 记忆音频 / 跳歌。
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.getcwd())
from core import camilla  # noqa: E402

SOCK = "/tmp/xiatiao-diag-dsd-cam.sock"
BIN = "audio_backend_rs/target/release/xiatiao-audio-backend"
DSF = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)

# 开启一个明显的 EQ（PEQ + 低音提升），保证 Camilla pipeline 非空。
PARAMS = {
    "enabled": True,
    "eq_enabled": True,
    "eq_gains": [0, 0, 0, 0, 0, 3.0, 0, 0, 0, 0],
    "peq_enabled": True,
    "peq_bands": [{"kind": "pk", "freq": 1000.0, "gain": 4.0, "q": 1.5}],
    "bass_enabled": True,
    "bass_freq": 100.0,
    "bass_gain_db": 3.0,
}


def main():
    if not os.path.exists(DSF):
        print(f"测试文件不存在: {DSF}")
        sys.exit(1)
    if os.path.exists(SOCK):
        os.remove(SOCK)

    # 生成 Camilla YAML（352800 Hz，2ch）
    cfg = camilla.build_config(PARAMS, samplerate=352800, channels=2)
    yaml_str = camilla.to_yaml(cfg)
    print("=== Camilla YAML 前 20 行 ===")
    print("\n".join(yaml_str.splitlines()[:20]))

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

    def rate(evs):
        ps = [(t, e["sec"]) for t, e in evs if e.get("event") == "position"]
        if len(ps) < 2:
            return None, ps
        (t0, p0), (t1, p1) = ps[0], ps[-1]
        dt = t1 - t0
        return ((p1 - p0) / dt if dt > 0 else None), ps

    print("\n=== 初始化 ===")
    send({"cmd": "set_volume", "value": 0.02})
    drain(0.3)
    send({"cmd": "set_dsd_mode", "mode": "pcm"})
    drain(0.3)
    send({"cmd": "set_dsp", "params": PARAMS})
    drain(0.3)
    send({"cmd": "set_camilla_yaml", "yaml": yaml_str})
    drain(0.3)
    send({"cmd": "set_engine", "camilla": True})
    drain(0.3)

    print("=== 播放 DSD（pcm + Camilla）===")
    send({"cmd": "play", "path": DSF})
    evs = drain(6.0)
    r, ps = rate(evs)
    print(f"播放中速率: {r}" if r else "播放中 position 不足")
    if ps:
        print(f"  position: {ps[0][1]:.2f} -> {ps[-1][1]:.2f}")

    print("=== seek 到 60s（Camilla 开）===")
    send({"cmd": "seek", "seconds": 60.0})
    evs2 = drain(6.0)
    r2, ps2 = rate(evs2)
    print(f"seek 后速率: {r2}" if r2 else "seek 后 position 不足")
    if ps2:
        print(f"  position: {ps2[0][1]:.2f} -> {ps2[-1][1]:.2f}")
    eof = [e for _, e in evs2 if e.get("event") == "end_of_stream"]
    print(f"  end_of_stream: {'有（跳下一首！）' if eof else '无'}")

    send({"cmd": "shutdown"})
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read().decode(errors="replace")
    print("\n=== 后端 stderr（末尾）===")
    print("\n".join(err.splitlines()[-30:]))


if __name__ == "__main__":
    main()
