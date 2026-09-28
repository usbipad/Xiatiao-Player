"""沉浸页前景色决策：按背景色计算可读的前景色。

设计目标：
- 用 WCAG 相对亮度（含 sRGB 伽马校正）替代旧的「299/587/114」近似公式，
  亮度判定更贴近人眼，临界色不再误判。
- 不再硬切单一阈值，而是按「目标对比度」选择前景，消除临界跳变。
- 前景不再是纯黑/纯白，而是带极少量背景互补色的「带色中性色」，
  与封面主色同一语境，视觉更成套，同时不牺牲对比度。

本模块为纯函数，不依赖 GTK，便于单测与在后台线程调用。
"""
from __future__ import annotations

import colorsys

#: sRGB 线性化后计算相对亮度的通道系数（WCAG 2.1）
_LUM_R = 0.2126
_LUM_G = 0.7152
_LUM_B = 0.0722

#: 主体文字（当前歌词、歌名、控件图标）的目标对比度
CONTRAST_PRIMARY = 4.5
#: 次要文字（非活动歌词、歌手、时间）的目标对比度
CONTRAST_SECONDARY = 3.0


def _srgb_to_linear(c: float) -> float:
    """单通道 sRGB(0..1) → 线性(0..1)，按 WCAG 定义。"""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度（0..1）。含伽马校正，比加权平均更准。"""
    r, g, b = rgb
    lr = _srgb_to_linear(r / 255.0)
    lg = _srgb_to_linear(g / 255.0)
    lb = _srgb_to_linear(b / 255.0)
    return _LUM_R * lr + _LUM_G * lg + _LUM_B * lb


def contrast_ratio(rgb1: tuple[int, int, int],
                   rgb2: tuple[int, int, int]) -> float:
    """两色的 WCAG 对比度比（1..21）。"""
    l1 = relative_luminance(rgb1)
    l2 = relative_luminance(rgb2)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _tinted_neutral(bg: tuple[int, int, int], dark_fg: bool,
                    tint: float = 0.08) -> tuple[int, int, int]:
    """生成「带色中性前景」：在黑/白基础上混入极少量背景互补色。

    dark_fg=True  → 深色前景（背景亮时用），基准 #1a1a1a。
    dark_fg=False → 浅色前景（背景暗时用），基准 #ffffff。

    tint: 混入背景色的比例（0..1）。0 即纯黑/白；默认 0.08 很克制，
          只让前景「带一点背景色相」，不改变明暗属性、不影响对比度判据。
    做法：取背景色相 H，用极低饱和度、按明暗方向定亮度，直接生成前景，
    而非真的做 RGB 混合——这样前景始终干净，不会因混合变浑。
    """
    r, g, b = bg
    h, _l, _s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    if dark_fg:
        # 深色前景：低亮度、低饱和（趋近 #1a1a1a，但带一点点背景色相）
        rr, gg, bb = colorsys.hls_to_rgb(h, 0.10, tint * 1.4)
    else:
        # 浅色前景：高亮度、低饱和（趋近 #ffffff，但带一点点背景色相）
        rr, gg, bb = colorsys.hls_to_rgb(h, 0.97, tint)
    return (int(rr * 255), int(gg * 255), int(bb * 255))


def _pick_fg(bg: tuple[int, int, int], target: float) -> tuple[int, int, int]:
    """按目标对比度选前景色。

    先试「带色浅色」与「带色深色」，取对比度更高者；
    若两者都不足 target（极少见，如中灰背景），回退到纯黑/纯白中对比更高者。
    """
    light = _tinted_neutral(bg, dark_fg=False)
    dark = _tinted_neutral(bg, dark_fg=True)
    c_light = contrast_ratio(bg, light)
    c_dark = contrast_ratio(bg, dark)
    if c_light >= c_dark:
        best, best_c = light, c_light
    else:
        best, best_c = dark, c_dark
    if best_c >= target:
        return best
    # 对比不足：用纯黑/纯白里对比更高的那个兜底
    if contrast_ratio(bg, (255, 255, 255)) >= contrast_ratio(bg, (26, 26, 26)):
        return (255, 255, 255)
    return (26, 26, 26)


def _is_light_fg(fg: tuple[int, int, int]) -> bool:
    """该前景是否为浅色（用于统一 is_dark 判定方向）。"""
    return relative_luminance(fg) > 0.5


def decide_foreground(bg: tuple[int, int, int]) -> dict:
    """给定背景色，输出沉浸页整套前景色决策。

    返回 dict：
      is_dark:  背景是否偏暗（供 SeekBar 等自绘控件切换）
      primary:  主体文字色（当前歌词/歌名/控件图标）
      dim:      次要文字色（非活动歌词/歌手/时间）
      faint:    更弱的远处歌词色
      on_primary: 主背景色上放强调块时的前景（黑/白）

    关键：is_dark 与 primary 必须同源——都来自「_pick_fg 选出的前景方向」，
    否则中灰等临界背景会出现「控件切白、文字选黑」的自相矛盾。
    """
    primary = _pick_fg(bg, CONTRAST_PRIMARY)
    # is_dark 由前景方向反推：前景是浅色 ⇒ 背景偏暗。
    is_dark = _is_light_fg(primary)
    # 次要色：用较弱目标，避免整体过白/过黑、丢失层次
    dim = _pick_fg(bg, CONTRAST_SECONDARY)
    faint = _pick_fg(bg, CONTRAST_SECONDARY)
    # on_primary 用于「封面色块上的前景」：与主前景方向保持一致
    on_primary = (255, 255, 255) if is_dark else (26, 26, 26)
    return {
        "is_dark": is_dark,
        "primary": primary,
        "dim": dim,
        "faint": faint,
        "on_primary": on_primary,
    }


def rgb_to_css(rgb: tuple[int, int, int], alpha: float | None = None) -> str:
    """(r,g,b[,a]) → CSS 颜色串。alpha=None 时不带透明度。"""
    r, g, b = rgb
    if alpha is None:
        return f"rgb({r}, {g}, {b})"
    return f"rgba({r}, {g}, {b}, {alpha:.3f})"
