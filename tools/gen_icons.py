"""从根目录 image.png 生成 data/icons/ 下各尺寸应用图标。

若源图已是正方形：直接缩放到各尺寸（保留原有透明边角，适合已做好的圆角图标）。
若源图非正方形：裁剪到非透明主体、居中为正方形后再缩放。

用法：python3 tools/gen_icons.py
依赖：GdkPixbuf（PyGObject 自带）。
"""
import os
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "image.png")
OUT_DIR = os.path.join(ROOT, "data", "icons")
SIZES = [16, 24, 32, 48, 64, 128, 256]
#: 主体在正方形中占的比例（0.9 = 四周各留 5% 边距）
SUBJECT_RATIO = 0.90


def _bbox_alpha(pb):
    """返回非透明像素的边界框 (minx, miny, maxx, maxy)；全透明返回 None。"""
    w, h = pb.get_width(), pb.get_height()
    if not pb.get_has_alpha():
        return (0, 0, w - 1, h - 1)
    stride = pb.get_rowstride()
    n = pb.get_n_channels()
    data = bytes(pb.get_pixels())
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        base = y * stride
        for x in range(w):
            if data[base + x * n + 3] > 8:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return None
    return (minx, miny, maxx, maxy)


def _crop_to_subject(pb):
    """裁到非透明主体，并扩成正方形（主体占 SUBJECT_RATIO）。"""
    box = _bbox_alpha(pb)
    if box is None:
        return pb
    minx, miny, maxx, maxy = box
    sub_w = maxx - minx + 1
    sub_h = maxy - miny + 1
    # 以主体为中心的裁剪正方形，边长 = 主体最长边 / SUBJECT_RATIO
    side = int(max(sub_w, sub_h) / SUBJECT_RATIO)
    cx = (minx + maxx) // 2
    cy = (miny + maxy) // 2
    src_w, src_h = pb.get_width(), pb.get_height()
    # 裁剪区域（可超出原图 → 用透明填充）
    out = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, side, side)
    out.fill(0x00000000)
    off_x = (side - src_w) // 2 if side > src_w else 0
    off_y = (side - src_h) // 2 if side > src_h else 0
    # 把原图贴到 out 的合适位置：目标是让 (cx, cy) 落在 out 中心
    dst_x = side // 2 - cx
    dst_y = side // 2 - cy
    if dst_x < 0 or dst_y < 0:
        # side 小于原图：需从原图裁剪
        sx = cx - side // 2
        sy = cy - side // 2
        sx = max(0, min(sx, src_w - side)) if side <= src_w else 0
        sy = max(0, min(sy, src_h - side)) if side <= src_h else 0
        return pb.new_subpixbuf(sx, sy, min(side, src_w), min(side, src_h))
    pb.composite(
        out, dst_x, dst_y, src_w, src_h, 0, 0, 1.0, 1.0,
        GdkPixbuf.InterpType.BILINEAR, 255,
    )
    return out


def main() -> int:
    if not os.path.isfile(SRC):
        print(f"找不到源图：{SRC}", file=sys.stderr)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    src = GdkPixbuf.Pixbuf.new_from_file(SRC)
    print(f"源图：{src.get_width()}x{src.get_height()}")

    # 关键：若源图已是正方形，直接缩放（不做裁剪/合成）。
    # 裁剪+合成会破坏圆角图标的透明边角（产生凸块）。
    # 仅当源图非正方形时，才走裁剪居中逻辑。
    if src.get_width() == src.get_height():
        print("源图为正方形：直接缩放（不裁剪）")
        base = src
    else:
        base = _crop_to_subject(src)
        print(f"非正方形源图，裁剪后：{base.get_width()}x{base.get_height()}")

    for size in SIZES:
        scaled = base.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        if not scaled.get_has_alpha():
            scaled = scaled.add_alpha(False, 0, 0, 0)
        path = os.path.join(OUT_DIR, f"xiatiao-{size}.png")
        scaled.savev(path, "png", [], [])
        print(f"  -> data/icons/xiatiao-{size}.png ({scaled.get_width()}x{scaled.get_height()})")

    # 主图 xiatiao.png = 256 尺寸。
    import shutil
    shutil.copy(os.path.join(OUT_DIR, "xiatiao-256.png"),
                os.path.join(OUT_DIR, "xiatiao.png"))
    print("  -> data/icons/xiatiao.png (256)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
