# Rust dead_code 警告逐项清单 — 2026-09-20

`cargo build --release` 共 17 条警告。按「性质」分类，供逐项处理。

- 状态：`[ ]` 待处理 / `[x]` 已处理
- 风险：低 / 中 / 高

---

## A. 孤立项（可直接删，零连带）

| # | 位置 | 警告 | 说明 | 风险 | 状态 |
|---|---|---|---|---|---|
| 1 | dsp/params.rs:199 | enum Owner never used | 整个枚举无人引用 | 低 | [ ] |
| 2 | shared.rs:80 | field dsp_enabled never read | 仅 engine.rs:66 初始化，从不读取 | 低 | [ ] |
| 3 | output.rs:65 | const MAX_CHANNELS never used | 本地常量；614 行用的是另一个 spa 同名常量 | 低 | [ ] |
| 4 | output.rs:125 | method available never used | ring buffer 辅助方法，无调用 | 低 | [ ] |
| 5 | camilla_engine.rs:102 | field channels never read | 结构体字段存了不用 | 低 | [ ] |
| 6 | camilla_engine.rs:176 | method has_pipeline never used | 无调用 | 低 | [ ] |

---

## B. 仅测试用（非测试构建下为死代码）

| # | 位置 | 警告 | 说明 | 风险 | 状态 |
|---|---|---|---|---|---|
| 7 | ir_resample.rs:30 | fn read_wav_info never used | 仅被 cfg(test) 模块调用 | 低 | [ ] |
| 8 | ir_resample.rs:138 | fn resample never used | 仅被测试调用 | 低 | [ ] |

处理建议：加 #[cfg(test)] 或 #[allow(dead_code)]（保留测试可用）。

---

## C. 连锁预留（成组，删需一起动）

同一功能链的预留接口，单独删会破坏编译或语义：

| # | 位置 | 警告 | 关联 | 状态 |
|---|---|---|---|---|
| 9 | dsp/mod.rs:149 | process_split / process_interleaved never used | 分阶段 DSP 处理入口 | [ ] |
| 10 | dsp/params.rs:16 | enum DspStage never used | 仅被 #9 使用（Pre/Post 阶段） | [ ] |
| 11 | shared.rs:10 | const OUTPUT_LATENCY_SECS never used | 仅被 #12 使用 | [ ] |
| 12 | shared.rs:91 | method position_secs never used | 用 #11 做延迟补偿 | [ ] |

处理建议：若确认「分阶段处理」「延迟补偿」短期内不做，可整组删除；否则保留并加注释。

---

## D. output 层 latency / is_failed 系列

| # | 位置 | 警告 | 状态 |
|---|---|---|---|
| 13 | output.rs:296 | set_output_device / latency never used（PipewireOutput） | [ ] |
| 14 | output.rs:752 | latency / is_failed never used（AudioOut 包装） | [ ] |
| 15 | output_alsa.rs:573 | latency never used（AlsaOutput） | [ ] |

处理建议：需人工确认。AudioOut::latency 是「分发到各后端」的包装方法，
若确实无人调用可整组删；若将来要做延迟显示/失败检测，则保留。

---

## E. 协议预留

| # | 位置 | 警告 | 说明 | 状态 |
|---|---|---|---|---|
| 16 | protocol.rs:22 | field camilla never read | SetEngine{camilla} 字段；HANDOVER 记 SetEngine 仅占位 | [ ] |
| 17 | protocol.rs:45 | variants Ready / AudioInfo never constructed | 后端从不发送 | [ ] |

注意：#17 的 AudioInfo 虽后端不发，但 Python 侧 rust_backend.py:332 已实现接收处理。
删除前需确认该分支是否一并清理，或保留为协议契约。

---

## 处理优先级建议

1. A 组（6 项）：可直接删，收益明确、风险最低。
2. B 组（2 项）：加 #[cfg(test)] 即可消警告。
3. C/D/E 组：属预留接口，删前需确认产品方向；建议先加 #[allow(dead_code)]
   并注明用途，或维护在本清单中，暂不删。

---

## 附：完整警告原文

    warning: field `channels` is never read                          --> src/camilla_engine.rs:102:5
    warning: method `has_pipeline` is never used                     --> src/camilla_engine.rs:176:12
    warning: methods `process_split` and `process_interleaved` never used --> src/dsp/mod.rs:149:12
    warning: enum `DspStage` is never used                           --> src/dsp/params.rs:16:10
    warning: enum `Owner` is never used                              --> src/dsp/params.rs:199:10
    warning: constant `OUTPUT_LATENCY_SECS` never used               --> src/shared.rs:10:18
    warning: field `dsp_enabled` never read                          --> src/shared.rs:80:16
    warning: method `position_secs` never used                       --> src/shared.rs:91:19
    warning: constant `MAX_CHANNELS` never used                      --> src/output.rs:65:7
    warning: method `available` never used                           --> src/output.rs:125:8
    warning: methods `set_output_device` and `latency` never used    --> src/output.rs:296:12
    warning: methods `latency` and `is_failed` never used            --> src/output.rs:752:12
    warning: method `latency` never used                             --> src/output_alsa.rs:573:12
    warning: function `read_wav_info` never used                     --> src/ir_resample.rs:30:8
    warning: function `resample` never used                          --> src/ir_resample.rs:138:4
    warning: field `camilla` never read                              --> src/protocol.rs:22:17
    warning: variants `Ready` and `AudioInfo` never constructed      --> src/protocol.rs:45:5
