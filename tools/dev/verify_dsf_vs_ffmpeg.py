#!/usr/bin/env python3
"""用 ffmpeg 解码的参考 PCM 判定 DSF 布局。

ffmpeg 的 DSF 解码是权威参考。把两种布局解出的 L/R 分别与
ffmpeg 的 L/R 做相关：匹配者 = 正确布局。
"""
import struct
import numpy as np

DSF = (
    "/mnt/D1333F9E1DE59718/音乐/"
    "DANA WINNER - Unforgettable (2001) [SACD]/"
    "01 - Dana Winner - Moonlight Shadow.dsf"
)
REF = "/tmp/ref.pcm"


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


def bits_of(ch_bytes):
    arr = np.frombuffer(bytes(ch_bytes), dtype=np.uint8)
    return np.unpackbits(arr).astype(np.float32)


def lowpass(bits, stages=6, win=16):
    x = bits
    for _ in range(stages):
        k = np.ones(win, dtype=np.float32) / win
        x = np.convolve(x, k, mode="valid")
    return x


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n].astype(np.float64), b[:n].astype(np.float64)
    a -= a.mean(); b -= b.mean()
    d = (np.sqrt((a*a).sum()) * np.sqrt((b*b).sum()))
    return float((a*b).sum() / d) if d else 0.0


def main():
    f, ch, bs = parse(DSF)
    nblocks = 300
    group = bs * ch
    raw = f.read(group * nblocks)

    # 参考 PCM（ffmpeg 输出 352800Hz 立体声 f32 交错）
    ref = np.fromfile(REF, dtype=np.float32)
    refL = ref[0::2]
    refR = ref[1::2]
    print(f"参考 PCM 帧数={len(refL)} (@352800Hz, {len(refL)/352800:.2f}s)")

    # DSF 每声道总 bit 数（前 nblocks 块）
    bits_per_ch = nblocks * bs * 8
    ref_n = int(bits_per_ch * 352800 / 2822400)  # DSD→PCM 抽样比 8 (DSD64: 2822400/352800=8)
    refL = refL[:ref_n]
    refR = refR[:ref_n]

    # 布局 A：块交错
    a0 = bytearray(); a1 = bytearray()
    for b in range(nblocks):
        base = b * group
        a0 += raw[base:base + bs]
        a1 += raw[base + bs:base + 2 * bs]
    pa0 = lowpass(bits_of(a0))
    pa1 = lowpass(bits_of(a1))

    # 布局 B：字节交错
    pb0 = lowpass(bits_of(raw[0::2]))
    pb1 = lowpass(bits_of(raw[1::2]))

    # 重采样到参考长度（简单抽取，比较低频结构）
    def match(sig, refL):
        # sig 每 8 个点抽取 1 个 ≈ 352800Hz
        ds = sig[::8]
        n = min(len(ds), len(refL))
        return corr(ds[:n], refL[:n])

    print()
    print("与 ffmpeg 参考 L 的相关性：")
    print(f"  [块交错] ch0 vs refL = {match(pa0, refL):.4f}")
    print(f"  [块交错] ch1 vs refL = {match(pa1, refL):.4f}")
    print(f"  [字节交错] ch0 vs refL = {match(pb0, refL):.4f}")
    print(f"  [字节交错] ch1 vs refL = {match(pb1, refL):.4f}")
    print()
    print("最高者对应的布局 + 声道映射即正确解。")


if __name__ == "__main__":
    main()
