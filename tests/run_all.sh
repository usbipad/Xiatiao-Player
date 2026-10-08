#!/usr/bin/env bash
# 一键运行全部 Python 测试（重构安全网）。
#
# 用法：bash tests/run_all.sh
# 退出码 0=全过 / 非 0=有失败（可直接用于 CI）。
#
# 覆盖：冒烟 / 边界单元 / DspState / IPC 契约。
# 注：integration_api.py 需要真实 Subsonic 服务端，默认不跑；
#     需要时单独执行 `python3 tests/integration_api.py`（不可达自动跳过）。
set -u
cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python3}"
FAIL=0

for t in smoke_test test_boundary test_dsp_state test_ipc_protocol test_online_lyrics test_replaygain_apply test_seekbar test_dsp_leak_fixed test_peq_leak test_basic_page_leak test_adv_window_leak test_settings_leak test_viz_window_leak test_viz_thread_leak; do
    echo "=== $t ==="
    if ! "$PY" "tests/$t.py"; then
        FAIL=1
    fi
done

echo
echo "=========================================="
if [ "$FAIL" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi
exit "$FAIL"
