"""Provider 注册与动态发现。

设计目标：
- UI 只通过本模块获取 Provider 实例，不直接 import 具体实现。
- 使用动态导入，删除在线音源 provider 后本地功能不受任何影响。

合规边界（重要）：
- 内置 Provider 只有两个：本地曲库（local）与标准 Subsonic 客户端（subsonic）。
- 本软件【不内置】任何国内音乐平台的接口解析、登录态或音源获取逻辑。
- 若用户希望接入其它音源，应由用户【自行提供】符合 Subsonic 协议的
  服务端，或自行提供第三方 Provider 模块（通过下方外挂机制加载）。
  这些外挂模块不随本软件分发，其合规性由提供方与使用者自负。

外挂 Provider 加载机制（供高级用户/第三方）：
- 环境变量 XIATIAO_PROVIDER_MODULES：形如 "my_pkg.my_provider:MyProvider;other.mod:Cls"，
  分号分隔多个，冒号分隔模块与类名。
- 环境变量 XIATIAO_PROVIDER_PATH：额外加入 sys.path 的目录（分号分隔），
  便于把插件放在任意位置而无需安装成包。
- 上述模块 import 失败会被静默跳过（与内置一致），绝不影响主程序启动。
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Dict, List, Optional, Tuple, Type

from .base import BaseMusicProvider

log = logging.getLogger(__name__)

#: 内置候选 Provider 模块；import 失败会被静默跳过（解耦要求）。
#: 只含合规的本地曲库与标准 Subsonic 客户端，不含任何平台私有实现。
_BUILTIN_PROVIDER_MODULES: List[Tuple[str, str]] = [
    ("providers.local", "LocalProvider"),
    ("providers.subsonic", "SubsonicProvider"),
]

#: 外挂 Provider 环境变量名。
_ENV_MODULES = "XIATIAO_PROVIDER_MODULES"
_ENV_PATH = "XIATIAO_PROVIDER_PATH"

_registry: Dict[str, Type[BaseMusicProvider]] = {}
_discovered = False


def _apply_extra_path() -> None:
    """把 XIATIAO_PROVIDER_PATH 中的目录加入 sys.path（供外挂插件导入）。

    分号分隔；空项忽略；已存在项跳过。绝不修改已有 sys.path 顺序之外的语义。
    """
    raw = os.environ.get(_ENV_PATH, "")
    if not raw:
        return
    for part in raw.split(";"):
        d = part.strip()
        if not d:
            continue
        try:
            if os.path.isdir(d) and d not in sys.path:
                sys.path.append(d)
                log.info("已加入外挂 Provider 搜索路径: %s", d)
        except Exception:
            log.debug("加入外挂路径失败: %s", d, exc_info=True)


def _external_module_specs() -> List[Tuple[str, str]]:
    """解析 XIATIAO_PROVIDER_MODULES 为 (module, class) 列表。

    格式："mod:Cls;mod2:Cls2"。缺少类名或格式非法的项被跳过。
    """
    raw = os.environ.get(_ENV_MODULES, "")
    specs: List[Tuple[str, str]] = []
    if not raw:
        return specs
    for part in raw.split(";"):
        item = part.strip()
        if not item or ":" not in item:
            continue
        mod, cls = item.split(":", 1)
        mod, cls = mod.strip(), cls.strip()
        if mod and cls:
            specs.append((mod, cls))
    return specs


def _try_register(module_name: str, class_name: str) -> None:
    """尝试导入并注册单个 Provider 类；失败静默跳过。

    注册键为 cls.source_type；重复 source_type 时后者覆盖前者并记录警告，
    便于用户用外挂替换内置（例如用自己的 subsonic 增强实现）。
    """
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        log.info("跳过 Provider %s.%s: %s", module_name, class_name, exc)
        return
    try:
        st = str(getattr(cls, "source_type", "") or "")
        if not st or st == "base":
            log.info("跳过 Provider %s.%s: 未声明有效 source_type", module_name, class_name)
            return
        if st in _registry:
            log.warning("Provider source_type=%s 被外挂覆盖: %s.%s",
                        st, module_name, class_name)
        _registry[st] = cls
    except Exception:
        log.debug("注册 Provider 失败: %s.%s", module_name, class_name, exc_info=True)


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True
    # 1) 先把外挂路径加入 sys.path，供后续导入。
    _apply_extra_path()
    # 2) 内置 Provider（合规：local + 标准 subsonic）。
    for module_name, class_name in _BUILTIN_PROVIDER_MODULES:
        _try_register(module_name, class_name)
    # 3) 用户外挂 Provider（不随包分发，合规性由提供方/使用者自负）。
    for module_name, class_name in _external_module_specs():
        _try_register(module_name, class_name)


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
