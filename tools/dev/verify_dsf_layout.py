#!/usr/bin/env python3
"""验证 DSF 数据区是「每声道 block_size 字节块交错」布局。

思路：DSD → PCM 用滑动平均（低通）。若解交错正确，左右声道应是
高度相关的音乐信号；若按字节交错误处理，会在块边界出现周期性跳变，
左右相关性显著下降。
"""
import struct
import sys

P = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)


def parse(path):
    f = open(path, "rb")
    f.read(28)  # DSD chunk
    assert f.read(4) == b"fmt "
    fmt_size = struct.unpack("<Q", f.read(8))[0]
    body = f.read(fmt_size - 12)
    ch = struct.unpack("<I", body[12:16])[0]
    rate = struct.unpack("<I", body[16:20])[0]
    bs = struct.unpack("<I", body[32:36])[0]
    assert f.read(4) == b"data"
    data_size = struct.unpack("<Q", f.read(8))[0]
    return f, ch, rate, bs, data_size


def dsd_to_pcm(ch_bytes, win=64):
    """DSD 字节（MSB-first）→ 浮点：1→+1，0→-1，滑动平均低通。"""
    samples = []
    acc = 0.0
    from collections import deque
    buf = deque()
    for byte in ch_bytes:
        for b in range(7, -1, -1):
            bit = (byte >> b) & 1
            v = 1.0 if bit else -1.0
            buf.append(v)
            acc += v
            if len(buf) > win:
                acc -= buf.popleft()
            samples.append(acc / len(buf))
    return samples


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((x - mb) ** 2 for x in b) ** 0.5
    return num / (da * db) if da * db else 0.0


def main():
    f, ch, rate, bs, data_size = parse(P)
    print(f"ch={ch} dsd_rate={rate} block_size={bs} data_size={data_size}")
    # 只取前 ~200 个块组做分析（省时）。
    nblocks = 200
    group = bs * ch
    raw = f.read(group * nblocks)
    print(f"读入 {len(raw)} 字节 ≈ {len(raw)//group} 个块组")

    # 正确布局：块交错 → ch0 = [b0 blk0][b1 blk0]... ; ch1 = [b0 blk1][b1 blk1]...
    ch0 = bytearray()
    ch1 = bytearray()
    for b in range(nblocks):
        base = b * group
        ch0 += raw[base:base + bs]
        ch1 += raw[base + bs:base + 2 * bs]

    # 错误布局对照：按字节交错。
    w0 = raw[0::2]
    w1 = raw[1::2]

    p0 = dsd_to_pcm(ch0)
    p1 = dsd_to_pcm(ch1)
    print(f"[正确:块交错] 左右声道相关性 = {corr(p0, p1):.4f}")

    q0 = dsd_to_pcm(w0)
    q1 = dsd_to_pcm(w1)
    print(f"[错误:字节交错] 左右声道相关性 = {corr(q0, q1):.4f}")

    # 块边界检测：正确解交错下，相邻块 rms 应平滑；错误下会跳变。
    def block_rms(sig):
        step = bs * 8
        vals = []
        for i in range(0, len(sig) - step, step):
            seg = sig[i:i + step]
            vals.append((sum(x * x for x in seg) / len(seg)) ** 0.5)
        return vals

    r0 = block_rms(p0)
    print(f"[正确] ch0 各块 RMS 前 8 个: {[round(x,4) for x in r0[:8]]}")
    print(f"       相邻块 RMS 最大跳变: {max(abs(r0[i+1]-r0[i]) for i in range(len(r0)-1)):.4f}")


if __name__ == "__main__":
    main()
