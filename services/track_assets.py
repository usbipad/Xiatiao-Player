"""切歌资产加载：封面提取 / 缩放 / 主色 / 背景模糊 / 亮度判断。

从 ui/window.py 的 _load_track_assets_bg 抽出。本模块为**纯逻辑**：
不做 UI 操作、不持控件引用，仅输入封面字节与尺寸参数，输出纹理与颜色。
设计为可在后台线程运行（与原实现一致）。

注意：返回的 Gdk.Texture 在后台线程创建——与既有实现一致
（GdkTexture 不可变，跨线程创建可接受）。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def load_cover_assets(cover_raw: bytes | None,
                      panel_size: int,
                      np_size: int,
                      blur_on: bool,
                      blur_px: int,
                      dark_threshold: float,
                      dark_theme: bool = False) -> dict:
    """处理封面，返回资产字典。

    参数：
        cover_raw: 原始封面字节；None/空则返回空资产。
        panel_size: 左侧面板封面尺寸。
        np_size: 沉浸页封面尺寸。
        blur_on: 是否生成沉浸页模糊背景。
        blur_px: 模糊强度。
        dark_threshold: 背景亮度阈值（低于此值判定为暗背景）。
        dark_theme: 当前是否暗色主题。为 True 时主界面背景色压暗，
                    否则暗色模式下背景仍被拉到高亮度，表现为「白灰蒙层」。

    返回 dict：
        panel_tex, np_tex        —— Gdk.Texture 或 None
        bg_tex                   —— 沉浸页模糊背景纹理或 None
        bg_rgb, seekbar_rgb      —— 颜色三元组或 None
        bg_dark                  —— bool 或 None（是否暗背景）
        pending_cover_raw        —— 原始封面字节（供后续惰性取色）
    """
    out = {
        "panel_tex": None,
        "np_tex": None,
        "bg_tex": None,
        "bg_rgb": None,
        "main_bg_rgb": None,
        "dominant_rgb": None,
        "seekbar_rgb": None,
        "bg_dark": None,
        "bg_png_path": None,
        "popover_bg_path": None,
        "pending_cover_raw": None,
    }
    if not cover_raw:
        return out

    try:
        from models.coverart import make_cover_textures
        texs, dom_raw = make_cover_textures(
            cover_raw, [panel_size, np_size], want_color=True,
        )
        out["panel_tex"] = texs.get(panel_size)
        out["np_tex"] = texs.get(np_size)
        # 背景/进度条色（后台算好，主线程只接收）
        if dom_raw:
            try:
                r, g, b = dom_raw
                # 原色主色一并带出：系统切明暗时据此重算主界面背景
                # （纯计算，无需重新解码封面）。
                out["dominant_rgb"] = (r, g, b)
                from models import lighten_for_background, tint_for_background
                out["bg_rgb"] = lighten_for_background((r, g, b))
                # 主界面「背景跟随封面」：保留原始饱和度（按比例压制），
                # 使淡色封面得到淡背景，不再被强制拉成鲜艳纯色。
                out["main_bg_rgb"] = tint_for_background((r, g, b), dark=dark_theme)
                f = 0.72
                # 进度条跟随封面取色：开关关闭时不输出颜色（进度条回退默认深灰/白）
                try:
                    from config.settings import get_config
                    _follow = get_config().get_bool("progress_follow_cover", True)
                except Exception:
                    _follow = True
                if _follow:
                    out["seekbar_rgb"] = (r / 255.0 * f, g / 255.0 * f, b / 255.0 * f)
            except Exception:
                out["bg_rgb"] = None
                out["seekbar_rgb"] = None

        # 背景模糊图（仅用户开启时生成）
        try:
            png = None
            if blur_on:
                from models.coverart import make_blurred_bg
                png = make_blurred_bg(cover_raw, 640, 480,
                                      darken=0.0, blur_px=blur_px,
                                      lighten=0.25,
                                      edge_vignette=0.55)   # 上下暗角→歌词边缘隐去
            else:
                # 关闭背景功能：背景回退主题色；进度条仍跟随封面主色
                out["bg_rgb"] = None
            if png:
                from gi.repository import Gdk, GLib
                out["bg_tex"] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
                out["bg_dark"] = _is_dark_background(png, dark_threshold)
                # 模糊图写到临时文件：供沉浸页音效气泡用 CSS background-image
                # 直接引用（GSK 无法模糊 popover 后方，用同一张模糊图作气泡底，
                # 与沉浸页视觉一致）。
                try:
                    import os as _os
                    import tempfile as _tf
                    _p = _os.path.join(_tf.gettempdir(),
                                       "xiatiao-immersive-bg.png")
                    with open(_p, "wb") as _fp:
                        _fp.write(png)
                    out["bg_png_path"] = _p
                    # 气泡专用：最高模糊（seed_dim 很小 → 高度模糊的色块底）
                    png_blur = make_blurred_bg(cover_raw, 640, 480,
                                               darken=0.0, blur_px=4,
                                               lighten=0.25, min_seed=4)
                    if png_blur:
                        _pb = _os.path.join(_tf.gettempdir(),
                                            "xiatiao-popover-bg.png")
                        with open(_pb, "wb") as _fp:
                            _fp.write(png_blur)
                        out["popover_bg_path"] = _pb
                except Exception:
                    out["bg_png_path"] = None
        except Exception:
            out["bg_tex"] = None

        out["pending_cover_raw"] = cover_raw
    except Exception:
        out["panel_tex"] = None
        out["np_tex"] = None

    return out


def _is_dark_background(png_bytes: bytes, threshold: float):
    """判断模糊背景是否偏暗（决定沉浸页前景黑白）；失败返回 None。

    改进（相比旧的「整图算术平均」）：
      1) **区域加权**：沉浸页前景（歌词在右、控件在底）实际只压在
         画面右侧与底部；整图平均会被左侧封面区/局部深色块带偏
         （几何封面「左黑右白」会被平均成中灰 → 误判暗 → 前景用白）。
         故按「右列歌词区、底部控件区」加大权重。
      2) **截尾**：去掉最暗/最亮各 15% 像素（边框/纯黑 logo 不代表主体）。
      3) **中位数**：取加权后亮度中位数，抗离群值（非算术平均）。

    与 core.color_contrast 用同一套 WCAG 亮度公式（含 sRGB 伽马校正）。
    阈值默认 0.38。
    """
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib

        from core.color_contrast import relative_luminance
        st = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(png_bytes))
        pb = GdkPixbuf.Pixbuf.new_from_stream(st, None)
        w, h = pb.get_width(), pb.get_height()
        if w <= 0 or h <= 0:
            return None
        data = bytes(pb.get_pixels())
        stride = pb.get_rowstride()
        nch = pb.get_n_channels()
        step = max(1, min(w, h) // 24)

        weighted_lums: list[tuple[float, float]] = []   # (亮度, 权重)
        for y in range(0, h, step):
            base = y * stride
            fy = y / h            # 0(顶)..1(底)
            for x in range(0, w, step):
                o = base + x * nch
                r, g, b = data[o], data[o + 1], data[o + 2]
                lum = relative_luminance((r, g, b))
                fx = x / w        # 0(左)..1(右)
                # 区域权重：右侧歌词区权重最高，底部控件区次之。
                wgt = 0.25
                if fx >= 0.45:                       # 右半：歌词区
                    wgt = 1.0
                elif fx >= 0.30:                     # 中右过渡
                    wgt = 0.6
                if fy >= 0.80:                       # 底部：控件区
                    wgt += 0.6
                elif fy >= 0.65:
                    wgt += 0.2
                if fy <= 0.10:                       # 顶部栏
                    wgt += 0.2
                weighted_lums.append((lum, wgt))
        if not weighted_lums:
            return None

        # 截尾：按亮度排序，去掉最暗/最亮各 15%（按数量）。
        weighted_lums.sort(key=lambda t: t[0])
        total = len(weighted_lums)
        lo = int(total * 0.15)
        hi = int(total * 0.85)
        core = weighted_lums[lo:hi] or weighted_lums

        # 加权分位数（p75）：按权重累积到总权重 75% 时的亮度。
        # 用 p75 而非中位数——中位数会被「底部暗角 / 封面深色块」拉低，
        # 把「浅灰背景」误判成暗（前景用白字，实际对比不足）。
        # p75 代表「较亮的那部分背景」，前景要在其上也可读：
        #   若连较亮区域都够亮 → 背景偏亮 → 用深色前景。
        core_sorted = sorted(core, key=lambda t: t[0])
        total_w = sum(w2 for _l, w2 in core_sorted) or 1.0
        acc = 0.0
        median_lum = core_sorted[len(core_sorted) // 2][0]
        for lum, wgt in core_sorted:
            acc += wgt
            if acc >= total_w * 0.75:
                median_lum = lum
                break

        try:
            thr = float(threshold)
        except Exception:
            thr = 0.38
        if thr > 0.55:
            thr = 0.38
        return median_lum < thr
    except Exception:
        return None
