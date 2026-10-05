"""IPC 契约常量：Python 侧与 Rust 后端（audio_backend_rs/src/protocol.rs）
通信的「命令字 / 事件名 / 字段名」唯一定义处。

背景：此前这些字符串以裸字面量散落在 core/rust_backend.py 各处，
与 Rust 侧的 enum 靠 serde 的 rename_all="snake_case" 隐式对齐——
改字/加字段时没有任何校验，失配是静默的（命令丢弃只 warning）。

本模块把契约集中到一处：
  - 改命令字只需改这里；
  - 与 Rust protocol.rs 的 enum 变体一一对应，便于人工核对；
  - 测试可据此校验「Python 发/收的字面量都在契约内」。

对齐规则（Rust 侧 serde）：
  - Request 枚举 tag="cmd"，rename_all="snake_case"：
      SetDsp { params } -> {"cmd":"set_dsp","params":{...}}
  - Event 枚举 tag="event"，rename_all="snake_case"：
      AudioInfo { info } -> {"event":"audio_info","info":{...}}

维护提示：新增命令/事件时，**同时**更新本文件与 protocol.rs，
并在 tests/test_ipc_protocol.py 中补一条断言。
"""
from __future__ import annotations


class Cmd:
    """客户端 -> 后端 的命令字（对应 Rust protocol.rs::Request）。"""

    PING = "ping"
    PLAY = "play"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SEEK = "seek"
    SET_VOLUME = "set_volume"
    SET_EFFECT = "set_effect"
    SET_DSP = "set_dsp"
    SET_CAMILLA_YAML = "set_camilla_yaml"
    SET_COLORING = "set_coloring"
    RESET_DSP = "reset_dsp"
    SET_DSD_MODE = "set_dsd_mode"
    SET_OUTPUT_DEVICE = "set_output_device"
    LIST_OUTPUT_DEVICES = "list_output_devices"
    QUERY_STATE = "query_state"
    SHUTDOWN = "shutdown"

    #: 全部命令字集合（供测试校验无遗漏/无越界）。
    ALL = frozenset({
        PING, PLAY, PAUSE, RESUME, STOP, SEEK, SET_VOLUME, SET_EFFECT,
        SET_DSP, SET_CAMILLA_YAML, SET_COLORING, RESET_DSP, SET_DSD_MODE,
        SET_OUTPUT_DEVICE, LIST_OUTPUT_DEVICES, QUERY_STATE, SHUTDOWN,
    })


class Evt:
    """后端 -> 客户端 的事件名（对应 Rust protocol.rs::Event）。"""

    READY = "ready"
    PONG = "pong"
    ACK = "ack"
    ERROR = "error"
    POSITION = "position"
    DURATION = "duration"
    STATE = "state"
    END_OF_STREAM = "end_of_stream"
    AUDIO_INFO = "audio_info"
    EFFECT = "effect"
    DSP = "dsp"
    OUTPUT_DEVICES = "output_devices"

    #: 全部事件名集合（供测试校验）。
    ALL = frozenset({
        READY, PONG, ACK, ERROR, POSITION, DURATION, STATE,
        END_OF_STREAM, AUDIO_INFO, EFFECT, DSP, OUTPUT_DEVICES,
    })


class Field:
    """IPC 报文里常用的字段名（避免裸字符串拼写漂移）。"""

    #: 请求 tag
    CMD = "cmd"
    #: 事件 tag
    EVENT = "event"
    #: 通用载荷字段
    PATH = "path"
    SECONDS = "seconds"
    VALUE = "value"
    PRESET = "preset"
    PARAMS = "params"
    YAML = "yaml"
    TUBE_DRIVE = "tube_drive"
    BBE_AMOUNT = "bbe_amount"
    MODE = "mode"
    NAME = "name"
    #: 事件载荷字段
    SEC = "sec"
    GEN = "gen"
    STATE = "state"
    MESSAGE = "message"
    INFO = "info"
    DEVICES = "devices"
    ID = "id"
    DESCRIPTION = "description"
