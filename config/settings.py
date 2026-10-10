"""应用配置持久化（JSON 实现，零编译依赖）—— 本地曲库版。"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
CONFIG_FILENAME = "config.json"

#: 应用版本号（单一真相源）。发版时只改这里。
APP_VERSION = "1.0.5"


def xdg_config_dir() -> str:
    """$XDG_CONFIG_HOME/xiatiao（未设置 XDG_CONFIG_HOME 时回退 ~/.config）。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, APP_DIR_NAME)


def xdg_data_dir() -> str:
    """$XDG_DATA_HOME/xiatiao（未设置 XDG_DATA_HOME 时回退 ~/.local/share）。"""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, APP_DIR_NAME)


def xdg_cache_dir() -> str:
    """$XDG_CACHE_HOME/xiatiao（未设置 XDG_CACHE_HOME 时回退 ~/.cache）。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, APP_DIR_NAME)

DEFAULT_CONFIG: Dict[str, Any] = {
    "music_dirs": [],
    "volume": 1.0,
    # ---- 播放 ----
    "restore_playback": True,
    "default_play_mode": 0,
    "remember_volume": True,
    # ---- DSD 输出 ----
    # DSD 输出总开关：关=走老逻辑（DSD 经 ffmpeg 软解为 PCM + PipeWire 默认输出）；
    #                开=下面的 DSD 输出模式与输出设备才生效。
    "dsd_output_enabled": False,
    # DSD 输出模式：auto（自动按 DAC 能力）/ native（原生直通）/ dop / pcm（软解）
    "dsd_output_mode": "auto",
    # 输出设备名（ALSA hw 设备名）；空=系统默认（PipeWire）
    "output_device": "",
    # ---- 下载 ----
    # 在线歌曲下载目录（空=默认 ~/下载）
    "download_dir": "",
    # ---- 在线音源（Subsonic 通用客户端）----
    # 在线音源总开关：关闭后不主动连接/显示在线音乐（默认启用）
    "subsonic_enabled": False,
    # 服务端地址（如 http://127.0.0.1:4533 指向 Navidrome/自建服务）
    "subsonic_url": "",
    "subsonic_user": "",
    "subsonic_password": "",
    # 认证方式：True=标准 token（md5(password+salt)）；False=明文 p=（兼容部分服务端）
    "subsonic_use_token": True,
    # 额外查询参数（形如 a=1&b=2），留给特殊服务端
    "subsonic_extra_params": "",
    # 在线音质档位：standard / high / lossless / hires / master
    "online_quality": "lossless",
    # ---- 音效 ----
    "audio_effect": "off",
    # 全局 DSP 开关（Rust 内置链）
    "dsp_enabled": False,
    # 嵌入式 CamillaDSP 引擎开关（运行中可切）
    "camilla_enabled": False,
    # ---- 可视化 ----
    "viz_fps": 90,
    "viz_fft_window": 2048,
    "viz_bin_count": 64,
    "viz_hop": 128,
    "viz_enabled": True,
    "viz_style": "bars",
    "viz_fall_speed": 0.50,
    "viz_rise_speed": 0.70,
    "viz_db_floor": -50.0,
    "viz_scale_mode": "freq",
    "viz_sync_delay_ms": 250.0,
    # ---- 外观 ----
    "theme": "system",
    "row_density": "normal",
    # 界面语言：system（跟随系统）/ zh（中文）/ en（英文）
    "language": "system",
    # 沉浸页背景：开启后使用封面模糊图铺满背景（关闭则用纯色）
    "nowplaying_blur_bg": True,
    # 模糊强度（越小越糊）
    "nowplaying_blur_px": 6,
    # 背景亮度自适应阈值（WCAG 相对亮度）：背景加权亮度**低于**此值时
    # 用浅色前景（白字），否则用深色前景（黑字）。
    # 实测（59 首样本）：0.55 约 86% 白字 / 14% 黑字，中灰/暖色封面（如
    # 暖橙日落、浅蓝）多判白字，氛围更好；0.38（旧值）黑字偏多、发闷。
    "nowplaying_dark_threshold": 0.55,
    # 主界面背景是否跟随当前封面主色调（关闭则用主题色 @view_bg_color）
    "main_bg_follow_cover": False,
    # 进度条已播段是否跟随当前封面主色（关闭则用默认深灰/白）
    "progress_follow_cover": True,
    # ---- 行为 ----
    "auto_scan_on_start": True,
    "close_to_tray": False,
    "remember_window_size": True,
    "window_width": 1400,
    "window_height": 900,
    "window_maximized": False,
    "last_track": None,
    # ---- 快捷键（Gdk 键名；可带 Ctrl+/Alt+/Shift+ 前缀；空串=未绑定）----
    #   默认全部为空，由用户在「设置 → 快捷键」自行录制。
    "shortcuts": {
        "play_pause": "",
        "prev": "",
        "next": "",
        "seek_back": "",
        "seek_fwd": "",
        "vol_up": "",
        "vol_down": "",
    },
    # ---- 主页卡片随机轮播 ----
    "card_rotate_enabled": True,
    # 轮播速度：slow / medium / fast
    "card_rotate_speed": "medium",
}

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".ape", ".wv", ".m4a", ".ogg", ".wav", ".dsf", ".dff",
}


class AppConfig:
    """配置对象：所有读取带默认值兜底，写入失败不影响主流程。

    线程安全：本对象被主线程与后台线程（如本地曲库扫描、可视化）并发访问。
    用一个可重入锁保护 _data 的读写与文件保存——
      * 避免 `save()` 迭代 _data 时另一线程 `set()` 触发
        `RuntimeError: dictionary changed size during iteration`；
      * 避免两个线程并发写同一临时文件导致配置文件损坏。
    锁只覆盖内存字典操作与磁盘写入，不跨用户回调，故无死锁风险。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        # 可重入：set_xxx() 内部会调用 save()，两者都持锁。
        self._lock = threading.RLock()
        self.load()

    @staticmethod
    def _default_path() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        return Path(base) / APP_DIR_NAME / CONFIG_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                with self._lock:
                    self._data.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("读取配置失败，使用默认值: %s", exc)

    def save(self) -> None:
        # 全程持锁：不仅迭代 _data 要防并发修改，写临时文件 + replace 也必须
        # 串行——否则两个线程会用同一个 .json.tmp 文件名互相踩，一个 replace
        # 后另一个 replace 找不到 tmp 而抛 FileNotFoundError。配置写入不频繁，
        # 锁内做磁盘 IO 的代价可接受。
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".json.tmp")
                with tmp.open("w", encoding="utf-8") as fp:
                    json.dump(self._data, fp, ensure_ascii=False, indent=2)
                tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存配置失败: %s", exc)

    # ---- 音乐目录 ----
    def get_music_dirs(self) -> List[str]:
        dirs = self._data.get("music_dirs", [])
        if not isinstance(dirs, list):
            return []
        return [d for d in dirs if isinstance(d, str)]

    def set_music_dirs(self, dirs: List[str]) -> None:
        with self._lock:
            self._data["music_dirs"] = list(dict.fromkeys(dirs))
        self.save()

    def add_music_dir(self, directory: str) -> bool:
        dirs = self.get_music_dirs()
        if directory in dirs:
            return False
        dirs.append(directory)
        self.set_music_dirs(dirs)
        return True

    def remove_music_dir(self, directory: str) -> bool:
        dirs = self.get_music_dirs()
        if directory not in dirs:
            return False
        dirs.remove(directory)
        self.set_music_dirs(dirs)
        return True

    # ---- 通用读写 ----
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self.save()

    def get_bool(self, key: str, default: bool = False) -> bool:
        with self._lock:
            return bool(self._data.get(key, default))

    def set_bool(self, key: str, value: bool) -> None:
        with self._lock:
            self._data[key] = bool(value)
        self.save()

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            with self._lock:
                return int(self._data.get(key, default))
        except (TypeError, ValueError):
            return default

    def set_int(self, key: str, value: int) -> None:
        with self._lock:
            self._data[key] = int(value)
        self.save()

    def get_str(self, key: str, default: str = "") -> str:
        with self._lock:
            val = self._data.get(key, default)
        return val if isinstance(val, str) else default

    def set_str(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
        self.save()


_instance: AppConfig | None = None
_instance_lock = threading.Lock()


def get_config() -> AppConfig:
    """返回全局配置单例（线程安全）。

    双重检查锁：多线程首次并发调用时，避免构造出多个 AppConfig 实例
    （各自持有独立 _data，会导致配置读写不一致）。
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AppConfig()
    return _instance
