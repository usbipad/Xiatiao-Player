"""MPRIS 封面落盘（从 ui/window.py 抽出）。

把封面字节写入 XDG 缓存目录，供 MPRIS 的 mpris:artUrl 指向本地文件
（KDE Connect / Android 端据此同步封面到通知栏）。

关键设计（勿改）：文件名带 token（唯一），原因有二——
  1. 避免快速连切时旧线程覆盖新封面（即使有 token 校验，文件层面也隔离）；
  2. mpris:artUrl 每次变化，客户端不会命中旧缓存，
     否则固定 URL 会导致一直显示第一张封面。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def cover_cache_dir() -> str:
    """MPRIS 封面落盘目录（XDG 缓存目录下）。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "xiatiao")


def cover_ext(image_bytes: bytes) -> str:
    """按图片魔数判定扩展名。"""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def write_mpris_cover(image_bytes: bytes, token: int) -> Optional[str]:
    """把封面字节写成缓存文件，返回路径；失败返回 None。

    文件名带 token（唯一）：避免快速连切时旧线程覆盖新封面，且让
    mpris:artUrl 每次变化（客户端不命中旧缓存）。同时清理本目录下
    其它过期封面文件（保留当前这张）。
    """
    if not image_bytes:
        return None
    try:
        ext = cover_ext(image_bytes)
        d = cover_cache_dir()
        os.makedirs(d, exist_ok=True)
        name = f"mpris_cover_{token}{ext}"
        path = os.path.join(d, name)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fp:
            fp.write(image_bytes)
        os.replace(tmp, path)
        # 清理本目录下其它 mpris_cover_* 旧文件，避免无限堆积
        try:
            for fn in os.listdir(d):
                if fn == name or not fn.startswith("mpris_cover_"):
                    continue
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
        except OSError:
            pass
        return path
    except OSError as exc:
        log.debug("写入 MPRIS 封面失败: %s", exc)
        return None
