#!/usr/bin/env bash
# 虾条播放器 — 调试启动入口
#
# 一键开启全量诊断日志启动应用，出问题时用它复现并收集信息，
# 无需改代码。日志同时输出到终端与文件。
#
# 用法：
#   bash tools/debug_run.sh            # DEBUG 级别，过滤 GTK 噪音
#   bash tools/debug_run.sh verbose    # 更啰嗦：GTK 噪音也保留
#
# 产物：
#   /tmp/xiatiao-debug.log      前端（Python）日志
#   /tmp/xiatiao-backend.log    后端（Rust）日志
#
# 说明：脚本只是设置 XIATIAO_DEBUG 环境变量后运行 main.py，
#       核心逻辑在 main.py 的「调试入口」段，普通启动不受影响。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LEVEL="${1:-1}"
export XIATIAO_DEBUG="$LEVEL"

# 每次调试用独立 socket，避免与正在运行的实例抢占同一后端。
export XIATIAO_BACKEND_SOCKET="${XIATIAO_BACKEND_SOCKET:-/tmp/xiatiao-debug-backend.sock}"

cat <<EOF
============================================================
 虾条播放器 调试模式
------------------------------------------------------------
 XIATIAO_DEBUG            = $LEVEL
 XIATIAO_BACKEND_SOCKET   = $XIATIAO_BACKEND_SOCKET
 前端日志                  = /tmp/xiatiao-debug.log
 后端日志                  = /tmp/xiatiao-backend.log
------------------------------------------------------------
 退出应用后查看日志：  tail -f /tmp/xiatiao-debug.log
 仅看错误/异常：       grep -E 'ERROR|Traceback|WARNING' /tmp/xiatiao-debug.log
============================================================
EOF

exec python3 main.py
