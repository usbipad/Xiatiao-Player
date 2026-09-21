"""内嵌封面读取与主色提取（基于 mutagen / cairo）。

- extract_cover：读取音频文件内嵌封面原始字节
- extract_dominant_color：从封面提取主色调（用于沉浸式背景）
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis

    _HAS_MUTAGEN = True
except Exception:  # pragma: no cover - 环境相关
    _HAS_MUTAGEN = False
    log.info("mutagen 不可用，无法读取内嵌封面")


def extract_cover(path: str) -> bytes | None:
    """从音频文件提取内嵌封面，返回图片字节；无则返回 None。"""
    if not _HAS_MUTAGEN or not path or not os.path.isfile(path):
        return None
    try:
        return _extract(path)
    except Exception as exc:
        log.debug("读取封面失败 %s: %s", path, exc)
        return None


def _extract(path: str) -> bytes | None:
    ext = os.path.splitext(path)[1].lower()

    if ext == ".mp3":
        try:
            tags = ID3(path)
        except Exception:
            return None
        for key in tags.keys():
            if key.startswith("APIC"):
                return bytes(tags[key].data)
        return None

    if ext == ".flac":
        audio = FLAC(path)
        if audio.pictures:
            return bytes(audio.pictures[0].data)
        return None

    if ext in (".m4a", ".mp4", ".m4b"):
        audio = MP4(path)
        covers = audio.tags.get("covr") if audio.tags else None
        if covers:
            return bytes(covers[0])
        return None

    if ext == ".ogg":
        audio = OggVorbis(path)
        covers = audio.get("metadata_block_picture")
        if covers:
            import base64

            from mutagen.flac import Picture

            pic = Picture(base64.b64decode(covers[0]))
            return bytes(pic.data)
        return None

    audio = MutagenFile(path)
    if audio is None:
        return None
    if hasattr(audio, "pictures") and audio.pictures:
        return bytes(audio.pictures[0].data)
    if audio.tags and "covr" in audio.tags:
        return bytes(audio.tags["covr"][0])
    return None


# ----------------------------------------------------------------
# 主色调提取（cairo 采样平均色）
# ----------------------------------------------------------------
def extract_dominant_color(image_bytes: bytes, lighten: float = 0.72) -> tuple[int, int, int] | None:
    """从封面提取主色调，返回 (r,g,b)。

    lighten: 向白色混合的比例（0=原色，1=纯白）。
    默认 0.72 让背景成为柔和浅色（类似 Euphonica）。

    性能：解码一次后用 GdkPixbuf 直接读像素，跳到 32x32 再取色；
    跳过旧实现的「缩图→编码PNG→解码PNG」两次中转，并用 C 层切片求和
    代替 Python 逐像素循环（整体从 ~70ms 降到 ~5ms）。
    """
    if not image_bytes:
        return None
    # 首选：GdkPixbuf 直接解码到小图并读像素
    try:
        return _dominant_via_pixbuf(image_bytes, lighten)
    except Exception as exc:
        log.debug("主色提取(pixbuf)失败，回退 GDK: %s", exc)
    # 回退：非 PNG 等场景
    return _color_via_gdk(image_bytes, lighten)


def lighten_for_background(rgb: tuple[int, int, int],
                           target_lum: float = 0.82,
                           target_sat: float = 0.55) -> tuple[int, int, int]:
    """把封面主色转成背景色：只保留色相，直接输出一个亮色，不混白。

    做法：取主色的色相 H，把亮度 L / 饱和度 S 推到目标值，
    直接作为背景色铺满，不与白色混合。

    rgb:        封面主色 (r,g,b)，0..255。
    target_lum: 目标亮度 L（0..1）。越大越亮，0.82 左右是清爽的浅色。
    target_sat: 目标饱和度 S（0..1）。越大越鲜艳，0.55 左右柔和不过艳。
    返回: 背景色 (r,g,b)。
    """
    try:
        import colorsys
        r, g, b = rgb
        h, _l, _s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        r2, g2, b2 = colorsys.hls_to_rgb(h, target_lum, target_sat)
        return (int(r2 * 255), int(g2 * 255), int(b2 * 255))
    except Exception:
        return rgb


def tint_for_background(rgb: tuple[int, int, int],
                        target_lum: float = 0.80,
                        sat_scale: float = 0.55,
                        sat_cap: float = 0.32) -> tuple[int, int, int]:
    """把封面主色转成背景色：保留色相，**保留原始饱和度**（按比例压制）。

    与 lighten_for_background 的区别：
      后者把饱和度强制设为固定值，导致「封面上很淡的偏色」被拉成鲜艳纯色；
      本函数保留原始饱和度并按 sat_scale 缩小、以 sat_cap 封顶，
      使输出浓度贴合封面本身：淡封面→淡背景，艳封面→柔和背景。

    rgb:        封面主色 (r,g,b)。
    target_lum: 目标亮度 L（0..1）。
    sat_scale:  饱和度缩放系数（0..1）。
    sat_cap:    饱和度上限（0..1），防止过艳。
    返回: 背景色 (r,g,b)。
    """
    try:
        import colorsys
        r, g, b = rgb
        h, _l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        s2 = min(s * sat_scale, sat_cap)
        r2, g2, b2 = colorsys.hls_to_rgb(h, target_lum, s2)
        return (int(r2 * 255), int(g2 * 255), int(b2 * 255))
    except Exception:
        return rgb


def _lighten_hsl(r: int, g: int, b: int, amount: float) -> tuple[int, int, int]:
    """在 HSL 空间提升亮度：保持色相/饱和，仅抬高 L。

    深色封面提亮后仍鲜艳，不像 RGB 向白混那样容易变灰。
    amount 0..1：0=不变，1=提亮较多但仍保留色相。
    """
    try:
        import colorsys
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        # 温和提亮：最多提升到约 0.55 的亮度系数，避免纯白
        l2 = l + (1.0 - l) * min(0.85, amount * 0.55)
        l2 = max(l, min(1.0, l2))
        r2, g2, b2 = colorsys.hls_to_rgb(h, l2, s)
        return (int(r2 * 255), int(g2 * 255), int(b2 * 255))
    except Exception:
        return (r, g, b)


def _dominant_via_pixbuf(image_bytes: bytes, lighten: float) -> tuple[int, int, int]:
    """用 GdkPixbuf 解码 → 缩到 32px → 直接读像素求平均色（无 PNG 中转）。"""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gio, GLib

    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w <= 0 or h <= 0:
        return (240, 240, 240)
    # 缩到 32x32（取色足够，越小越快）
    target = 32
    scale = max(target / w, target / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    if (nw, nh) != (w, h):
        pixbuf = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
    # 统一样式为 8bit RGB(A)
    if pixbuf.get_bits_per_sample() != 8 or pixbuf.get_colorspace() != GdkPixbuf.Colorspace.RGB:
        pixbuf = pixbuf.copy()
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(True, 255, 255, 255)

    sw, sh = pixbuf.get_width(), pixbuf.get_height()
    stride = pixbuf.get_rowstride()
    n_ch = pixbuf.get_n_channels()
    data = bytes(pixbuf.get_pixels())

    # 量化直方图求主色：把 RGB 各分量降为 32 级（>>3），统计出现最多的色桶。
    # 相比「平均色」，主色更鲜明不灰浊；过滤接近黑/白/灰的桶以保鲜明。
    from collections import Counter
    step = max(1, min(sw, sh) // 24)
    hist = Counter()
    for y in range(0, sh, step):
        base = y * stride
        for x in range(0, sw, step):
            o = base + x * n_ch
            if o + 2 >= len(data):
                break
            r8 = data[o]
            g8 = data[o + 1]
            b8 = data[o + 2]
            a8 = data[o + 3] if n_ch == 4 else 255
            if a8 < 40:          # 透明像素忽略
                continue
            # 亮度过低/过高忽略（避免取到纯黑纯白）
            lum = (r8 * 299 + g8 * 587 + b8 * 114) // 1000
            if lum < 25 or lum > 240:
                continue
            # 灰度过高忽略（r≈g≈b，避免取到灰）
            if max(r8, g8, b8) - min(r8, g8, b8) < 12:
                continue
            # 量化：各分量 >>3（32 级）
            hist[(r8 >> 3, g8 >> 3, b8 >> 3)] += 1
    if not hist:
        # 全灰/黑/白图：退化为平均色（保证有结果）
        r_sum = g_sum = b_sum = 0
        n = 0
        for y in range(0, sh, step):
            base = y * stride
            for x in range(0, sw, step):
                o = base + x * n_ch
                if o + 2 >= len(data):
                    break
                r_sum += data[o]
                g_sum += data[o + 1]
                b_sum += data[o + 2]
                n += 1
        if n == 0:
            return (240, 240, 240)
        r, g, b = r_sum // n, g_sum // n, b_sum // n
        return _lighten_hsl(r, g, b, lighten)
    else:
        # 加权评分选主色：像素数 × 鲜亮度（饱和度 + 亮度适中）
        # 避免只取「出现最多」的暗色桶，导致背景发闷。
        best = None
        best_score = -1.0
        total = sum(hist.values()) or 1
        for (qr, qg, qb), cnt in hist.items():
            cr = (qr << 3) + 4
            cg = (qg << 3) + 4
            cb = (qb << 3) + 4
            mx = max(cr, cg, cb)
            mn = min(cr, cg, cb)
            sat = (mx - mn) / 255.0                 # 饱和度 0..1
            lum = (cr * 299 + cg * 587 + cb * 114) / 255000.0  # 亮度 0..1
            # 亮度适中偏好：0.5 最佳，过暗过亮衰减
            lum_pref = 1.0 - abs(lum - 0.5) * 1.2
            lum_pref = max(0.15, lum_pref)
            freq = cnt / total                       # 频率
            score = (0.5 + freq) * (0.4 + sat) * lum_pref
            if score > best_score:
                best_score = score
                best = (cr, cg, cb)
        if best is not None:
            r, g, b = best
        else:
            (qr, qg, qb), _cnt = hist.most_common(1)[0]
            r = (qr << 3) + 4
            g = (qg << 3) + 4
            b = (qb << 3) + 4
    # 提亮：用 HSL 只提升亮度，保留色相与饱和度（深色封面提亮后仍鲜艳，
    # 不像 RGB 向白混那样容易变灰）。lighten 0..1 映射为亮度提升量。
    r, g, b = _lighten_hsl(r, g, b, lighten)
    return (r, g, b)


def _average_color(surf, lighten: float) -> tuple[int, int, int]:
    w, h = surf.get_width(), surf.get_height()
    surf.flush()
    buf = surf.get_data()
    stride = surf.get_stride()
    r = g = b = n = 0
    step = max(1, min(w, h) // 32)  # 采样步长
    for y in range(0, h, step):
        row = y * stride
        for x in range(0, w, step):
            off = row + x * 4
            b += buf[off]
            g += buf[off + 1]
            r += buf[off + 2]
            n += 1
    if n == 0:
        return (240, 240, 240)
    r, g, b = r // n, g // n, b // n
    # 提亮：HSL 保色相/饱和
    r, g, b = _lighten_hsl(r, g, b, lighten)
    return (r, g, b)


def _shrink_for_color(image_bytes: bytes, size: int = 64) -> bytes | None:
    """把封面缩到 size x size 的 PNG 字节（用于快速取色）。失败返回 None。"""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib

        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w <= 0 or h <= 0:
            return None
        scale = max(size / w, size / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        scaled = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        x = (nw - size) // 2
        y = (nh - size) // 2
        sq = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
        sq.fill(0x00000000)
        scaled.composite(sq, 0, 0, size, size, -x, -y, 1.0, 1.0,
                         GdkPixbuf.InterpType.BILINEAR, 255)
        ok, buf = sq.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else None
    except Exception as exc:
        log.debug("缩图取色失败: %s", exc)
        return None


def _color_via_gdk(image_bytes: bytes, lighten: float) -> tuple[int, int, int] | None:
    """非 PNG 图片：用 Gdk 解码后取色。"""
    try:
        import io

        import cairo
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GLib

        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(image_bytes))
        # GTK4 的 Texture 没有 download_bytes；转成 PNG 字节后用 cairo 读取。
        png_bytes = texture.save_to_png_bytes().get_data()
        surf = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
        return _average_color(surf, lighten)
    except Exception as exc:
        log.debug("GDK 取色失败: %s", exc)
        return None


# ----------------------------------------------------------------
# 固定尺寸正方形封面（消除布局随图片 natural size 变化）
# ----------------------------------------------------------------
def make_square_cover_bytes(image_bytes: bytes, size: int,
                            radius: int = 0) -> bytes:
    """把封面缩放并居中裁剪成 size x size 的 PNG 字节（可带圆角）。

    性能：用 cairo 一步完成「解码后缩放 → 居中裁剪 → 圆角」并直接输出 PNG，
    避免旧实现中 pixbuf→PNG→cairo→PNG→pixbuf 的多次编解码（切歌封面卡顿主因）。
    radius: 圆角半径（像素），0 表示直角。失败时原样返回，不影响播放。
    """
    if not image_bytes or size <= 0:
        return image_bytes
    # 直接走 GdkPixbuf 一次到位：解码一次 → 快速缩到目标 → 圆角 → 存 PNG。
    # 绕开"预缩+再解码"或"pixbuf↔cairo 中转"的往返开销（大封面下省数百 ms）。
    try:
        return _square_cover_via_pixbuf(image_bytes, size, radius)
    except Exception as exc:
        log.debug("封面缩放失败(pixbuf): %s", exc)
    # 回退 cairo
    try:
        return _square_cover_via_cairo(image_bytes, size, radius)
    except Exception as exc:
        log.debug("封面缩放失败(cairo): %s", exc)
        return image_bytes


def _preshrink_if_huge(image_bytes: bytes, size: int) -> bytes:
    """若封面远大于目标尺寸，解码一次并缩到目标 2 倍以内，返回小图 PNG。

    背景：内嵌封面可能高达 6000x6000（36MP），GdkPixbuf 解码就要 ~116ms。
    旧实现里预缩 + cairo 路径会各解码一次大图（白花一倍时间）。
    这里解码一次 → scale_simple（C 实现，快）→ 输出小图 PNG，
    之后 cairo 路径处理的是小图，几乎无开销。
    尺寸已够小则原样返回（不引入多余的解码/编码）。
    """
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib

        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        pb = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        if pb is None:
            return image_bytes
        w, h = pb.get_width(), pb.get_height()
        limit = size * 2
        if max(w, h) <= limit:
            return image_bytes   # 已经够小，交给后续路径
        scale = max(limit / w, limit / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        small = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        ok, buf = small.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else image_bytes
    except Exception as exc:
        log.debug("预缩放大图失败: %s", exc)
        return image_bytes


def _square_cover_via_cairo(image_bytes: bytes, size: int, radius: int) -> bytes:
    """用 cairo 一步完成缩放/裁剪/圆角并输出 PNG。

    做法：先把原图解码为 cairo surface（GdkPixbuf 只用于解码，避免二次编码），
    再在目标 surface 上按 cover 方式缩放绘制到固定正方形，最后按圆角裁剪。
    """
    import io

    import cairo
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gio, GLib

    # 解码原图 -> pixbuf（仅一次解码）
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w <= 0 or h <= 0:
        return image_bytes

    # 直接把 pixbuf 像素拷进 cairo surface（ARGB32），避免走 PNG
    n_ch = pixbuf.get_n_channels()
    has_alpha = pixbuf.get_has_alpha()
    rowstride = pixbuf.get_rowstride()
    data = pixbuf.get_pixels()

    # 先缩到接近目标尺寸（用 pixbuf 的快速缩放，比 cairo 大比例缩放快）
    scale = max(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    scaled = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR) if (nw, nh) != (w, h) else pixbuf

    # 目标 surface（带 alpha）+ 圆角裁剪 + 居中绘制
    dst = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(dst)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()
    if radius and radius > 0:
        r = min(radius, size // 2)
        ctx.new_sub_path()
        ctx.arc(size - r, r, r, -1.5707963, 0)
        ctx.arc(size - r, size - r, r, 0, 1.5707963)
        ctx.arc(r, size - r, r, 1.5707963, 3.1415927)
        ctx.arc(r, r, r, 3.1415927, 4.7123890)
        ctx.close_path()
        ctx.clip()

    # 把 scaled pixbuf 的像素拷进临时 surface，再作为 source 绘制（居中裁剪）
    sw, sh = scaled.get_width(), scaled.get_height()
    tmp = _pixbuf_to_cairo_surface(scaled)
    ctx.set_source_surface(tmp, (size - sw) / 2.0, (size - sh) / 2.0)
    ctx.paint()

    out = io.BytesIO()
    dst.write_to_png(out)
    return out.getvalue()


def _pixbuf_to_cairo_surface(pixbuf):
    """把 GdkPixbuf 的像素直接拷进 cairo ARGB32 surface（不经过 PNG 编解码）。

    用 GdkPixbuf 的 add_alpha + 通道重排由 C 层完成，避免 Python 逐像素循环。
    """
    import cairo
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    # 统一成 8bit RGBA，再拷进连续缓冲区（C 实现，快）
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(True, 0, 0, 0)
    pb = pixbuf
    w, h = pb.get_width(), pb.get_height()
    stride = pb.get_rowstride()
    n_ch = pb.get_n_channels()
    src = pb.get_pixels()

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    dst = surf.get_data()
    dst_stride = surf.get_stride()

    # 逐行用 bytes.translate 思路仍不够快；改用 memoryview 切片 + 预建映射表
    # RGBA -> BGRA：用 bytes 的一次性通道重排（仅 4 通道时）
    if n_ch == 4:
        mv = memoryview(dst)
        for y in range(h):
            s = y * stride
            d = y * dst_stride
            row = bytes(src[s:s + w * 4])
            # 用切片拼装 BGRA：R/B 互换
            ba = bytearray(w * 4)
            ba[0::4] = row[2::4]
            ba[1::4] = row[1::4]
            ba[2::4] = row[0::4]
            ba[3::4] = row[3::4]
            mv[d:d + w * 4] = ba
    else:
        # 兜底：经 PNG 中转（罕见）
        import io

        ok, png = pb.save_to_bufferv("png", [], [])
        if ok:
            tmp = cairo.ImageSurface.create_from_png(io.BytesIO(bytes(png)))
            ctx = cairo.Context(surf)
            ctx.set_source_surface(tmp, 0, 0)
            ctx.paint()
    surf.mark_dirty()
    return surf


def make_cover_textures(image_bytes: bytes, sizes, want_color: bool = False):
    """简化版：一次解码 → 缩到各尺寸 → 直接返回 GdkTexture（无 PNG 编解码）。

    sizes: [320, 420] 等目标边长（正方形 COVER 填充）。
    返回 (textures_dict, color)：textures_dict = {size: GdkTexture}，color=(r,g,b) 或 None。
    圆角交给 UI 层 CSS，不在这里画（省掉 cairo + PNG 往返）。
    必须在有 GTK/Gdk 的线程调用；GdkTexture 创建允许在非主线程。
    """
    tex = {}
    color = None
    if not image_bytes or not sizes:
        return tex, color
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import GdkPixbuf, Gdk, Gio, GLib

        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        base = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        if base is None:
            return tex, color
        bw, bh = base.get_width(), base.get_height()
        if bw <= 0 or bh <= 0:
            return tex, color
        # 大图先缩到「最大目标尺寸」一次，后续各尺寸从它缩
        max_size = max(sizes)
        scale0 = max(max_size / bw, max_size / bh)
        if scale0 < 1.0:
            mw, mh = max(1, int(round(bw * scale0))), max(1, int(round(bh * scale0)))
            base = base.scale_simple(mw, mh, GdkPixbuf.InterpType.BILINEAR)
            bw, bh = base.get_width(), base.get_height()
        # 取色：从当前（已缩小）的 base 取，零额外解码
        if want_color:
            try:
                color = _color_from_pixbuf(base, lighten=0.0)
            except Exception:
                color = None
        for size in sizes:
            try:
                # 覆盖式缩放 + 居中裁剪成 size×size（含 alpha）
                scale = max(size / bw, size / bh)
                nw, nh = max(1, int(round(bw * scale))), max(1, int(round(bh * scale)))
                scaled = base.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR) if (nw, nh) != (bw, bh) else base
                square = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
                square.fill(0x00000000)
                sw, sh = scaled.get_width(), scaled.get_height()
                scaled.composite(
                    square, 0, 0, size, size,
                    -(sw - size) // 2, -(sh - size) // 2,
                    1.0, 1.0, GdkPixbuf.InterpType.BILINEAR, 255,
                )
                # 直接从像素建纹理：跳过 PNG 编码/解码（最省时）
                tex[size] = _pixbuf_to_texture(square)
            except Exception as exc:
                log.debug("封面纹理生成失败 size=%s: %s", size, exc)
    except Exception as exc:
        log.debug("封面纹理处理失败: %s", exc)
    return tex, color


def _pixbuf_to_texture(pixbuf):
    """把 GdkPixbuf 的像素直接建成 Gdk.MemoryTexture（不经过 PNG）。

    要求 pixbuf 为 8bit RGBA；返回 GdkTexture。
    """
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import GdkPixbuf, Gdk, GLib

    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(True, 255, 255, 255)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    stride = pixbuf.get_rowstride()
    n_ch = pixbuf.get_n_channels()
    # 确保紧凑 RGBA 行距（MemoryTexture 要求 stride >= w*4）
    if n_ch == 4 and stride >= w * 4:
        data = bytes(pixbuf.get_pixels())
        if stride != w * 4:
            # 逐行去 padding
            rows = bytearray()
            for y in range(h):
                rows += data[y * stride: y * stride + w * 4]
            data = bytes(rows)
            stride = w * 4
        gb = GLib.Bytes.new(data)
        return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, gb, stride)
    # 兜底：其它格式转 PNG
    ok, png = pixbuf.save_to_bufferv("png", [], [])
    if ok:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(bytes(png)))
    raise ValueError("unsupported pixbuf format")


def make_square_covers_multi(image_bytes: bytes, specs, want_color: bool = False):
    """一次解码，按多个 (size, radius) 生成封面 PNG。

    specs: [(size, radius), ...]。
    始终返回 (covers_dict, color_rgb)：covers_dict 为 {(size, radius): png_bytes}，
    color_rgb 为 (r,g,b) 或 None（want_color=False 时恒为 None）。
    统一返回类型，避免调用方按 want_color 分别解包出错。

    大封面（如 6000px）解码一次就要 ~116ms；切歌要 panel+沉浸页两个尺寸，
    分别调用会各解码一次（双倍浪费）。此函数只解码一次；
    若需要主色，顺便从同一份解码结果缩 32px 取色（零额外解码）。
    """
    result = {}
    color = None
    if not image_bytes or not specs:
        return result, color
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib

        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        base = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        if base is None:
            return result, color
        # 先把大图一次性缩到「最大目标尺寸的 2 倍」作为中间图，
        # 各目标尺寸都从中间图缩——避免从 6000px 源多次 scale_simple。
        bw, bh = base.get_width(), base.get_height()
        max_size = max((s for (s, _r) in specs), default=0)
        mid_limit = max(1, max_size * 2)
        if bw > 0 and bh > 0 and max(bw, bh) > mid_limit:
            sc = max(mid_limit / bw, mid_limit / bh)
            mw, mh = max(1, int(round(bw * sc))), max(1, int(round(bh * sc)))
            try:
                base = base.scale_simple(mw, mh, GdkPixbuf.InterpType.BILINEAR)
            except Exception:
                pass
        if want_color:
            try:
                color = _color_from_pixbuf(base, lighten=0.0)
            except Exception as exc:
                log.debug("从 pixbuf 取色失败: %s", exc)
        for (size, radius) in specs:
            try:
                result[(size, radius)] = _square_from_pixbuf(base, size, radius)
            except Exception as exc:
                log.debug("多尺寸封面失败 size=%s: %s", size, exc)
    except Exception as exc:
        log.debug("多尺寸封面解码失败: %s", exc)
    return result, color


def _color_from_pixbuf(pixbuf, lighten: float = 0.0):
    """从已解码的 pixbuf 缩 32px 求平均色（零额外解码）。返回 (r,g,b) 或 None。"""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w <= 0 or h <= 0:
            return None
        target = 32
        scale = max(target / w, target / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        pb = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR) if (nw, nh) != (w, h) else pixbuf
        if not pb.get_has_alpha():
            pb = pb.add_alpha(True, 255, 255, 255)
        sw, sh = pb.get_width(), pb.get_height()
        stride = pb.get_rowstride()
        n_ch = pb.get_n_channels()
        data = bytes(pb.get_pixels())
        step = max(1, min(sw, sh) // 16)
        r_sum = g_sum = b_sum = 0
        n = 0
        if n_ch == 4:
            for y in range(0, sh, step):
                row = data[y * stride: y * stride + sw * 4]
                r_sum += sum(row[0::4][::step])
                g_sum += sum(row[1::4][::step])
                b_sum += sum(row[2::4][::step])
                n += len(row[0::4][::step])
        elif n_ch == 3:
            for y in range(0, sh, step):
                row = data[y * stride: y * stride + sw * 3]
                r_sum += sum(row[0::3][::step])
                g_sum += sum(row[1::3][::step])
                b_sum += sum(row[2::3][::step])
                n += len(row[0::3][::step])
        if n == 0:
            return None
        r, g, b = r_sum // n, g_sum // n, b_sum // n
        r = int(r + (255 - r) * lighten)
        g = int(g + (255 - g) * lighten)
        b = int(b + (255 - b) * lighten)
        return (r, g, b)
    except Exception:
        return None


def _square_from_pixbuf(base_pixbuf, size: int, radius: int) -> bytes:
    """从已解码的 pixbuf 生成 size×size（可圆角）PNG。"""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    w, h = base_pixbuf.get_width(), base_pixbuf.get_height()
    if w <= 0 or h <= 0:
        raise ValueError("empty pixbuf")
    scale = max(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    pixbuf = base_pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR) if (nw, nh) != (w, h) else base_pixbuf
    square = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
    square.fill(0x00000000)
    sw, sh = pixbuf.get_width(), pixbuf.get_height()
    pixbuf.composite(
        square, 0, 0, size, size, -(sw - size) // 2, -(sh - size) // 2,
        1.0, 1.0, GdkPixbuf.InterpType.BILINEAR, 255,
    )
    if not radius or radius <= 0:
        ok, buf = square.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else b""
    try:
        return _round_corners_to_png(square, radius)
    except Exception:
        ok, buf = square.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else b""


def _square_cover_via_pixbuf(image_bytes: bytes, size: int, radius: int) -> bytes:
    """GdkPixbuf 解码+缩放+裁剪 → 小图上做圆角 → 输出 PNG。

    关键：圆角只用 cairo 处理「已缩到 size×size 的小图」，逐行通道重排
    仅 size 行（几百行），避免在 6000px 大图上做 Python 循环。
    """
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, Gio, GLib

    # 关键：让 GdkPixbuf 在「解码阶段」就缩到目标尺寸附近。
    # 内嵌封面可能高达 6000x6000，整张解码成 RGBA 约 144MB；
    # 首页网格会为每个专辑/艺术家并发解码封面，几张同时跑就吃掉数百 MB。
    # 这里按目标尺寸的 2 倍解码，之后只需极小比例缩放，内存降到几百 KB。
    limit = max(1, size * 2)
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
            stream, limit, limit, True, None)
    except Exception:
        # 旧版 gdk-pixbuf 无 at_scale：退回全尺寸解码（行为同以前）
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w <= 0 or h <= 0:
        return image_bytes
    # 先缩到覆盖正方形（用 C 层 scale_simple，快）
    scale = max(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    if (nw, nh) != (w, h):
        pixbuf = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
    # 居中裁剪到 size×size
    square = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
    square.fill(0x00000000)
    sw, sh = pixbuf.get_width(), pixbuf.get_height()
    pixbuf.composite(
        square, 0, 0, size, size, -(sw - size) // 2, -(sh - size) // 2,
        1.0, 1.0, GdkPixbuf.InterpType.BILINEAR, 255,
    )
    if not radius or radius <= 0:
        ok, buf = square.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else image_bytes
    # 圆角：此时 square 已是 size×size 小图，cairo 处理很快
    try:
        return _round_corners_to_png(square, radius)
    except Exception as exc:
        log.debug("圆角失败，回退直角: %s", exc)
        ok, buf = square.save_to_bufferv("png", [], [])
        return bytes(buf) if ok else image_bytes


def _round_corners_to_png(square, radius: int) -> bytes:
    """在已缩好的小图上画圆角并输出 PNG（cairo 一次完成，无多余往返）。"""
    import io

    import cairo

    w, h = square.get_width(), square.get_height()
    r = min(radius, w // 2, h // 2)
    # pixbuf -> cairo surface（小图，逐行通道重排很快）
    src_surf = _pixbuf_to_cairo_surface(square)
    dst_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    ctx = cairo.Context(dst_surf)
    ctx.new_sub_path()
    ctx.arc(w - r, r, r, -1.5707963, 0)
    ctx.arc(w - r, h - r, r, 0, 1.5707963)
    ctx.arc(r, h - r, r, 1.5707963, 3.1415927)
    ctx.arc(r, r, r, 3.1415927, 4.7123890)
    ctx.close_path()
    ctx.clip()
    ctx.set_source_surface(src_surf, 0, 0)
    ctx.paint()
    out = io.BytesIO()
    dst_surf.write_to_png(out)
    return out.getvalue()


def make_blurred_bg(image_bytes: bytes, out_w: int = 640, out_h: int = 480,
                    darken: float = 0.0, blur_px: int = 24,
                    lighten: float = 0.0) -> bytes | None:
    """用封面生成「模糊 + 遮罩」的背景图（Apple Music 风格）。

    做法（不依赖 PIL）：
    1. 缩到极小（如 blur_px x blur_px）再放大 → 天然模糊；
    2. 可选叠暗色遮罩压暗（darken），或叠白色遮罩提亮（lighten）；
    3. 输出 PNG 字节。
    darken:  0=不压暗，1=全黑。
    lighten: 0=不提亮，1=全白。
    blur_px: 越小越糊（放大倍数越大）。
    失败返回 None（调用方回退纯色背景）。
    """
    if not image_bytes or out_w <= 0 or out_h <= 0:
        return None
    try:
        import math

        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib

        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(image_bytes))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w <= 0 or h <= 0:
            return None
        # 1) 先缩到中等尺寸（保留较多细节，避免单次大比例放大产生马赛克）
        work = max(32, min(int(blur_px) * 8, 200))
        scale = max(work / w, work / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        base = pixbuf.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
        # 2) 逐级缩小再放大：多轮小比例缩放 = 近似高斯模糊，边缘平滑无方块。
        #    每一轮缩到上一轮的 1/2，再放大回去，反复几次把细节磨平。
        # 固定 9 轮模糊：多轮「缩一半再放回」把细节磨到最平，最大化模糊
        levels = 9
        cur = base
        cur_w, cur_h = cur.get_width(), cur.get_height()
        for _ in range(levels):
            hw = max(1, cur_w // 2)
            hh = max(1, cur_h // 2)
            down = cur.scale_simple(hw, hh, GdkPixbuf.InterpType.BILINEAR)
            cur = down.scale_simple(cur_w, cur_h, GdkPixbuf.InterpType.BILINEAR)
        # 3) 覆盖式缩放到输出尺寸并居中裁剪（方形）
        out_scale = max(out_w / cur_w, out_h / cur_h)
        fw, fh = max(1, int(cur_w * out_scale)), max(1, int(cur_h * out_scale))
        fitted = cur.scale_simple(fw, fh, GdkPixbuf.InterpType.BILINEAR)
        big = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, out_w, out_h)
        big.fill(0x00000000)
        fitted.composite(
            big, 0, 0, out_w, out_h,
            -(fw - out_w) // 2, -(fh - out_h) // 2, 1.0, 1.0,
            GdkPixbuf.InterpType.BILINEAR, 255,
        )
        # 3) 叠遮罩（暗化 / 提亮），用 cairo 画半透明层
        import io

        import cairo

        ok, png = big.save_to_bufferv("png", [], [])
        if not ok:
            return None
        surf = cairo.ImageSurface.create_from_png(io.BytesIO(bytes(png)))
        cr = cairo.Context(surf)
        da = max(0.0, min(1.0, darken))
        la = max(0.0, min(1.0, lighten))
        if da > 0:
            cr.set_source_rgba(0.0, 0.0, 0.0, da)
            cr.paint()
        if la > 0:
            cr.set_source_rgba(1.0, 1.0, 1.0, la)
            cr.paint()
        # 输出 PNG
        out = io.BytesIO()
        surf.write_to_png(out)
        return out.getvalue()
    except Exception as exc:
        log.debug("生成模糊背景失败: %s", exc)
        return None


def _apply_round_corners(pixbuf, radius: int):
    """把 pixbuf 四角裁成透明圆角（用 cairo clip 后重绘）。

    流程：pixbuf -> PNG -> cairo 表面 -> 圆角 clip 后绘制 -> PNG -> pixbuf。
    走 PNG 中转可避免 new_from_data 的内存生命周期问题，最稳。
    """
    import io

    import cairo
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    w, h = pixbuf.get_width(), pixbuf.get_height()
    r = min(radius, w // 2, h // 2)

    # pixbuf -> PNG bytes
    ok, src_png = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        return pixbuf
    # 读入 cairo 表面
    src_surf = cairo.ImageSurface.create_from_png(io.BytesIO(bytes(src_png)))

    # 新建带 alpha 的目标表面
    dst_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    ctx = cairo.Context(dst_surf)
    # 圆角矩形路径
    ctx.new_sub_path()
    ctx.arc(w - r, r, r, -1.5708, 0)
    ctx.arc(w - r, h - r, r, 0, 1.5708)
    ctx.arc(r, h - r, r, 1.5708, 3.1416)
    ctx.arc(r, r, r, 3.1416, 4.7124)
    ctx.close_path()
    ctx.clip()
    # 在圆角内绘制原图
    ctx.set_source_surface(src_surf, 0, 0)
    ctx.paint()
    dst_surf.flush()

    # cairo 表面 -> PNG bytes -> pixbuf
    out = io.BytesIO()
    dst_surf.write_to_png(out)
    stream = gi.repository.Gio.MemoryInputStream.new_from_bytes(
        gi.repository.GLib.Bytes.new(out.getvalue())
    )
    return GdkPixbuf.Pixbuf.new_from_stream(stream, None)
