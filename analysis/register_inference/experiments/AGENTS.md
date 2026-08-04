# 实验性 ngKSI 寄存器推断规则

本目录脚本与分析仅生成弱协议先验支持的黑盒候选，不得将候选表述为 AMF 实现变量、源码更新点或协议事实。

## 观测与候选边界

1. 当前工作流把携带 ngKSI 的 AMF 下行字段视为所属区域的更新后 `r_after` 观测；不在本工作流中推断它是更新前字段。
2. 有限 Mealy 观察等价类不证明真实 AMF 寄存器数量有限。算法只对配置的、可由 SUL/UE 观测支持的有限寄存器基生成候选；其余内部状态保持未建模。
3. 非自环且目标为 D 类状态的边可保留 `r'=7` 重置候选，优先级高于无观测保持候选；它仍是未确认先验。
4. 输入、输出均无 ngKSI 观测的边可保留 `r'=r` 最简候选；缺失观测不是寄存器未变的证明。

## 原始轨迹物化与输入契约

1. `inputs.trace` 必须是冻结外部 raw 快照内完整 `statelearner_trace.jsonl` 的字节一致
   `evidence/` 副本。先执行 `prepare_register_inference_trace.py`，再执行本目录的推断器；
   不得直接使用 live run 目录、`statelearner_trace.cleaned.jsonl`、筛选导出或手工改写的
   JSONL。
2. 预处理器必须记录源/目标路径、字节数、SHA-256、记录数、`sequence_id` 组数、选中循环
   变体到轨迹组的映射，以及 `payload_transformation: none`。目标证据已存在时仅允许校验其
   SHA-256 相等，禁止覆盖不同字节的证据。
3. 所有循环输入统一来自 `cycle_cover.sequence_export.cycles`。每项使用 `cycle_id`、
   `line_number` 和 `loop_inputs`；不得读取或恢复 `sequence_export.routes`、`route_inputs`
   或同义兼容层。
4. 对每个选中变体，组末 `sequence_inputs` 必须唯一匹配 `.seq` 行，组内记录数必须等于该行
   输入数，且每步 `abstract_io.input` 必须按序匹配。任一失败表示该 raw run 不适合当前
   推断配置，停止而不是筛选或修补记录。
5. 允许的转换仅限内存内的 JSONL 行号标注、配置字段的整数解析和配置字段的布尔解析；不得
   对证据作字段补全、重解码、去重、排序、跨会话拼接或删除。

## 类型化观察与 schema v3

1. 推断区域保持原始 `(r_before, ordered_observation_items, r_after)`，每项保留类型、字段路径、逻辑输入、事件位置、轨迹行和出现序号。缺失、重复、乱序或身份不一致时报告异常，不得移位补齐。
2. schema v3 使用 `mapping.numeric_input_definitions`。每个定义具有唯一 `id`、整数路径、消息选择器和 `input_register_id`；相同 `input_register_id` 明确表示同类型输入，而不是由消息名隐式猜测。
3. 同一信号 ID 或输入寄存器 ID 在一个区域内按事件时序采用最后写入值。原始观察、覆盖链和来源消息必须同时保留；覆盖投影不能删除或改写原始证据。
4. 信号定义仍由 `mapping.signal_definitions` 驱动，消息选择器可为任意列表或唯一通配符 `"*"`。同一事件中信号按声明顺序位于数值输入之前。

## 输入寄存器与边级归属

1. 每个 `input_register_id` 对应一个输入寄存器 `r_i`。新输入在该边其他寄存器更新前写入：有输入为 `r_i'=i`，无输入为 `r_i'=r_i`。某循环从未出现该类型时标记 `unobservable_input_register`。
2. 输入寄存器以第 2 轮初始化；有该类输入的循环从第 3 轮开始拟合。除非跨边传播出现反例，本轮不得仅因信号存在而为 `r_i` 枚举常数分支。
3. 单边区域的已观察样本精确候选标记 `relatively_stable_candidate`；该标签只说明该边已观察分支相对稳定，未观察信号分支仍可为 `unknown`。
4. 多边区域采用“前序边最简、末端可观察边拟合”时，所有拆分结果标记 `hypothetical_candidate`，并记录 `region_to_edge_decomposition`、最后写入投影及最简默认等假设来源。
5. 无下行锚点的前序边默认 `r'=r`。若它带输入和信号，使用 `ite(s=1, unknown, r)`，同时仍记录输入寄存器赋值。

## 模型树规则

1. 所有信号节点统一为 `ite(s=1, true_branch, false_branch)`；不以 `s=0` 作为正向 guard。
2. `threshold_guard` 只表示可观察回绕结构 `ite(x<T,f,0)`，否则叶固定为常数 0；不显式声称一般模运算。
3. `derived_value_guard` 仅在基础叶和阈值树均失败后，从具有充分连续支持的输入寄存器值中枚举；整数 `7` 不自动具有协议语义。
4. 保留 `r'=c`、`r'=r+k`、`r'=r_i+k` 及所有精确并列树。`observationally_exact_candidate` 与 `partial_observational_candidate` 是观测状态，必须和候选等级分开记录。
5. 对 `hypothetical_candidate`，`candidates` 仍只保存跨全部已对齐样本精确成立的交集；同时必须保留按循环分区的局部候选、交集、非共识候选及同一类型化观察键对应多个 `r_after` 的冲突证据。局部候选不得混入全局 `candidate_index`，也不得因全局交集为空而删除。

## 报告边界

- 候选应同时报告公式、对齐异常、覆盖链、拆分假设、未观察分支和可用反例。
- 假设性分区的公式差异但没有相同观察键的不同输出时，标为分区分歧；相同完整观察键出现不同输出时，标为观察冲突。两者都保留，不得任意选择其中一个公式。
- 同一消息或同一 I/O 标签只能提出共享语义候选，不能直接证明 AMF 使用同一个源码处理函数。
- 循环内精确性与循环外泛化必须分开；共享或拆分结论应由后续后缀、留出轨迹及对应版本源码对照继续检验。
- schema v3 命令必须同时提供 `--report` 与 `--workbook`。Markdown 是 H13 式四列固定布局摘要，
  覆盖每个具体 DOT 边组；Excel 是完整审计工作簿。两项缺任一项即失败，JSON 同时记录两种产物的
  哈希、完整性与工作簿行数。
- Markdown 摘要固定为“循环、边与节点 / 边级候选 / 输入寄存器 / 候选等级”四列。它保留全部
  全局并列候选，但把 `cycle_id` 展开、`V01…` 变体、局部分区、非共识与直接冲突的完整材料交给
  Excel；消息对仍在 `/` 后换行。
- Excel 必须至少包含概览、边级协调、循环—边使用、变体、候选明细和协调证据。循环主键只用
  `cycle_id`，变体使用稳定 `V01…`；每个 `edge_samples.cycle_id` 都须在“循环—边使用”中独立
  出现，即使同一边已在其他循环出现。`hypothetical_candidate` 与
  `relatively_stable_candidate` 必须有独立的“候选类型”列。
- 工作簿不折叠并列公式；必须保存全局/交集/非共识/局部分区的候选作用域。交集为空必须明确显示；
  只有同一完整观察键对应相反 `r_after` 时才标记直接冲突。原始重复样本和区域快照仍由 JSON 无损
  保存，不为便利而复制或改写。
- 面向读者的 Markdown 与 Excel 使用统一展示简写：`unknown/<reason>` 显示为 `unknown`，
  `r_i[ngksi_uplink]` 显示为 `r_i`，输入寄存器更新不显示括号内的观察来源；候选类型、观测状态、
  协调状态、路线类型与候选作用域使用简明中文。JSON 保留未压缩的公式树、状态枚举、观察原因与更新来源，
  因此展示简写不得改变候选语义。
- 工作簿预览 PNG 只写入系统临时目录；artifact-tool 自动产生的
  `<workbook>.inspect.ndjson` 是检查中间文件，生成器在校验后必须删除，且不得写入实验记录或 Git。
- 交付前验证边组、循环、变体与循环—边使用的覆盖，以及工作簿筛选、冻结表头、换行和列宽；
  使用 artifact-tool 生成 XLSX，并渲染每个工作表检查没有截断或不可读的长文本。
