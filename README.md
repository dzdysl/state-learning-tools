# State Learning Tools

该仓库用于保存跨 Open5GS、free5GC 和 OAI 的状态机分析、渲染、实验归档和工作区操作工具。它是这些脚本和语义文档的唯一源码；个人 Codex skill 仅负责识别任务、给出行为约束并调用此处的版本。

## 工具阶段

- `rendering/mealy_to_pdf`：纯 Graphviz 渲染，或保留删边、合并转移逻辑的 `_smp` Mealy 渲染（`render_graphviz.py`）。
- `classification/coarse_classification`：对状态爆炸模型进行初始粗分类。
- `classification/iterative_refinement`：初始粗分类与迭代细化状态类别（`analyze_refinement.py`）。
- `classification/state_class_backtrace`：从状态类别回溯原始状态或路径。
- `classification/iterative_backtrace`：细化后反复回溯和验证分类结果（`trace_binary_splits.py`）。
- `refinement/counterexample_ttt`：基于反例快速细化 TTT 模型。
- `protocol_events`：将三套核心网与 UERANSIM 日志冻结为可追溯的协议事件流。
- `analysis/state_distinction`：模拟输入并证明两个 Mealy 状态/类别的行为区分（`explain_distinction.py`）。
- `analysis/reachability`：查找最短访问序列与可达性（`find_shortest_paths.py`）。
- `operations/workspace`：六个学习器 JAR 的构建发布与整个工作区状态检查。
- `operations/experiment_archive`：失败实验的日志清单、哈希和大小分级归档。

## 使用约定

- `docs/` 保存算法和工作流的语义说明；不要在个人 skill 下维护第二份说明。
- 脚本保留原有 CLI 参数和输出格式。个人 skill 应调用本仓库中的绝对路径，方便 Git commit 作为实验 provenance。
- `protocol_events` 是已有的独立未提交工作，本次迁移不修改其实现或文档。

工具之间的输入输出格式稳定后，再补充数据契约文档；当前不预先假定统一的 Mealy 或分类文件格式。
