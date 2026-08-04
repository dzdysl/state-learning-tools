# 类型化时序寄存器候选推断：设计思想与边界

## 目标

本设计从已学习的 Mealy 状态机、重复执行的具体轨迹和少量协议层观察中，生成 AMF
逻辑寄存器的**候选更新关系**。它服务于黑盒 NAS 推断：结果描述“哪些更新树与已观察样本精确一致”，
而不声称已恢复 AMF 的内部成员、源代码变量或唯一实现路径。

当前实现位于
`analysis/register_inference/experiments/infer_cycle_ngksi_regions.py`，C01–C05 的可复现实例
位于 `state-learning-experiments` 对应 follow-up 的
`analysis/derived/register_inference/`。

## 核心观察单位：跨消息区域

单个 Mealy 边通常不足以直接看到寄存器更新前后值。因此，适配器以相邻、配置为可见寄存器值的
AMF 下行消息为边界，构造区域：

```text
(r_before, ordered_observation_items, r_after)
```

- `r_before` 是前一个下行 KSI 观察；
- `r_after` 是终止下行 KSI 观察；终止消息所属的具体 DOT 边是候选拟合边；
- 中间观察项按实际轨迹事件顺序保留；第 1 轮用于建立锚点。schema v3 中第 2 轮还用于初始化输入寄存器，
  含输入寄存器的循环由第 3–10 轮拟合。

该选择承认黑盒限制：无法断言 AMF 在区域中的哪一个内部时刻写寄存器；这里只把终止下行字段作为
该区域的更新后观测。

## 类型化、有身份且有序的观察项

观察项不是匿名的数值列表，而是带类型的对象：

```text
信号量： {s0=0/1}
数值输入： [i0=value]
```

每个对象还记录 `field_path`、`input_symbol`、`event_position`、`trace_line` 和
`occurrence_index`。因此，同一字段在不同逻辑消息中出现、或同一消息在一个区域中多次出现，都会形成
不同槽位。例如 C04 E0073：

```text
(0,{registrationRequestGUTI.isInitMsg=1},[registrationRequestGUTI.ksi=7],
   {registrationRequest.isInitMsg=0},[registrationRequest.ksi=7],0)
```

排序规则固定为：先按轨迹事件顺序；同一事件中信号量在数值输入前；多个信号按
`signal_definitions` 的声明顺序；多个数值字段按输入字段配置顺序。缺失、重复、乱序或跨样本身份不一致
会报出对齐异常，绝不将后一个观察项移动到缺失槽位补位。

## 配置接口

schema v3 的数值输入与信号都使用通用选择器，而不是在代码中硬编码 `registrationRequest` 等消息名：

```yaml
mapping:
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
```

`input_symbols` 可为任意消息列表，或单独使用 `"*"` 通配全部逻辑输入；同一消息可带多个信号或数值
输入。`id` 标识具体字段定义，`input_register_id` 标识显式共享的同类型输入寄存器；选择器不匹配时，即便
字段存在也不会采集。原始字段在不同消息中仍是不同观测项，但同一 `input_register_id` 的最后事件值可作为
边后有效输入值，且覆盖链必须保留。

`isInitMsg` 在方法文字中称为“初始上行传输上下文信号”：它不是 NAS PDU 内显式 IE，
而是随上行传输入口可观察的上下文。它可作为 AMF 处理可能依赖的外生输入，但不应被误写成 AMF
从 NAS PDU 字段直接读取的位。

## 候选语言与三类树节点

叶子只枚举下列简单更新：

```text
r' = c
r' = r + k
r' = r_i + k
```

其中 `c`、`k` 均由已观察样本确定。模型树严格区分三种 guard，避免把不同证据语义混为一谈。

1. `signal_guard`：外生上下文门控，`ite(sj == 1, f1, f2)`。
   所有已配置且实际出现在区域中的信号都强制成为外层节点，即使当前样本中恒为 0 或恒为 1。
2. `threshold_guard`：回绕的直接观察模型，`ite(x < T, f, 0)`。
    `x` 为 `r` 或某个输入寄存器，`T` 必须是轨迹中观察到的值，且 `else` 固定为常数 0。
   这不是一般性的模运算断言，也不会输出显式 `mod` 公式。
3. `derived_value_guard`：反例驱动的输入特殊值分裂，`ite(r_i == v, f1, f2)`。
    它只在基础叶子与阈值树均失败后启用；`v` 只从数值输入寄存器的观测值枚举，两个分支均必须非空并各自满足
   最小连续支持。数值 7 因此不会自动获得任何协议语义。

深度独立控制：

```text
总层次 = 实际信号层数 + max_numeric_depth + max_derived_signal_depth
```

默认数值阈值深度为 1，派生值分裂深度为 1；信号层不消耗数值深度预算。

## 候选保留、未知与索引

在每个已观察信号组合下，先找所有精确叶子；失败才尝试阈值树；两者均失败才尝试派生值分裂。
所有精确并列项都保留，不能仅因 `r'=r` 比 `r'=r_i+1` 简洁就丢弃后者。

- 信号组合从未出现：`unknown/unobserved_signal_branch`；
- 分支样本的连续支持不足：`unknown/insufficient_support`；
- 含任一未知叶子：`partial_observational_candidate`；
- 全部叶子由充分样本精确解释：`observationally_exact_candidate`。

结果中的 `candidate_index` 以有序 `guard_path + update_tree + input_register_updates + status + grade`
聚合具体 DOT 边集合；相同候选在多个边上出现时共享同一索引项，但每个边的区域与覆盖证据仍完整保留。

### 假设性候选的交集、分歧与冲突

对 `hypothetical_candidate`，顶层 `candidates` 仍有严格含义：它只包含对该边**全部**已对齐样本精确成立的
全局交集，且仍是唯一进入 `candidate_index` 的候选集合。多边区域的分解不能因为局部样本支持某个公式，就
把该公式误标为整个 DOT 边的确定更新。

同时，结果中的 `hypothetical_reconciliation` 按 `cycle_id` 保存每个循环分区的局部模型树、跨分区
`intersection_candidates` 与 `non_consensus_candidates`。局部候选只说明该分区在当前拆分假设下的解释，
即使全局交集为空也必须保留，以便后续后缀、额外信号或源码对照反驳、合并或细化它们。

冲突的判定比“公式不相同”更严格：只有同一完整类型化观察键（`r_before`、带字段身份和出现序号的信号与
数值输入、输入寄存器值）实际对应多个 `r_after` 时，才输出 `confirmed_observational_conflict` 及全部证据。
若局部公式不同而观察键未重叠，则仅为 `partition_divergent`；两种信息均保留，算法不得任意挑选一棵树。

## 结构性弱先验

下列规则是协议启发式候选，不是自动确认：

- 非自环边进入 D 类状态时，加入高优先级 `r'=7` 重置候选；
- 输入和输出均无相关 KSI 观察时，加入 `r'=r` 候选；若与前一条同时适用，D 类重置优先。

当前工作流不因“UE 上行携带 KSI”直接加入 `r'=i` 或 `r'=r`；这类关系必须由区域样本自行拟合。

## C01–C05 的作用与论文边界

C01–C05 验证了常值信号门控、信号外嵌回绕树、未知分支、并列叶子、跨事件多信号/多输入排序和
同候选跨边聚合。它们是机制验证，不验证未观察分支，也不能证明某个信号必然对应 AMF 内部寄存器的
真实控制条件。

论文中可将该方法定位为“协议上下文引导的类型化时序模型树”。其可辩护性来自：

- 明确区分外生传输上下文、数值回绕和反例派生值；
- 不把未观察分支补成确定规则；
- 对特殊值分裂施加输入范围、分支非空、连续支持和深度限制；
- 保留并列精确解释，避免人为过早选择；
- 将循环内精确性与循环外泛化明确分开。

真正的泛化结论仍应使用留出轨迹或循环外轨迹检验，并对照该次运行对应的 AMF 与 UERANSIM/SUL
源码版本；源代码对照可提高解释置信度，但不改变候选推断本身的观察性性质。

## 边级候选、最后写入投影与输入寄存器

### 有限观察基而非实现寄存器数量断言

已完成的 Mealy 学习将访问前缀约简为有限控制状态，但这并不严格推出真实 AMF 的寄存器数量有限。
它只说明：在选定输入字母表、查询预算和观测字段下，前缀后的可见行为可由有限观察等价类描述。
本算法据此采用较弱假设：仅配置并建模有限个由 SUL/UE 观测支持的寄存器类型；任何未被这些字段
区分的内部状态仍是不可辨识部分。结果必须称为启发式符号寄存器候选，而非对实现存储布局的证明。

### schema v3：显式输入寄存器身份

`numeric_input_definitions` 中的 `id` 标识一个具体消息字段，而 `input_register_id` 标识其所属的逻辑
输入寄存器。多个输入符号可显式写入同一个 `input_register_id`，例如普通注册与 GUTI 注册的 ngKSI 都
写入 `ngksi_uplink`。这种同类关系来自 YAML，而非由相同路径或相同消息名隐式猜测。

同一类型信号（同一 signal ID）或数值输入（同一 input-register ID）在区域内按轨迹事件顺序采用最后
写入值。原始观察项不删除；结果同时给出 `effective_region_snapshot`、每项覆盖链和每条边后的
`effective_edge_snapshot`。因此 C04 E0073 的原始区域

```text
(0,{GUTI.isInitMsg=1},[GUTI.ksi=7],{reg.isInitMsg=0},[reg.ksi=7],0)
```

可被审计地投影为该末端边的 `(0,{isInitMsg=0},[ngksi_uplink=7],0)`，而前一事件仍保留为独立边的
证据。

### 输入寄存器更新顺序

每个 `input_register_id` 对应输入寄存器 \(r_i\)。含该输入的边先执行 \(r_i'=i\)，无该输入的边执行
\(r_i'=r_i\)，再对下行可见寄存器 \(r\) 拟合 `r'=c`、`r'=r+k` 或 `r'=r_i+k`。第 2 轮用于输入寄存器
初始化，含该类型输入的循环从第 3 轮起参与拟合；整个循环未出现该类型则报告
`unobservable_input_register`。当前阶段不因常规强制信号门控而任意为 \(r_i\) 添加常数叶子；只有后续
跨边传播出现反例时，才允许提出信号条件赋值候选。

### 区域到边的候选等级

区域恰好只包含一条 DOT 边，且该边的已观察样本精确满足时，输出
`relatively_stable_candidate`。该等级不消除 `unknown` 信号分支，也不证明源码实现，只表示本边的已观察
证据没有使用跨边分配假设。

区域含多条边时，算法把末端、携带下行锚点的边作为拟合边；前序无锚点边暂取最简 `r'=r`。前序边若
还携带信号，则固定为 `ite(s=1,unknown,r)`，而输入寄存器照常赋值。所有这些边均标记
`hypothetical_candidate`，并携带 `region_to_edge_decomposition`、`last_write_projection` 或
`minimal_predecessor_default` 等假设来源。信号 guard 统一使用 `ite(s=1,\ldots,\ldots)`；候选等级与
`observationally_exact_candidate` / `partial_observational_candidate` 的观测状态独立保存。

## 从边局部候选到跨边等价重构

### 观察性动机

固定的、只依赖单次前后字段的推导规则无法覆盖复杂的内部寄存器逻辑。可学习的 Mealy 机已经给出
一个重要参照：无限多的访问前缀可以被约简为有限个控制状态，只要这些前缀后的所有输入后缀在可观察
的输入/输出行为上不可区分。寄存器推断可采用同一观察论立场，但推断对象从控制状态变为一条具体
DOT 边上的更新关系。

对边 \(e:q\xrightarrow{i/o}q'\)，内部寄存器更新是确定的，却通常不能在该边发生的瞬间直接观察。
因此，当前脚本从该边之前和之后的已对齐观测建立候选；更复杂的内部逻辑仍可先保留为候选，并由后续
边、后续循环或专门构造的输入后缀所产生的行为来排除或确认。这里的“等价”严格说是**观察等价**：
只有在已执行的后缀、所采用的输入域和实验假设下不可区分。若要声称最小 Mealy 机意义上的等价，则
还需要对全部允许输入后缀的等价查询或相应证明。

### 差别性假设：以唯一边为保守基线

差别性假设（edge-local update hypothesis）将每条具体的 Mealy 转移边单独建模。即使两条边具有相同
的逻辑输入/输出标签，例如 N 状态自环上的 `regReq/authReq` 与 D→N 转移上的 `regReq/authReq`，它们的
源状态、历史和已对齐的伴随信号仍可能不同，因而各自维护独立的候选集合。这样做不会预设 AMF 的
内部实现，也不会把控制状态差异误写为寄存器规则相同。

唯一边本身可作为“存在上下文差异”的证据，但不是 `isInitMsg` 等具体字段的证明。`isInitMsg` 必须仍以
带身份、时序和来源消息的外生观察信号进入样本；边位置只提供额外的控制状态上下文。该基线也解释了
为什么仅对两条同标签边分别拟合，尚不能充分恢复“初始注册”和“非初始注册”在内部寄存器上的真实
差异。

### 同一性假设：作为待检验的跨边约束

同一性假设（cross-edge equality / same-semantics hypothesis）不把“相同消息”直接等同于“相同处理
函数”。它提出可检验的共享约束：若两条或多条边在统一后的类型化槽位模式下存在同一个更新模板，
则这些边可共享模板参数或共享完整候选。例如可检验

\[
U_e(r,\mathbf{i},\mathbf{s})=U_f(r,\mathbf{i},\mathbf{s}).
\]

合并只在以下条件同时满足时进行：槽位身份与顺序可对齐、每一条参与边均有足够连续支持、共享模板对
全部样本精确成立，并且没有已知后缀反例。否则保留边局部候选及其反例见证，而不是强行合并。因而，
差别性假设提供最细粒度、低承诺的起点；同一性假设是在证据支持下对该起点施加的压缩约束。

### 上下行消息分解：结构性假设而非源码断言

跨边关系可按共享的上行、共享的下行和共享的完整输入/输出对建立。例如：

- `regReq/authReq` 与 `regReqGUTI/authReq` 共享下行 `authReq`；
- `regReqGUTI/idReq` 与 `regReqGUTI/authReq` 共享上行 `regReqGUTI`。

这些关系提示可尝试一个结构化候选：将上行事件的输入规范化、控制/寄存器上下文更新和下行可见输出
分别表示为候选组件。例如，令 \(z=P_{in}(i,\mathbf{s})\)，再用

\[
(q',r')=U(q,r,z),\qquad o=P_{out}(q',r',z)
\]

描述一个**可能的**输入处理—上下文更新—输出产生分解。共享上行或下行只是在该分解中增加共享
`P_in` 或 `P_out` 的候选约束；它不证明源码中存在同名处理函数，也不证明两类消息的处理时点相同。
所有组件仍须由完整边样本和未来行为共同约束，且在证据不足时保留多个分解。

### 逐步重构与未来后缀验证

建议将后续算法分为五层，且每层均可撤销或细化前层结论：

1. 按当前类型化时序区域产生每条边的边局部更新候选；
2. 以“共享上行”“共享下行”“共享完整 I/O”“相同控制状态类”和“兼容槽位模式”建立跨边关系图；
3. 对关系图中的边组尝试共享公式、共享参数或上述输入/更新/输出分解，并保存所有精确并列解；
4. 将每个候选沿后续边序列传播，寻找候选间会预测不同寄存器观测、输出字段或状态行为的后缀；
5. 回放已有后缀或设计新的区分查询。若出现反例则拆分共享组、细化 guard 或重做区域对齐；若持续一致，
   则提高共享候选的观察置信度，而不将其提升为无条件的实现事实。

这正是控制状态 Mealy 学习与寄存器推断的联合观测接口：控制状态划分给出可比较的边上下文，寄存器
候选给出对未来行为的附加预测；新的区分后缀既可能否定寄存器候选，也可能暴露原有 Mealy 状态划分
需要重新对齐或细化。每次重构都应版本化保存边映射、轨迹对齐规则、候选状态和产生反例的后缀，避免
把不同假设版本的证据混合。

### 适用范围与限制

C04 的 E0073 已显示同一推断区域可包含多个事件输入和多个伴随信号，适合用作组合观测的例子；但它
本身不能证明 AMF 的实际处理函数已被分解或共享。类似地，当前下行 KSI 被当作更新后观测只是一个
可替换的观测约定，而非从黑盒数据必然推出的因果顺序。

因此，论文应将结果表述为“协议上下文引导的、可被未来后缀反驳的符号寄存器转导器候选”，并明确：
未观察分支保持未知；不同精确候选可以并列；共享语义的证据强度取决于跨边样本、保持条件不变的
区分后缀和留出轨迹；源码对照只能增加解释层面的置信度，不能替代黑盒可辨识性证明。

## 复现与维护

### 严格的原始数据转换边界

推断器的 `inputs.trace` 不是“任意整理后的轨迹”。它必须是冻结运行快照中
`statelearner_trace.jsonl` 的字节一致证据副本；`cleaned`、筛选、CSV、按消息重排、
根据日志/pcap 回填字段的文件均不是合法输入。转换只允许在内存中为当前计算增加
JSONL 行号，以及将**配置指定**字段解析为整数或布尔值；原始记录的键、值、顺序和
分组不得写回修改。

在调用推断器前，必须运行 `prepare_register_inference_trace.py`。它以同一个 YAML
配置核对 `inputs.trace`、`inputs.sequence_file` 与 `inputs.cycle_cover`：接口只能是
`sequence_export.cycles`，每个选中 `cycle_id` 的每个 `line_number` 必须唯一匹配一个
`sequence_id` 组，组末 `sequence_inputs`、组内记录数和逐步
`abstract_io.input` 都必须与 `.seq` 一致。失败时停止推断，不得手工挑选“看起来正常”
的记录。其转换清单记录源/目标 SHA-256、字节数、记录数、组数与匹配关系。

该检查将归档、转换和初始脚本的实际输入契约绑定在一起：随后
`infer_cycle_ngksi_regions.py` 直接读取已物化的完整 JSONL，而不是读取转换后的中间
表。这样，区域中的 `trace_line` 始终能回指到不可变证据中的同一物理行。

运行方式：

```powershell
D:\anaconda3\python.exe D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\experiments\prepare_register_inference_trace.py `
  --config <record>\analysis\derived\register_inference\c01-c14-ngksi-signal-inference.yaml `
  --source-trace D:\state-learning-lab\run-data\<platform>\<run-id>\statelearner_trace.jsonl `
  --evidence-trace <record>\evidence\statelearner_trace.jsonl `
  --manifest <record>\analysis\derived\register_inference\c01-c14-trace-materialization.json

D:\anaconda3\python.exe D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\experiments\infer_cycle_ngksi_regions.py `
  --config <record>\analysis\derived\register_inference\c01-c14-ngksi-signal-inference.yaml `
  --output <record>\analysis\derived\register_inference\c01-c14-ngksi-signal-candidates.json `
  --report <record>\analysis\derived\register_inference\c01-c14-ngksi-signal-summary.md `
  --workbook <record>\analysis\derived\register_inference\c01-c14-ngksi-signal-details.xlsx
```

对于 schema v3，`--report` 与 `--workbook` 都是必填项。Markdown 按 H13 的四列固定布局
HTML 格式摘要全部具体边组；完整审计工作簿以 `cycle_id` 为主键，分别保存边级协调、循环—边
使用、`V01…` 变体、逐公式候选及协调证据。同一边在不同循环中分别列出，且所有全局、交集、
非共识和局部并列公式都不得折叠或选择其一。`hypothetical_candidate` 与
`relatively_stable_candidate` 必须作为 Excel 的独立候选类型列；不以物理 `.seq` 行号作为阅读主键。
交集为空与 `confirmed_observational_conflict` 必须分开标注；前者不自动成为矛盾。

面向读者的 Markdown 与 Excel 统一使用展示简写：`unknown/<reason>` 显示为 `unknown`，
`r_i[ngksi_uplink]` 显示为 `r_i`，输入寄存器更新不显示括号内的观察来源；候选类型、观测状态、
协调状态、路线类型和作用域显示为简明中文。候选 JSON 仍保存未压缩的公式树、状态枚举、观察原因和
更新来源；展示简写不改变推断语义或证据边界。

修改候选语言、排序规则、槽位身份、转换契约或未知分支语义时，必须同步更新：
预处理器与适配器测试、`analysis/register_inference/experiments/AGENTS.md`、本文件，
以及使用该适配器的实验总结。
