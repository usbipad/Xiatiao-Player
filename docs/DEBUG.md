# 调试指南

出问题时如何开启诊断日志、收集信息。

---

## 快速开始

    # 一键调试启动（DEBUG 级别，过滤 GTK 噪音）
    bash tools/debug_run.sh

    # 更啰嗦（GTK 噪音也保留，排查 GTK 层问题时用）
    bash tools/debug_run.sh verbose

启动后日志同时输出到终端和文件。

---

## 日志位置

| 日志 | 路径 | 内容 |
|---|---|---|
| 前端（Python） | /tmp/xiatiao-debug.log | UI / 播放控制 / 配置 / 系统集成 |
| 后端（Rust） | /tmp/xiatiao-backend.log | 解码 / DSP / 音频输出 |

常规启动（不调试）时，前端日志在 /tmp/xiatiao-app.log。

---

## 常用排查命令

    # 实时跟踪前端日志
    tail -f /tmp/xiatiao-debug.log

    # 只看错误与异常堆栈
    grep -nE 'ERROR|Traceback|WARNING' /tmp/xiatiao-debug.log

    # 看最近的异常（含上文）
    grep -n -B2 -A10 'Traceback' /tmp/xiatiao-debug.log

    # 后端日志
    tail -50 /tmp/xiatiao-backend.log

---

## 环境变量

| 变量 | 说明 |
|---|---|
| XIATIAO_DEBUG | 调试开关。1=DEBUG 级别；verbose=DEBUG 且不过滤 GTK 噪音；未设置/0/false=常规 INFO |
| XIATIAO_BACKEND_SOCKET | 后端 socket 路径（调试脚本默认用独立 socket，避免与运行中实例冲突） |
| XIATIAO_VIZ_DEBUG | 可视化旁路调试输出 |
| XIATIAO_DSP_DEBUG | DSP 调试信息 |

也可手动开（不借助脚本）：

    XIATIAO_DEBUG=1 python3 main.py

---

## 常见问题定位

功能无声消失（如歌词不显示、封面不更新）
这类问题多因某处 except: pass 吞掉了异常，中断了后续 UI 更新。
用 DEBUG 模式复现，然后：

    grep -nE '失败|未更新|Traceback' /tmp/xiatiao-debug.log

关键路径（切歌资产应用、队列刷新等）已补 log.debug(..., exc_info=True)，
DEBUG 模式下会记录完整堆栈。

静态检查未定义变量

    python3 tools/audit_undef.py ui core models services providers config main.py
