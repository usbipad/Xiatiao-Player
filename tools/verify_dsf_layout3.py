#!/usr/bin/env python3
"""决定性判定 DSF 布局：多级低通重建后比较左右声道相关性。

DSD 的 1-bit 流经 4 级级联滑动平均（等效多阶 FIR 低通，截止 ~20kHz）
重建出音乐波形。立体声左右声道同源、高度相关（音乐），
错误布局会破坏声道分离 → 相关性显著下降或异常。
"""
import struct

P = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)


def parse(path):
    f = open(path, "rb")
    f.read(28)
    assert f.read(4) == b"fmt "
    fs = struct.unpack("<Q", f.read(8))[0]
    body = f.read(fs - 12)
    ch = struct.unpack("<I", body[12:16])[0]
    bs = struct.unpack("<I", body[32:36])[0]
    assert f.read(4) == b"data"
    f.read(8)
    return f, ch, bs


def lowpass_stages(bits, stages=4, win=32):
    """bits: 0/1 序列。级联滑动平均。"""
    import numpy as np
    x = np.array(bits, dtype=np.float32)
    for _ in range(stages):
        # 移动平均（valid，长度缩短，但两声道同处理，可比）
        k = np.ones(win, dtype=np.float32) / win
        x = np.convolve(x, k, mode="valid")
    return x


def bits_of(ch_bytes):
    import numpy as np
    arr = np.frombuffer(bytes(ch_bytes), dtype=np.uint8)
    # 每字节 MSB→LSB 展开为 8 bit
    bits = np.unpackbits(arr)  # unpackbits 默认 big-endian（MSB first）
    return bits


def analyze(ch0, ch1, tag):
    import numpy as np
    b0 = bits_of(ch0)
    b1 = bits_of(ch1)
    p0 = lowpass_stages(b0)
    p1 = lowpass_stages(b1)
    n = min(len(p0), len(p1))
    p0, p1 = p0[:n], p1[:n]
    c = float(np.corrcoef(p0, p1)[0, 1])
    # 频谱重心（粗糙）：能量是否集中在低频（音乐）
    print(f"[{tag}] 左右相关性={c:.4f}  样本数={n}")
    return c


def main():
    f, ch, bs = parse(P)
    nblocks = 300
    group = bs * ch
    raw = f.read(group * nblocks)
    print(f"ch={ch} block_size={bs} 块组={len(raw)//group}")

    a0 = bytearray(); a1 = bytearray()
    for b in range(nblocks):
        base = b * group
        a0 += raw[base:base + bs]
        a1 += raw[base + bs:base + 2 * bs]
    cA = analyze(a0, a1, "块交错")

    cB = analyze(raw[0::2], raw[1::2], "字节交错")

    print()
    print(f"块交错相关性={cA:.4f}  字节交错相关性={cB:.4f}")
    print("立体声音乐左右强相关（通常>0.3）；相关性高者为正确布局。")


if __name__ == "__main__":
    main()
