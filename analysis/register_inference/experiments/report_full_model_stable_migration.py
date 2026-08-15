"""Generate the standalone H14 full-model stable-inference migration report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import yaml

TOOLS_ROOT = Path(__file__).resolve().parents[3]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from analysis.register_inference.full_model_stable_migration import (
    TARGET_INPUT_OUTPUTS,
    analyze_full_model_stable_migration,
)
from analysis.register_inference.trajectory_formula_discovery import load_json, load_jsonl


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def edge_text(edge: dict[str, Any]) -> str:
    return (
        f"`{edge['edge_id']}：{edge['source_state']}→{edge['target_state']}，"
        f"{edge['logical_input']}/{edge['logical_output']}`"
    )


def signal_text(context: dict[str, Any]) -> str:
    if not context:
        return "不适用"
    return "，".join(f"{{{key}={value}}}" for key, value in sorted(context.items()))


def observation(point: dict[str, Any], context: dict[str, Any]) -> str:
    signal = "".join(f"{{{key}={value}}}," for key, value in sorted(context.items()))
    return f"({point['r_before']},{signal}[ngksi_uplink={point['r_i']}],{point['r_after']})"


def trajectory_line(points: list[dict[str, Any]], context: dict[str, Any]) -> str:
    return " → ".join(
        f"R{point['repetition']}:{observation(point, context)}" for point in points
    )


def _status_text(status: str) -> str:
    return {
        "existing_stable_inference": "既有稳定性推断",
        "stable_inference_migration": "稳定性推断（迁移）",
        "temporarily_not_migrated": "暂不迁移",
    }[status]


def edge_list_text(io_result: dict[str, Any], eids: list[str]) -> str:
    by_eid = {item["eid"]: item["edge"] for item in io_result["edges"]}
    return "、".join(edge_text(by_eid[eid]) for eid in eids) if eids else "无"


def render_report(result: dict[str, Any], provenance: dict[str, dict[str, str]]) -> str:
    ordinary = result["by_input_output"]["registrationRequest/authenticationRequest"]
    guti = result["by_input_output"]["registrationRequestGUTI/authenticationRequest"]
    reverse = result["reverse_closure"]
    lines = [
        "# H14 轨迹归类算法 B：全模型稳定性推断迁移",
        "",
        "## 阅读入口",
        "",
        "这是一份可脱离原聊天记录独立阅读的迁移审计报告。它说明 H14 重复循环实验的寄存器观察如何被物化、",
        "轨迹归类算法 B 如何从二维公式候选逐步得到完整稳定树，以及本阶段如何把两棵树迁移到同 input/output 的全模型边。",
        "本报告只记录黑盒观察支持的候选与迁移结论，不把 `r`、`r_i` 或公式解释为已经确认的 Open5GS 源码变量。",
        "",
        "本阶段没有修改 `candidates.json`、算法 B 机器结果或交互 HTML，也没有执行下一轮寄存器推断。",
        "新增边级状态只使用“稳定性推断（迁移）”；未满足本阶段闭合条件的边只写“暂不迁移”。",
        "",
        "## 项目结构与证据链",
        "",
        "实验记录位于 `experiments/open5gs/ueransim-smc-context-pdu-selection/h14-base-runtime-20260804/`。",
        "关键材料按以下顺序连接：",
        "",
        "1. `evidence/hypotheses/hypothesis_14.dot` 给出 H14 的 EID、状态端点和 input/output 语义。",
        "2. `evidence/statelearner_trace.jsonl` 是从冻结 raw 快照无损物化的完整运行轨迹。",
        "3. 兄弟记录 `../h14-complete-teardown-20260801/analysis/cycle-cover/base-result.json` 与本记录的 `analysis/register-inference/config.yaml` 界定循环、R3–R10重复、KSI输入和信号字段。",
        "4. `analysis/register-inference/candidates.json` 保存原始观察区域、候选等级、相对稳定推断、旧迁移检验与前序反推材料。",
        "5. `analysis/register-inference/trajectory-formula-candidates.json` 保存算法 B 的真实八点轨迹、二维候选、稳定聚合和新稳定推断。",
        "6. 本报告生成器只读取以上冻结材料并在内存中计算迁移；输出即本文件，不建立平行 JSON。",
        "",
        "输入哈希：",
        "",
    ]
    for name, item in provenance.items():
        lines.append(f"- `{name}`：`{item['path']}`；SHA-256 `{item['sha256']}`。")

    lines.extend(
        [
            "",
            "## 术语与坐标",
            "",
            "- `r_before`：一个观察区域开始前最近一次真实 KSI 下行值。",
            "- `r_i`：当前事件之前有效的 `ngksi_uplink` 输入寄存器值；直接观察不到时由冻结 trace 在同一序列内重放。",
            "- `r_after`：观察区域末端真实 KSI 下行值；本阶段反推前序事件时另行标明伪值来源。",
            "- 三元组统一写为 `(r_before,{signal=value},[ngksi_uplink=r_i],r_after)`，并保持信号与数值输入的事件顺序。",
            "- `r_before–r_after` 与 `r_i–r_after` 是算法 B 的两个二维投影；静态点和纯铅垂轨迹不独立建立常数公式。",
            "- 稳定树是用既有相对稳定推断及后续新稳定推断样本逐点验证后的完整更新树。",
            "- 稳定性推断迁移表示已建立的同 input/output 树能够解释目标边的完整证据；它不是源码事实。",
            "",
            "## 算法 B 到本阶段的工作脉络",
            "",
            "1. 二维公式候选阶段以 EID 为基本对象，在两个投影中发现常数、单位仿射与单阈值分段公式；候选组仅为反向索引。",
            "2. 稳定性推断聚合阶段只联合相对稳定推断源边；优先使用完整简单公式，必要时用唯一铅垂值构造跨投影模型树。",
            "3. 新稳定推断阶段把收紧重划后得到的动态长度1区域与旧稳定样本联合验证；两个信号分支公式相同，因此不保留冗余信号根节点。",
            "4. 本阶段把已经完整的树应用到对应 input/output 的所有 H14 边；长度2区域允许一次前序反推闭合，长度3和长度4反例暂不处理。",
            "",
            "## 已完成的两棵稳定树",
            "",
            f"- `registrationRequest/authenticationRequest`：`{ordinary['formula']}`。旧、新稳定样本已经验证96/96点；本阶段检查15条H14边。",
            f"- `registrationRequestGUTI/authenticationRequest`：`{guti['formula']}`。旧、新稳定样本已经验证88/88点；本阶段检查10条H14边。",
            "- 两组均保留 `s=0` 与 `s=1` 的样本来源；因为分支公式相同，最终树不生成信号根节点。",
            "",
            "## 全模型迁移规则",
            "",
            "1. 既有稳定性推断边保持不变。",
            "2. 某条边的全部真实R3–R10样本逐点满足完整树时，标记为“稳定性推断（迁移）”。",
            "3. 剩余完整长度2区域先在值域0…7上反推前序事件的 `r_after`。",
            f"4. 数学前像为 `{{6,7}}` 时，读取旧稳定输入投影的唯一核心铅垂成分 `x={reverse['vertical_preference']['x']}`，只选择值7，不生成值6分支。",
            "5. 这一步是算法 B 的显式候选消歧规则；逆方程本身仍有两个前像，报告不得把7表述为数学唯一解。",
            "6. 前序 `r_before` 直接取原观察区域起点；本批9条轨迹均有直接值，不需要延伸。前序 `r_i` 必须与冻结 trace 重放值一致。",
            "7. 用重建前序三元组运行与二维公式候选阶段相同的精确拟合，再把选择的前序 `r_after=7` 代回末端稳定树逐点验证。",
            "8. 长度3或长度4中仍有反例时，本阶段整条边暂不迁移。",
            "",
            "## 迁移总览",
            "",
            f"- `registrationRequest/authenticationRequest`：覆盖 {ordinary['counts']['covered_edge_count']}/{ordinary['counts']['edge_count']} 条；"
            f"本阶段迁移 {edge_list_text(ordinary, ordinary['migration_eids'])}；暂不迁移 "
            f"{edge_list_text(ordinary, ordinary['temporarily_not_migrated_eids'])}。",
            f"- `registrationRequestGUTI/authenticationRequest`：覆盖 {guti['counts']['covered_edge_count']}/{guti['counts']['edge_count']} 条；"
            f"暂不迁移 {edge_list_text(guti, guti['temporarily_not_migrated_eids'])}。",
            "",
            "## 两个 input/output 的完整边清单",
            "",
        ]
    )

    for io in TARGET_INPUT_OUTPUTS:
        item = result["by_input_output"][io]
        lines.extend([f"### `{io}`", "", f"完整树：`{item['formula']}`。", ""])
        for edge_item in item["edges"]:
            lines.append(
                f"- {edge_text(edge_item['edge'])}：{_status_text(edge_item['status'])}；"
                f"{edge_item['trajectory_count']}条轨迹/{edge_item['sample_count']}点；"
                f"原三元组逐点匹配 {edge_item['direct_validation']['matched_sample_count']}/"
                f"{edge_item['direct_validation']['sample_count']}。"
            )
        lines.append("")

    lines.extend(
        [
            "## 9条长度2区域的前序反推",
            "",
            f"本节覆盖 {reverse['trajectory_count']} 条轨迹。每轮先得到数学前像 `{{6,7}}`，再由核心铅垂成分 `r_i=7` 只选择前序 `r_after=7`。",
            "",
        ]
    )
    for item in reverse["trajectories"]:
        lines.extend(
            [
                f"### `{item['terminal_trajectory_id']}`",
                "",
                f"- 末端边：{edge_text(item['terminal_edge'])}；信号：`{signal_text(item['signal_context'])}`。",
                f"- 前序边：{edge_text(item['predecessor_edge'])}。",
                "- 数学前像与选择：" + "、".join(
                    f"R{sample['repetition']}={sample['allowed_r_after_values']}→{sample['selected_r_after']}"
                    for sample in item["mathematical_preimages"]
                ) + "。",
                "- 重建前序轨迹：" + trajectory_line(item["predecessor_points"], {}) + "。",
                "- 来源：`r_before=direct_region_start`，`r_i=frozen_trace_replay`，"
                "`r_after=reverse_preimage_vertical_x_preference`。",
                "",
            ]
        )

    lines.extend(["## 前序边二维拟合", ""])
    for eid, item in reverse["predecessor_fits"].items():
        lines.append(f"### {edge_text(item['edge'])}")
        lines.append("")
        lines.append(f"证据轨迹：`{item['trajectory_ids']}`；对应末端：`{item['terminal_trajectory_ids']}`。")
        for projection_name, projection in item["projections"].items():
            label = "r_before–r_after" if projection_name == "before_after" else "r_i–r_after"
            formulas = [candidate["formula"] for candidate in projection.get("candidates", [])]
            if formulas:
                lines.append(f"- {label}：候选 `{formulas}`。")
            else:
                lines.append(f"- {label}：不产生公式；原因 `{projection.get('no_formula_reason')}`。")
        lines.append("")
    lines.extend(
        [
            f"因此 {edge_text(reverse['predecessor_fits']['E0042']['edge'])} 的动态水平轨迹在两个投影都得到 `r'=7`。",
            f"{edge_text(reverse['predecessor_fits']['E0114']['edge'])} 与 {edge_text(reverse['predecessor_fits']['E0210']['edge'])} "
            "各自只有静态点，按算法B规则不独立产生常数公式；其事件级伪值仍参与末端闭合验证。",
            "",
            "## 迁移边的逐点证据",
            "",
        ]
    )
    for eid in ("E0085", "E0181", "E0193"):
        edge_item = next(
            edge for edge in ordinary["edges"] if edge["eid"] == eid
        )
        lines.extend([f"### {edge_text(edge_item['edge'])}", ""])
        lines.append(f"最终状态：{_status_text(edge_item['status'])}。")
        if "reverse_closure" in edge_item:
            closure = edge_item["reverse_closure"]
            lines.append(
                f"选择前序 `r_after=7` 后，末端稳定树逐点匹配 "
                f"{closure['matched_sample_count']}/{closure['sample_count']}。"
            )
            for validation in closure["validations"]:
                lines.append(
                    f"- `{validation['trajectory_id']}`：" +
                    trajectory_line(validation["adjusted_points"],
                                    next(t["signal_context"] for t in edge_item["trajectories"] if t["trajectory_id"] == validation["trajectory_id"])) +
                    f"；匹配 {validation['validation']['matched_sample_count']}/{validation['validation']['sample_count']}。"
                )
        else:
            for trajectory in edge_item["trajectories"]:
                lines.append(
                    f"- `{trajectory['trajectory_id']}`；信号 `{signal_text(trajectory['signal_context'])}`："
                    f"{trajectory_line(trajectory['points'], trajectory['signal_context'])}；匹配 "
                    f"{trajectory['validation']['matched_sample_count']}/{trajectory['validation']['sample_count']}。"
                )
        lines.append("")

    lines.extend(["## 暂不迁移的反例", ""])
    for eid in ordinary["temporarily_not_migrated_eids"]:
        edge_item = next(edge for edge in ordinary["edges"] if edge["eid"] == eid)
        lines.extend([f"### {edge_text(edge_item['edge'])}", ""])
        for trajectory in edge_item["trajectories"]:
            result_item = trajectory["validation"]
            lines.append(
                f"- `{trajectory['trajectory_id']}`；区域长度 `{trajectory['region_edge_count']}`；"
                f"信号 `{signal_text(trajectory['signal_context'])}`；"
                f"匹配 {result_item['matched_sample_count']}/{result_item['sample_count']}；"
                f"轨迹 {trajectory_line(trajectory['points'], trajectory['signal_context'])}。"
            )
            if result_item["failures"]:
                lines.append(f"  - 反例：`{result_item['failures']}`。")
        lines.append("本阶段不处理该边的多步前序反推，因此整条边暂不迁移。")
        lines.append("")

    lines.extend(
        [
            "## 全部轨迹审计",
            "",
            "以下折叠段覆盖两个 input/output 的全部真实R3–R10轨迹，便于新任务直接复核边级结论。",
            "",
        ]
    )
    for io in TARGET_INPUT_OUTPUTS:
        lines.extend(["<details>", f"<summary>{io}：展开全部轨迹</summary>", ""])
        for edge_item in result["by_input_output"][io]["edges"]:
            for trajectory in edge_item["trajectories"]:
                lines.append(
                    f"- {edge_text(edge_item['edge'])} / `{trajectory['trajectory_id']}` / "
                    f"`{signal_text(trajectory['signal_context'])}`："
                    f"{trajectory_line(trajectory['points'], trajectory['signal_context'])}。"
                )
        lines.extend(["", "</details>", ""])

    lines.extend(
        [
            "## 结论与边界",
            "",
            "- `registrationRequestGUTI/authenticationRequest` 已覆盖10/10条H14边。",
            "- `registrationRequest/authenticationRequest` 已覆盖13/15条H14边；"
            f"{edge_list_text(ordinary, ordinary['temporarily_not_migrated_eids'])} 暂不迁移。",
            "- 反推只为这9条证据轨迹选择事件级伪值7。"
            f"{edge_text(reverse['predecessor_fits']['E0042']['edge'])} 的 `r'=7` 是二维拟合候选；"
            f"{edge_text(reverse['predecessor_fits']['E0114']['edge'])} 与 "
            f"{edge_text(reverse['predecessor_fits']['E0210']['edge'])} 没有边级公式。",
            "- 铅垂线 `x=7` 是候选消歧依据，不消除逆方程存在值6前像这一数学事实。",
            "- 本报告不修改原候选、不把迁移结论回写算法A、不继续处理长度3或长度4区域，也不声称确认真实AMF实现。",
            "",
            "## 可复现命令",
            "",
            "```powershell",
            "D:\\anaconda3\\python.exe D:\\state-learning-lab\\projects\\state-learning-tools\\analysis\\register_inference\\experiments\\report_full_model_stable_migration.py `",
            "  --candidates analysis/register-inference/candidates.json `",
            "  --trajectory-formulas analysis/register-inference/trajectory-formula-candidates.json `",
            "  --trace evidence/statelearner_trace.jsonl `",
            "  --cycle-cover ../h14-complete-teardown-20260801/analysis/cycle-cover/base-result.json `",
            "  --config analysis/register-inference/config.yaml `",
            "  --output analysis/register-inference/stable-migration-report.md",
            "```",
            "",
            "## 排版检查",
            "",
            "报告不使用宽表格。长公式、绝对路径、EID组合和R3–R10轨迹均放在独立段落或可折叠块中，允许自然换行；",
            "具体边始终同时写出 `EID、src/dst、input/output`，避免脱离上下文的边号。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--trajectory-formulas", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--cycle-cover", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    paths = {
        name: Path(getattr(args, name.replace("-", "_")))
        for name in ("candidates", "trajectory-formulas", "trace", "cycle-cover", "config")
    }
    with paths["config"].open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    # Loading the cycle-cover file is an explicit scope/integrity check even
    # though the already materialized Algorithm B result supplies the points.
    cycle_cover = load_json(paths["cycle-cover"])
    if not isinstance(cycle_cover.get("sequence_export", {}).get("cycles"), list):
        raise ValueError("cycle_cover.sequence_export.cycles is required")
    result = analyze_full_model_stable_migration(
        load_json(paths["candidates"]),
        load_json(paths["trajectory-formulas"]),
        load_jsonl(paths["trace"]),
        config,
    )
    provenance = {
        name: {"path": str(path), "sha256": sha256(path)}
        for name, path in paths.items()
    }
    output = Path(args.output)
    output.write_text(render_report(result, provenance), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
