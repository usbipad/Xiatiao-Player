"""DSP 音效预设的存储与管理。

存储位置：~/.config/xiatiao/effects.json
结构：
    {
      "presets": [
        {"name": "耳机空间", "builtin": true, "type": "headphone", "params": {...}},
        {"name": "音箱空间", "builtin": true, "type": "speaker", "params": {...}},
        {"name": "我的音效", "builtin": false, "type": "headphone", "params": {...}}
      ],
      "current": "耳机空间"
    }

内置预设不可编辑/删除；自定义预设可编辑、可删除。
新建预设时，以"当前预设的参数"为起点复制一份。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
FILENAME = "effects.json"

#: 默认参数（关闭态）
def _default_params() -> Dict[str, Any]:
    return {
        "enabled": False,
        "pre_gain_db": 0.0,
        "output_gain_db": 0.0,
        "crossfeed_amount": 0.0,
        "crossfeed_delay_ms": 0.3,
        "crossfeed_hf_damp": 0.5,
        "bass_boost_db": 0.0,
        "stereo_width": 1.0,
        "hrtf_enabled": False,
        "hrtf_amount": 1.0,
    }


def _headphone_params() -> Dict[str, Any]:
    p = _default_params()
    p.update({
        "enabled": True,
        "crossfeed_amount": 0.45,
        "crossfeed_delay_ms": 0.3,
        "crossfeed_hf_damp": 0.6,
        "bass_boost_db": 2.0,
        "hrtf_enabled": False,
        "hrtf_amount": 1.0,
    })
    return p


def _speaker_params() -> Dict[str, Any]:
    p = _default_params()
    p.update({
        "enabled": True,
        "crossfeed_amount": 0.0,
        "stereo_width": 1.3,
        "bass_boost_db": 2.0,
        "hrtf_enabled": False,
        "hrtf_amount": 1.0,
    })
    return p


def builtin_presets() -> List[Dict[str, Any]]:
    return [
        {"name": "耳机空间", "builtin": True, "type": "headphone", "params": _headphone_params()},
        {"name": "音箱空间", "builtin": True, "type": "speaker", "params": _speaker_params()},
    ]


class DspPresetStore:
    """DSP 预设存取。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()
        self._data: Dict[str, Any] = {"presets": [], "current": None}
        self.load()

    @staticmethod
    def _default_path() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        return Path(base) / APP_DIR_NAME / FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self._data = {"presets": [], "current": None}
        if self._path.is_file():
            try:
                with self._path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("读取音效预设失败: %s", exc)

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存音效预设失败: %s", exc)

    # ---- 查询 ----

    def presets(self) -> List[Dict[str, Any]]:
        """返回全部预设：内置在前，自定义在后。"""
        builtin = builtin_presets()
        custom = [p for p in self._data.get("presets", []) if isinstance(p, dict)]
        return builtin + custom

    def current(self) -> Optional[Dict[str, Any]]:
        name = self._data.get("current")
        for p in self.presets():
            if p.get("name") == name:
                return p
        # 默认第一个内置
        allp = self.presets()
        return allp[0] if allp else None

    def current_name(self) -> str:
        cur = self.current()
        return cur.get("name") if cur else ""

    def set_current(self, name: str) -> None:
        self._data["current"] = name
        self.save()

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        for p in self.presets():
            if p.get("name") == name:
                return p
        return None

    # ---- 自定义预设增删改 ----

    def _custom_list(self) -> List[Dict[str, Any]]:
        return [p for p in self._data.get("presets", []) if isinstance(p, dict)]

    def add_custom(self, name: str, ptype: str, params: Dict[str, Any]) -> None:
        """新增自定义预设（name 需唯一）。"""
        custom = self._custom_list()
        base = name
        i = 1
        existing = {p.get("name") for p in self.presets()}
        while name in existing:
            i += 1
            name = f"{base} {i}"
        custom.append({
            "name": name,
            "builtin": False,
            "type": ptype,
            "params": dict(params),
        })
        self._data["presets"] = custom
        self._data["current"] = name
        self.save()

    def update_custom(self, name: str, params: Dict[str, Any]) -> None:
        custom = self._custom_list()
        for p in custom:
            if p.get("name") == name and not p.get("builtin"):
                p["params"] = dict(params)
                break
        self._data["presets"] = custom
        self.save()

    def remove_custom(self, name: str) -> bool:
        custom = self._custom_list()
        new = [p for p in custom if p.get("name") != name]
        if len(new) == len(custom):
            return False
        self._data["presets"] = new
        if self._data.get("current") == name:
            self._data["current"] = None
        self.save()
        return True


_instance: Optional[DspPresetStore] = None


def get_dsp_store() -> DspPresetStore:
    global _instance
    if _instance is None:
        _instance = DspPresetStore()
    return _instance
