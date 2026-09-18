"""Provider 注册与动态发现。

设计目标：
- UI 只通过本模块获取 Provider 实例，不直接 import 具体实现。
- 使用动态导入，删除在线音源 provider 后本地功能不受任何影响。
"""
from __future__ import annotations

import importlib
import logging
from typing import Dict, List, Optional, Type

from .base import BaseMusicProvider

log = logging.getLogger(__name__)

# 候选 Provider 模块；import 失败会被静默跳过（解耦要求）
_PROVIDER_MODULES = [
    ("providers.local", "LocalProvider"),
]

_registry: Dict[str, Type[BaseMusicProvider]] = {}


def _discover() -> None:
    if _registry:
        return
    for module_name, class_name in _PROVIDER_MODULES:
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            log.info("跳过 Provider %s.%s: %s", module_name, class_name, exc)
            continue
        _registry[cls.source_type] = cls


def available_source_types() -> List[str]:
    _discover()
    return list(_registry.keys())


def create_provider(source_type: str) -> Optional[BaseMusicProvider]:
    _discover()
    cls = _registry.get(source_type)
    if cls is None:
        log.warning("未找到 Provider: %s", source_type)
        return None
    return cls()


__all__ = [
    "BaseMusicProvider",
    "available_source_types",
    "create_provider",
]
