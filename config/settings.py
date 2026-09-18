"""应用配置持久化（JSON 实现，零编译依赖）—— 本地曲库版。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "music_dirs": [],
    "volume": 1.0,
    # ---- 播放 ----
    "restore_playback": True,
    "default_play_mode": 0,
    "remember_volume": True,
    # ---- DSD 输出 ----
    # DSD 输出模式：auto（自动按 DAC 能力）/ native（原生直通）/ dop / pcm（软解）
    "dsd_output_mode": "auto",
    # 输出设备名（PipeWire sink 名）；空=系统默认
    "output_device": "",
    # ---- 音效 ----
    "audio_effect": "off",
    # 全局 DSP 开关（Rust 内置链）
    "dsp_enabled": False,
    # 嵌入式 CamillaDSP 引擎开关（运行中可切）
    "camilla_enabled": False,
    # ---- 可视化 ----
    "viz_fps": 60,
    "viz_fft_window": 1024,
    "viz_bin_count": 32,
    "viz_hop": 512,
    "viz_enabled": True,
    "viz_style": "bars",
    "viz_fall_speed": 0.35,
    "viz_rise_speed": 0.6,
    "viz_db_floor": -60.0,
    "viz_scale_mode": "freq",
    "viz_sync_delay_ms": 100.0,
    # ---- 外观 ----
    "theme": "system",
    "row_density": "normal",
    # 界面语言：system（跟随系统）/ zh（中文）/ en（英文）
    "language": "system",
    # 沉浸页背景：开启后使用封面模糊图铺满背景（关闭则用纯色）
    "nowplaying_blur_bg": True,
    # 模糊强度（越小越糊）
    "nowplaying_blur_px": 6,
    # 背景亮度自适应阈值（低于此亮度时前景切亮色）
    "nowplaying_dark_threshold": 0.65,
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
    """配置对象：所有读取带默认值兜底，写入失败不影响主流程。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()
        self._data: Dict[str, Any] = dict(DEFAULT_CONFIG)
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
                self._data.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("读取配置失败，使用默认值: %s", exc)

    def save(self) -> None:
        try:
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
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._data.get(key, default))

    def set_bool(self, key: str, value: bool) -> None:
        self._data[key] = bool(value)
        self.save()

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._data.get(key, default))
        except (TypeError, ValueError):
            return default

    def set_int(self, key: str, value: int) -> None:
        self._data[key] = int(value)
        self.save()

    def get_str(self, key: str, default: str = "") -> str:
        val = self._data.get(key, default)
        return val if isinstance(val, str) else default

    def set_str(self, key: str, value: str) -> None:
        self._data[key] = value
        self.save()


_instance: AppConfig | None = None


def get_config() -> AppConfig:
    global _instance
    if _instance is None:
        _instance = AppConfig()
    return _instance
