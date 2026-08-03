# Register-state-machine inference workflow

类型化时序候选树的完整设计说明见
[typed-temporal-register-inference-design.md](typed-temporal-register-inference-design.md)。

`analysis/register_inference/analyze_register_machine.py` turns explicitly
mapped integer observations into a reviewable, edge-local register-update
candidate fit.  It is a first-stage inference aid, not proof of an AMF's
internal member names or source-level control flow.

## Inputs and configuration

Keep the experiment-specific YAML configuration in the record's `inputs/`
directory.  It names an immutable Mealy DOT, a frozen JSONL trace and dotted
paths for source state, target state, logical input, integer register values
before/after each transition, and optional integer input variables.  Every
trace record must map to exactly one labelled DOT transition.

The tool deliberately retains all observations, including repeated cycle
iterations.  It neither deduplicates abstract input/output rows nor silently
repairs missing, reordered or unmappable records.  Correct cycle alignment
remains a prerequisite: for repeated-cycle tests, retain the access prefix and
first iteration as setup context and map iterations 2–10 explicitly.

## KSI trajectory policy

For KSI-specific trajectory inference, record only `ue_sec_ctx_ngksi` and
fields whose names end in `_ksi_value`; exclude every `*_ksi_tsc` field from
the configuration, normalised observations, candidates and reports.
`ue_sec_ctx_ngksi` is an internal UE-side observation.  It may document whether
the UE adopted a received value, but it is not normally an AMF-visible input.
By default, do not substitute it for `registration_ksi_value` when fitting AMF
behaviour.  A concrete user request may explicitly select it for a UE-side rule
or a declared cross-side relation; label that use as UE-internal observation.
Use `registration_ksi_value` as the Registration Request value visible to the
AMF, and correlate it with downlink values such as `auth_request_ksi_value` and
`smc_ksi_value`.  Reports must label UE observations separately from AMF-facing
signal fields.

Use the bundled generic example as a starting point:

```powershell
$tool = 'D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\analyze_register_machine.py'
& D:\anaconda3\python.exe $tool run `
  --config <record>\inputs\register-inference.yaml `
  --output-dir <record>\analysis\derived\register_inference
```

`run` creates `prepared.json`, `candidates.json` and `fit.json`.  The three
subcommands can instead be run independently when comparing a new cleaner,
candidate generator or fitter.

## Initial inference contract

The built-in `simple_scalar_v1` generator may produce only these integer
update forms: `r'=r`, `r'=c`, `r'=r+k`, and `r'=i`.  The
`z3_scalar_edge_v1` fitter tests each formula against all observations for an
edge/register pair and picks the first satisfiable candidate according to the
configured priority.  An unsatisfiable group reports the failing observations.

There is intentionally no KSI-specific field name, fixed initial register
value, special DOT edge, output-equals-register assumption or automatic
access-path reconstruction.

`ite`, threshold/periodic guards, modular arithmetic, multi-register formulas,
CEGIS and inferred-model DOT rendering are reserved extension points.  Do not
interpret their absence as a negative finding.  Preserve candidate equations
and diagnostics in the record, then correlate a promising candidate with the
exact AMF and UERANSIM/SUL source revisions before reporting a causal claim.

## 协议上下文引导的类型化时序模型树

重复循环实验可使用
`analysis/register_inference/experiments/infer_cycle_ngksi_regions.py`。该实验适配器与上述通用
`simple_scalar_v1` 流程分离。schema v2 保留原有的区域末端边拟合；schema v3 将区域证据进一步投影为
具体 DOT 边候选，并把观察区域表示为 `(r_before, ordered_observation_items, r_after)`。观察项保留类型、
字段路径、逻辑输入、事件位置和出现序号；信号与数值输入不会因为缺失、重复或乱序而相互补位。

信号通过 YAML 的 `mapping.signal_definitions` 声明。例如：

```yaml
mapping:
  signal_definitions:
    - id: initial_uplink_context
      path: ue_side.fields.isInitMsg
      value_type: boolean
      match:
        input_symbols:
          - registrationRequest
          - registrationRequestGUTI
      phase: before_numeric_inputs
```

`input_symbols` 可列出任意消息，也可仅写 `"*"`。算法不硬编码消息名；同一消息可声明多个信号，
同一信号可匹配多个消息。区域内先按轨迹事件顺序排列，同一事件中信号位于数值输入之前，同类观察按
配置声明顺序排列。槽位身份由字段及消息共同决定，因此相同字段出现在不同消息中仍是不同槽位。

schema v3 以 `numeric_input_definitions` 明确数值字段与逻辑输入寄存器的关系：

```yaml
mapping:
  numeric_input_definitions:
    - id: registration_ngksi
      input_register_id: ngksi_uplink
      path: ue_side.fields.registration_ksi_value
      value_type: integer
      match:
        input_symbols: [registrationRequest, registrationRequestGUTI]
      phase: before_register_updates
```

`id` 标识字段定义，`input_register_id` 标识同类型输入；同一 ID 的最后事件值覆盖先前值，但原始项、
覆盖链和边后有效快照都会输出。输入寄存器先更新：有输入为 `r_i'=i`，无输入为 `r_i'=r_i`，随后才拟合
下行可见的 `r`。第 2 轮用于初始化，含该类型输入的循环从第 3 轮起拟合。单边精确候选标为
`relatively_stable_candidate`；多边区域的前序最简/末端拟合拆分标为 `hypothetical_candidate`。

对假设性候选，顶层 `candidates` 仍只保留跨全部已对齐样本精确成立的交集，并据此建立
`candidate_index`。同时读取 `hypothetical_reconciliation`：它按循环分区保留局部模型树、跨分区交集、
非共识候选和冲突证据。局部树不应并入全局索引；同一完整类型化观察键却得到多个 `r_after` 时必须标为
`confirmed_observational_conflict`，而仅公式不同且观察键未重叠时标为 `partition_divergent`。

候选树严格区分三类分裂：

- `signal_guard`：外层传输上下文门控，形式为 `ite(sj == 1, f1, f2)`；
- `threshold_guard`：回绕结构，形式固定为 `ite(x < T, f, 0)`；
- `derived_value_guard`：基础公式和阈值树均失败后，按输入槽观测值枚举的
  `ite(r_i == v, f1, f2)`。

所有已配置且出现在区域中的信号都形成外层门控，即使训练样本中的值恒定。未观察分支写成
`unknown/unobserved_signal_branch`，支持不足写成 `unknown/insufficient_support`；含未知叶子的树只能标为
`partial_observational_candidate`。派生值分裂的两侧必须非空并分别达到最小连续支持，且整数值本身不获得
协议语义。配置的信号层、`max_numeric_depth` 和 `max_derived_signal_depth` 分别计数。

无下行锚点的前序边暂取 `r'=r`；若该边带信号，则固定为 `ite(s=1,unknown,r)`，输入寄存器仍按赋值或
保持记录。全部信号 guard 统一使用 `s=1` 作为 true 分支；未知信号分支、候选等级和观察精确状态必须
分开报告。

`isInitMsg` 应表述为伴随 NAS 输入事件的“初始上行传输上下文信号”。它不是 NAS PDU 内的显式 IE，
但接收端可以从 NGAP `InitialUEMessage` 与普通 `UplinkNASTransport` 的不同入口区分该上下文。由此得到的
分支仍只是观察候选；当前 C01–C05 循环内训练结果不能验证未观察分支，也不能替代留出轨迹或循环外轨迹评估。
