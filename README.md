# State Learning Tools

该仓库用于保存跨 Open5GS、free5GC 和 OAI 的状态机分析工具。

## 工具阶段

- `rendering/mealy_to_pdf`：将 Mealy 模型渲染为 PDF。
- `classification/coarse_classification`：对状态爆炸模型进行初始粗分类。
- `classification/iterative_refinement`：迭代细化状态类别。
- `classification/state_class_backtrace`：从状态类别回溯原始状态或路径。
- `classification/iterative_backtrace`：细化后反复回溯和验证分类结果。
- `refinement/counterexample_ttt`：基于反例快速细化 TTT 模型。

工具之间的输入输出格式稳定后，再补充数据契约文档；当前不预先假定统一的 Mealy 或分类文件格式。
