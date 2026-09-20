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
                      dark_threshold: float) -> dict:
    """处理封面，返回资产字典。

    参数：
        cover_raw: 原始封面字节；None/空则返回空资产。
        panel_size: 左侧面板封面尺寸。
        np_size: 沉浸页封面尺寸。
        blur_on: 是否生成沉浸页模糊背景。
        blur_px: 模糊强度。
        dark_threshold: 背景亮度阈值（低于此值判定为暗背景）。

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
        "seekbar_rgb": None,
        "bg_dark": None,
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
                from models import lighten_for_background, tint_for_background
                out["bg_rgb"] = lighten_for_background((r, g, b))
                # 主界面「背景跟随封面」：保留原始饱和度（按比例压制），
                # 使淡色封面得到淡背景，不再被强制拉成鲜艳纯色。
                out["main_bg_rgb"] = tint_for_background((r, g, b))
                f = 0.72
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
                                      lighten=0.25)
            else:
                # 关闭背景功能：背景回退主题色；进度条仍跟随封面主色
                out["bg_rgb"] = None
            if png:
                from gi.repository import Gdk, GLib
                out["bg_tex"] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
                out["bg_dark"] = _is_dark_background(png, dark_threshold)
        except Exception:
            out["bg_tex"] = None

        out["pending_cover_raw"] = cover_raw
    except Exception:
        out["panel_tex"] = None
        out["np_tex"] = None

    return out


def _is_dark_background(png_bytes: bytes, threshold: float):
    """计算模糊背景的平均亮度，判断是否偏暗；失败返回 None。"""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio, GLib
        st = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(png_bytes))
        pb = GdkPixbuf.Pixbuf.new_from_stream(st, None)
        w, h = pb.get_width(), pb.get_height()
        data = bytes(pb.get_pixels())
        stride = pb.get_rowstride()
        nch = pb.get_n_channels()
        sr = sg = sb = n = 0
        step = max(1, min(w, h) // 16)
        for y in range(0, h, step):
            base = y * stride
            for x in range(0, w, step):
                o = base + x * nch
                sr += data[o]
                sg += data[o + 1]
                sb += data[o + 2]
                n += 1
        if not n:
            return None
        lum = (sr * 299 + sg * 587 + sb * 114) / (n * 255000.0)
        try:
            thr = float(threshold)
        except Exception:
            thr = 0.65
        return lum < thr
    except Exception:
        return None
