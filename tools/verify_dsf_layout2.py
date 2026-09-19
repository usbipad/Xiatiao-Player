#!/usr/bin/env python3
"""严谨判定 DSF 布局：块边界处波形是否连续。

对 1-bit DSD，直接低通后看「块边界样本跳变」——
- 正确布局：相邻块接缝处不应有异常跳变；
- 错误布局：每个块接缝处会引入跳变（周期性、每 block_size 一次）。
用「边界处相邻样本差」对比「块内平均样本差」。
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
    fmt_size = struct.unpack("<Q", f.read(8))[0]
    body = f.read(fmt_size - 12)
    ch = struct.unpack("<I", body[12:16])[0]
    bs = struct.unpack("<I", body[32:36])[0]
    assert f.read(4) == b"data"
    f.read(8)
    return f, ch, bs


def pcm(ch_bytes, win=512):
    """更强低通（512 bit 窗 ≈ 5.5kHz 截止 @2.8MHz），突出音乐成分。"""
    from collections import deque
    out = []
    buf = deque()
    acc = 0.0
    for byte in ch_bytes:
        for b in range(7, -1, -1):
            v = 1.0 if (byte >> b) & 1 else -1.0
            buf.append(v)
            acc += v
            if len(buf) > win:
                acc -= buf.popleft()
            out.append(acc / len(buf))
    return out


def seam_jump(sig, bs_bits):
    """返回 (接缝处 |Δ| 平均, 块内 |Δ| 平均)。"""
    seams = []
    inner = []
    n = len(sig)
    k = 1
    while k * bs_bits < n:
        i = k * bs_bits
        seams.append(abs(sig[i] - sig[i - 1]))
        k += 1
    for i in range(1, min(n, 50 * bs_bits)):
        if i % bs_bits != 0:
            inner.append(abs(sig[i] - sig[i - 1]))
    sm = sum(seams) / len(seams) if seams else 0
    im = sum(inner) / len(inner) if inner else 0
    return sm, im


def main():
    f, ch, bs = parse(P)
    nblocks = 400
    group = bs * ch
    raw = f.read(group * nblocks)
    print(f"ch={ch} block_size={bs} 读入 {len(raw)//group} 块组")

    # 布局 A：块交错（当前实现假设）
    a0 = bytearray(); a1 = bytearray()
    for b in range(nblocks):
        base = b * group
        a0 += raw[base:base + bs]
        a1 += raw[base + bs:base + 2 * bs]
    pa0 = pcm(a0)
    s0, i0 = seam_jump(pa0, bs * 8)
    print(f"[块交错] 接缝|Δ|={s0:.6f}  块内|Δ|={i0:.6f}  比值={s0/i0:.3f}")

    # 布局 B：字节交错（对照）
    b0 = raw[0::2]
    pb0 = pcm(b0)
    s1, i1 = seam_jump(pb0, bs * 8)
    print(f"[字节交错] 接缝|Δ|={s1:.6f}  块内|Δ|={i1:.6f}  比值={s1/i1:.3f}")

    print()
    print("判定：接缝/块内 比值接近 1 → 布局正确（无接缝跳变）；")
    print("      比值 >> 1 → 布局错误（接缝处周期性跳变）。")


if __name__ == "__main__":
    main()
