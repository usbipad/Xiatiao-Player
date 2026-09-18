"""DSP 预设存取。

存储：~/.config/xiatiao/dsp_presets.json
结构：
  { "presets": { "预设名": {参数...}, ... }, "current": "预设名" }
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

APP_DIR_NAME = "xiatiao"
FILENAME = "dsp_presets.json"


class DspPresetStore:
    """DSP 预设存取。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self._default_path()
        self._data: Dict[str, Any] = {"presets": {}, "current": None}
        self.load()

    @staticmethod
    def _default_path() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        return Path(base) / APP_DIR_NAME / FILENAME

    def load(self) -> None:
        self._data = {"presets": {}, "current": None}
        if self._path.is_file():
            try:
                with self._path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("读取 DSP 预设失败: %s", exc)

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("保存 DSP 预设失败: %s", exc)

    def names(self) -> List[str]:
        return sorted(self._data.get("presets", {}).keys())

    def current(self) -> Optional[str]:
        return self._data.get("current")

    def set_current(self, name: str) -> None:
        self._data["current"] = name
        self.save()

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        presets = self._data.get("presets", {})
        p = presets.get(name)
        return dict(p) if isinstance(p, dict) else None

    def put(self, name: str, params: Dict[str, Any]) -> None:
        """保存/覆盖预设。"""
        self._data.setdefault("presets", {})[name] = dict(params)
        self._data["current"] = name
        self.save()

    def remove(self, name: str) -> bool:
        presets = self._data.get("presets", {})
        if name in presets:
            del presets[name]
            if self._data.get("current") == name:
                self._data["current"] = None
            self.save()
            return True
        return False


_instance: Optional[DspPresetStore] = None


def get_dsp_preset_store() -> DspPresetStore:
    global _instance
    if _instance is None:
        _instance = DspPresetStore()
    return _instance
