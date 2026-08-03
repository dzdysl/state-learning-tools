# 实验性重复循环 ngKSI 推断

`infer_cycle_ngksi_regions.py` 在配置的 AMF 下行 KSI 字段之间建立类型化、按时序排序的区域，
并在 schema v3 中将区域证据拆分为具体 DOT 边候选。它是候选发现工具，不是 AMF 实现变量或更新点的证明。

```powershell
& D:\anaconda3\python.exe .\infer_cycle_ngksi_regions.py `
  --config <region-inference.yaml> --output <result.json>
```

Minimal configuration:

```yaml
schema_version: 3
inputs:
  dot: hypothesis.dot
  trace: statelearner_trace.jsonl
  cycle_cover: cycle_cover.json
  sequence_file: cycle_repeat.seq
mapping:
  downlink_ksi_by_output:
    authenticationRequest: downlink_side.fields.auth_request_ksi_value
    securityModeCommand: downlink_side.fields.smc_ksi_value
  numeric_input_definitions:
    - id: registration_ngksi
      input_register_id: ngksi_uplink
      path: ue_side.fields.registration_ksi_value
      value_type: integer
      match:
        input_symbols:
          - registrationRequest
          - registrationRequestGUTI
      phase: before_register_updates
  signal_definitions:
    - id: initial_uplink_context
      path: ue_side.fields.isInitMsg
      value_type: boolean
      match:
        input_symbols:
          - registrationRequest
          - registrationRequestGUTI
      phase: before_numeric_inputs
  d_states: []
analysis:
  repetitions: [2, 10]
  min_consecutive_support: 3
  max_numeric_depth: 1
  max_derived_signal_depth: 1
```

第 1 轮最后一个 KSI 下行锚定第 2 轮的第一个区域。schema v3 将第 2 轮作为输入寄存器
初始化：同一 `input_register_id` 的最后一个事件值覆盖先前值，且先于该边的 `r` 更新写入；第 3
轮开始拟合。原始区域、覆盖链和每条边后的有效快照均写入结果，因此覆盖不会丢失消息来源。

单边区域的精确已观察候选标为 `relatively_stable_candidate`；跨多边区域的“前序最简、末端拟合”结果
标为 `hypothetical_candidate`。无下行锚点但有信号的前序边使用 `ite(s=1,unknown,r)`，输入寄存器仍按
赋值或保持更新。信号槽构成外层 `signal_guard`，恒定信号的未观察分支仍显式为 `unknown`。叶子候选为
`r'=c`、`r'=r+k` 与 `r'=r_i+k`；仅在叶子失败后尝试固定回绕形式 `ite(x<T,f,0)`，其后才尝试受支持的
输入寄存器特殊值分裂。整数 `7` 不自动获得协议语义。

对假设性边，结果的 `candidates` 继续严格表示所有已对齐样本的全局精确交集；
`hypothetical_reconciliation` 另按循环保存局部候选、`intersection_candidates`、
`non_consensus_candidates` 与 `observational_conflicts`。前两类差异不能被混入全局候选；若完全相同的
类型化观察（含字段身份、出现序号、信号、输入寄存器和值）得到不同的 `r_after`，则冲突证据必须完整保留。
