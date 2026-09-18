"""把内置音效预设转成 Camilla YAML，输出到 /tmp/xiatiao_presets/ 供频响测试。

用法：python3 tools/dump_preset_yamls.py
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.eq_presets import BUILTIN_PRESETS  # noqa: E402
from core import camilla  # noqa: E402

OUT = "/tmp/xiatiao_presets"
os.makedirs(OUT, exist_ok=True)

index = []
for p in BUILTIN_PRESETS:
    name = p["name"]
    params = dict(p["params"])
    try:
        cfg = camilla.build_config(params, 48000)
        y = camilla.to_yaml(cfg)
    except Exception as exc:
        print(f"跳过 {name}: {exc}")
        continue
    safe = name.replace("/", "_")
    path = os.path.join(OUT, f"{safe}.yaml")
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(y)
    index.append(safe)

with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fp:
    json.dump(index, fp, ensure_ascii=False)

print(f"已生成 {len(index)} 个预设 YAML 到 {OUT}")
for n in index:
    print(" ", n)
