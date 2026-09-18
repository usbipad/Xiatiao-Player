"""把 DSP 参数字典转成 CamillaDSP 配置（YAML）。

与 audio_backend_rs/src/dsp.rs 的 DspParams 语义对应：
  eq_gains[10]        → 10 × Biquad Peaking
  peq_bands[]         → Biquad Peaking/Lowshelf/Highshelf
  bass_gain_db/freq   → Biquad Lowshelf
  treble_gain_db/freq → Biquad Highshelf
  pre_gain_db         → Gain
  balance             → Mixer（左右增益）
  stereo_width        → Mixer（M/S 矩阵）
  compressor_*        → Compressor（processor）
  limiter_*           → Limiter
  loudness_*          → Loudness
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: 与 Rust 侧 EQ_FREQS 一致（10 段图形 EQ 中心频率）
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def _mapping_entry(dest: int, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"dest": dest, "sources": sources}


def _src(channel: int, gain_db: float, inverted: bool = False) -> Dict[str, Any]:
    return {
        "channel": channel,
        "gain": float(gain_db),
        "inverted": bool(inverted),
        "scale": "dB",
    }


def build_config(params: Dict[str, Any], samplerate: int,
                 channels: int = 2, sink: str = "") -> Dict[str, Any]:
    """把 DSP 参数转成 CamillaDSP 配置字典。

    params 缺省/关闭的模块不会出现在 filters/pipeline 里。
    """
    p = params or {}
    filters: Dict[str, Any] = {}
    processors: Dict[str, Any] = {}
    mixers: Dict[str, Any] = {}
    steps: List[Dict[str, Any]] = []
    filter_names: List[str] = []

    dsp_on = bool(p.get("enabled", False))
    if not dsp_on:
        # 全局关闭：仍需一个直通步骤，否则 camilladsp 空管线不出声
        filters["pass"] = {"type": "Gain", "parameters": {"gain": 0.0, "scale": "dB"}}
        steps.append({"type": "Filter", "channels": list(range(channels)),
                      "names": ["pass"]})
        return _wrap(samplerate, channels, sink, filters, processors, mixers, steps, 1024)

    # ---- 预增益（用户设定 + 自动 headroom）----
    # 用户显式设定
    pre = float(p.get("pre_gain_db", 0.0) or 0.0)
    # 自动 headroom：扫描 EQ/PEQ/低音/高音的「正向提升」，预留余量防削波
    boosts = []
    if p.get("eq_enabled"):
        boosts += [float(g) for g in (p.get("eq_gains") or []) if float(g) > 0]
    if p.get("peq_enabled"):
        boosts += [float(b.get("gain", 0.0)) for b in (p.get("peq_bands") or []) if float(b.get("gain", 0.0)) > 0]
    if p.get("bass_enabled"):
        boosts += [float(p.get("bass_gain_db", 0.0) or 0.0)]
    boosts += [float(p.get("treble_gain_db", 0.0) or 0.0)]
    max_boost = max(boosts) if boosts else 0.0
    # 温和 headroom：只在提升较大时补，且最多补 2dB，避免吃掉音效效果
    auto_headroom = 0.0
    if max_boost > 3.0:
        auto_headroom = -min(max_boost - 2.0, 2.0)
    total_pre = pre + auto_headroom
    if abs(total_pre) > 1e-6:
        filters["pre_gain"] = {
            "type": "Gain",
            "parameters": {"gain": total_pre, "scale": "dB"},
        }
        filter_names.append("pre_gain")

    # ---- 10 段图形 EQ ----
    if p.get("eq_enabled"):
        gains = list(p.get("eq_gains") or [])
        for i, f in enumerate(EQ_FREQS):
            g = float(gains[i]) if i < len(gains) else 0.0
            if abs(g) < 1e-6:
                continue
            name = f"eq_{i}"
            eq_q = max(0.1, float(p.get("eq_q", 1.0) or 1.0))
            filters[name] = {
                "type": "Biquad",
                "parameters": {"type": "Peaking", "freq": float(f),
                                "gain": g, "q": eq_q},
            }
            filter_names.append(name)

    # ---- PEQ ----
    if p.get("peq_enabled"):
        for i, band in enumerate(p.get("peq_bands") or []):
            kind = str(band.get("kind", "pk"))
            btype = {"pk": "Peaking", "ls": "Lowshelf", "hs": "Highshelf"}.get(kind, "Peaking")
            name = f"peq_{i}"
            filters[name] = {
                "type": "Biquad",
                "parameters": {
                    "type": btype,
                    "freq": float(band.get("freq", 1000.0)),
                    "gain": float(band.get("gain", 0.0)),
                    "q": max(0.1, float(band.get("q", 1.0))),
                },
            }
            filter_names.append(name)

    # ---- 卷积 IR（单 IR，Wav，CamillaDSP 官方标准）----
    # 注意：camillalib 对不存在的 IR 文件会 panic，UI 侧须先校验路径存在
    _conv_steps = []   # 卷积的 Filter 步骤（可能 1 或 2 个）
    if p.get("convolution_enabled"):
        ir = str(p.get("convolution_ir") or "").strip()
        if ir:
            stereo_ir = bool(p.get("convolution_stereo_ir", False))
            n_ch = _ir_channel_count(ir)
            if stereo_ir and n_ch >= 2:
                # 立体声 IR：左声道用第 0 路、右声道用第 1 路
                filters["ir_l"] = {
                    "type": "Conv",
                    "parameters": {"type": "Wav", "filename": ir, "channel": 0},
                }
                filters["ir_r"] = {
                    "type": "Conv",
                    "parameters": {"type": "Wav", "filename": ir, "channel": 1},
                }
                _conv_steps.append({"type": "Filter", "channels": [0], "names": ["ir_l"]})
                _conv_steps.append({"type": "Filter", "channels": [1], "names": ["ir_r"]})
            else:
                # 单路模式（或 IR 只有 1 路）：选一路，左右共用
                ir_params: Dict[str, Any] = {"type": "Wav", "filename": ir}
                ch = p.get("convolution_channel")
                if ch is not None:
                    try:
                        ir_params["channel"] = max(0, int(ch))
                    except (TypeError, ValueError):
                        pass
                filters["ir"] = {"type": "Conv", "parameters": ir_params}
                _conv_steps.append({"type": "Filter", "channels": list(range(channels)),
                                    "names": ["ir"]})

    # ---- 低音 / 高音 ----
    if p.get("bass_enabled") and abs(float(p.get("bass_gain_db", 0.0) or 0.0)) > 1e-6:
        filters["bass"] = {
            "type": "Biquad",
            "parameters": {"type": "Lowshelf",
                            "freq": float(p.get("bass_freq", 100.0)),
                            "gain": float(p.get("bass_gain_db", 0.0)),
                            "q": 0.7},
        }
        filter_names.append("bass")

    if abs(float(p.get("treble_gain_db", 0.0) or 0.0)) > 1e-6:
        filters["treble"] = {
            "type": "Biquad",
            "parameters": {"type": "Highshelf",
                            "freq": float(p.get("treble_freq", 8000.0)),
                            "gain": float(p.get("treble_gain_db", 0.0)),
                            "q": 0.7},
        }
        filter_names.append("treble")

    # ---- Loudness ----
    if p.get("loudness_enabled") and float(p.get("loudness_amount", 0.0) or 0.0) > 1e-6:
        amt = max(0.0, min(1.0, float(p.get("loudness_amount", 0.0))))
        # 官方参数：reference_level / high_boost / low_boost / ramp_time
        filters["loudness"] = {
            "type": "Loudness",
            "parameters": {
                "reference_level": 0.0,
                "high_boost": 10.0 * amt,
                "low_boost": 10.0 * amt,
                "ramp_time": 400,
            },
        }
        filter_names.append("loudness")

    # ---- 压缩器（processor）----
    if p.get("compressor_enabled"):
        processors["comp"] = {
            "type": "Compressor",
            "parameters": {
                "channels": channels,
                "attack": max(0.001, float(p.get("compressor_attack", 0.01) or 0.01)),
                "release": max(0.01, float(p.get("compressor_release", 0.2) or 0.2)),
                "threshold": float(p.get("compressor_threshold_db", -18.0)),
                "factor": max(1.0, float(p.get("compressor_ratio", 2.0))),
                "makeup_gain": float(p.get("compressor_makeup_db", 0.0)),
                "monitor_channels": list(range(channels)),
                "process_channels": list(range(channels)),
            },
        }

    # ---- 限幅器 ----
    if p.get("limiter_enabled"):
        # 官方：clip_limit 默认 -1.0 dB；soft_clip 默认 false（软削波会染色）
        filters["limiter"] = {
            "type": "Limiter",
            "parameters": {
                "clip_limit": float(p.get("limiter_threshold_db", -1.0)),
                "soft_clip": bool(p.get("limiter_soft_clip", False)),
            },
        }
        filter_names.append("limiter")

    # 注：立体声宽度/平衡归 Rust 处理（Camilla 的 Mixer 改参数需重建 pipeline，
    # 拖动不平滑；两者都是简单线性运算，音质一致）。此处不生成。

    # ---- 通道矩阵（立体声实用选项）----
    cm = str(p.get("channel_matrix", "off") or "off")
    if cm == "swap":
        mixers["matrix"] = {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                _mapping_entry(0, [_src(1, 0.0)]),
                _mapping_entry(1, [_src(0, 0.0)]),
            ],
        }
    elif cm == "mono":
        mixers["matrix"] = {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                _mapping_entry(0, [_src(0, -6.0), _src(1, -6.0)]),
                _mapping_entry(1, [_src(0, -6.0), _src(1, -6.0)]),
            ],
        }
    elif cm == "left_both":
        mixers["matrix"] = {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                _mapping_entry(0, [_src(0, 0.0)]),
                _mapping_entry(1, [_src(0, 0.0)]),
            ],
        }
    elif cm == "right_both":
        mixers["matrix"] = {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                _mapping_entry(0, [_src(1, 0.0)]),
                _mapping_entry(1, [_src(1, 0.0)]),
            ],
        }

    # ---- 相位控制（全局反相）----
    if p.get("phase_invert"):
        filters["phase"] = {
            "type": "Gain",
            "parameters": {"gain": 0.0, "inverted": True, "scale": "dB"},
        }
        filter_names.append("phase")

    # ---- 组装 pipeline ----
    if filter_names:
        steps.append({"type": "Filter", "channels": list(range(channels)),
                      "names": filter_names})
    # 卷积步骤（单路 1 个 / 立体声 2 个，各绑声道）
    for _st in _conv_steps:
        steps.append(_st)
    if "comp" in processors:
        steps.append({"type": "Processor", "name": "comp"})
    if "matrix" in mixers:
        steps.append({"type": "Mixer", "name": "matrix"})
    # 保底：pipeline 为空时加直通 Gain，避免 camilladsp 静音
    if not steps:
        filters["pass"] = {"type": "Gain", "parameters": {"gain": 0.0, "scale": "dB"}}
        steps.append({"type": "Filter", "channels": list(range(channels)),
                      "names": ["pass"]})

    # chunksize：官方推荐 1024（约 22ms @44.1k）。
    # 卷积不要求 chunksize == IR 长度：camillalib 的 FftConv 按 nsegments
    # 分段处理任意长度 IR；只要每块喂恰好 chunksize 帧即可（Rust 侧已缓冲保证）。
    return _wrap(samplerate, channels, sink, filters, processors, mixers, steps, 1024)


def _ir_channel_count(path: str) -> int:
    """读取 WAV/IRS 的声道数（手动解析 RIFF；wave 不支持 f32）。失败返回 0。"""
    try:
        import struct
        with open(path, "rb") as f:
            data = f.read(4096)
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return 0
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            csz = struct.unpack_from("<I", data, pos + 4)[0]
            body = pos + 8
            if cid == b"fmt ":
                if body + 16 > len(data):
                    return 0
                _afmt, ch, _rate, _br, _ba, _bits = struct.unpack_from("<HHIIHH", data, body)
                return int(ch)
            pos = body + csz + (csz & 1)
        return 0
    except Exception:
        return 0


def _ir_frame_count(path: str) -> int:
    """读取 WAV/IRS 的帧数（不依赖 PyYAML；失败返回 0）。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return int(w.getnframes())
    except Exception:
        pass
    # 兜底：按 32-bit 立体声 WAV 估算（去掉 ~44 字节头）
    try:
        import os
        sz = os.path.getsize(path)
        return max(0, (sz - 44) // 8)
    except Exception:
        return 0


def _wrap(samplerate: int, channels: int, sink: str,
          filters: Dict, processors: Dict, mixers: Dict,
          pipeline: List[Dict], chunksize: int = 1024) -> Dict[str, Any]:
    # 嵌入式 camillalib 未启用 pipewire-backend，devices 仅作占位（不参与处理，
    # 实际输出由 Rust 侧的 pw-cat 负责）。用 Stdout 保证 Configuration 可解析。
    playback: Dict[str, Any] = {"type": "Stdout", "channels": channels, "format": "F32_LE"}
    return {
        "devices": {
            "samplerate": int(samplerate),
            "chunksize": int(max(1024, chunksize)),
            "capture": {"type": "Stdin", "channels": channels, "format": "F32_LE"},
            "playback": playback,
        },
        "filters": filters,
        "processors": processors,
        "mixers": mixers,
        "pipeline": pipeline,
    }


def to_yaml(cfg: Dict[str, Any]) -> str:
    """配置字典转 YAML 字符串（不依赖 PyYAML，手写简单序列化）。"""
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    except Exception:
        return _to_yaml_manual(cfg)


def _to_yaml_manual(obj: Any, indent: int = 0) -> str:
    """极简 YAML 序列化（PyYAML 不可用时兜底）。"""
    sp = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{sp}{k}:")
                lines.append(_to_yaml_manual(v, indent + 1))
            else:
                lines.append(f"{sp}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for item in obj:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{sp}- " if first else f"{sp}  "
                    first = False
                    if isinstance(v, (dict, list)) and v:
                        lines.append(f"{prefix}{k}:")
                        lines.append(_to_yaml_manual(v, indent + 2))
                    else:
                        lines.append(f"{prefix}{k}: {_scalar(v)}")
            else:
                lines.append(f"{sp}- {_scalar(item)}")
        return "\n".join(lines)
    return _scalar(obj)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, str):
        # 需要引号的场景：空串、含特殊字符
        if v == "" or any(c in v for c in ":#{}[],&*!|>'\"%@`"):
            return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return v
    return str(v)
