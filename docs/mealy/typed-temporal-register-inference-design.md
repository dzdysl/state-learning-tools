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
- `r_after` 是终止下行 KSI 观察；终止消息所属的具体 DOT 边是待拟合边；
- 中间观察项按实际轨迹事件顺序保留；第 1 轮用于建立锚点，重复 2–10 是拟合样本。

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

数值输入可以为每个逻辑输入配置一个路径或有序路径列表。信号使用通用选择器声明，而不是在代码中
硬编码 `registrationRequest` 等消息名：

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

`input_symbols` 可为任意消息列表，或单独使用 `"*"` 通配全部逻辑输入；同一消息可带多个信号，
同一信号可应用到多个消息。选择器不匹配时，即便字段存在也不会采集。

`isInitMsg` 在方法文字中称为“初始上行传输上下文信号”：它不是 NAS PDU 内显式 IE，
而是随上行传输入口可观察的上下文。它可作为 AMF 处理可能依赖的外生输入，但不应被误写成 AMF
从 NAS PDU 字段直接读取的位。

## 候选语言与三类树节点

叶子只枚举下列简单更新：

```text
r' = c
r' = r + k
r' = ij + k
```

其中 `c`、`k` 均由已观察样本确定。模型树严格区分三种 guard，避免把不同证据语义混为一谈。

1. `signal_guard`：外生上下文门控，`ite(sj == 1, f1, f2)`。
   所有已配置且实际出现在区域中的信号都强制成为外层节点，即使当前样本中恒为 0 或恒为 1。
2. `threshold_guard`：回绕的直接观察模型，`ite(x < T, f, 0)`。
   `x` 为 `r` 或某个输入槽，`T` 必须是轨迹中观察到的值，且 `else` 固定为常数 0。
   这不是一般性的模运算断言，也不会输出显式 `mod` 公式。
3. `derived_value_guard`：反例驱动的输入特殊值分裂，`ite(ij == v, f1, f2)`。
   它只在基础叶子与阈值树均失败后启用；`v` 只从数值输入槽的观测值枚举，两个分支均必须非空并各自满足
   最小连续支持。数值 7 因此不会自动获得任何协议语义。

深度独立控制：

```text
总层次 = 实际信号层数 + max_numeric_depth + max_derived_signal_depth
```

默认数值阈值深度为 1，派生值分裂深度为 1；信号层不消耗数值深度预算。

## 候选保留、未知与索引

在每个已观察信号组合下，先找所有精确叶子；失败才尝试阈值树；两者均失败才尝试派生值分裂。
所有精确并列项都保留，不能仅因 `r'=r` 比 `r'=i0+1` 简洁就丢弃后者。

- 信号组合从未出现：`unknown/unobserved_signal_branch`；
- 分支样本的连续支持不足：`unknown/insufficient_support`；
- 含任一未知叶子：`partial_observational_candidate`；
- 全部叶子由充分样本精确解释：`observationally_exact_candidate`。

结果中的 `candidate_index` 以有序 `guard_path + update_tree + status` 聚合具体 DOT 边集合；
相同候选在多个边上出现时共享同一索引项，但每个边的区域证据仍完整保留。

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

## 复现与维护

运行方式：

```powershell
D:\anaconda3\python.exe D:\state-learning-lab\projects\state-learning-tools\analysis\register_inference\experiments\infer_cycle_ngksi_regions.py `
  --config <record>\analysis\derived\register_inference\c01-c02-ngksi-signal-inference.yaml `
  --output <record>\analysis\derived\register_inference\c01-c02-ngksi-signal-candidates.json
```

修改候选语言、排序规则、槽位身份或未知分支语义时，必须同步更新：适配器测试、
`analysis/register_inference/experiments/AGENTS.md`、本文件，以及使用该适配器的实验总结。
