"""CLI for trajectory classification algorithm B exact two-projection formula discovery."""
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

from analysis.register_inference.trajectory_formula_discovery import (
    DEFAULT_INPUT_OUTPUTS,
    analyze,
    load_json,
    load_jsonl,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_report(result: dict[str, Any]) -> str:
    counts = result["counts"]
    lines = [
        "# 轨迹归类算法 B：二维公式候选、稳定性聚合与新稳定推断",
        "",
        "## 范围与口径",
        "",
        "本结果合并分析配置的三组 input/output，不区分相对稳定推断与假设性候选，也不按信号 `s=0/1` 拆分。",
        "EID 是拥有公式候选的基本对象；候选组只是规范化公式的反向索引，允许重叠，不进行唯一分簇。",
        "算法只采用精确整数关系与每个 EID 内去重的有向段，不读取算法 A 的任何聚类结果。",
        "",
        f"共覆盖 {counts['eid_count']} 个 EID、{counts['trajectory_count']} 条真实 R3–R10 轨迹、"
        f"{counts['sample_count']} 个样本点，形成 {counts['candidate_group_count']} 个候选组。",
        "",
        "各 input/output 的覆盖为：",
        "",
    ]
    for input_output, item in counts["by_input_output"].items():
        lines.append(
            f"- `{input_output}`：{item['eid_count']} 个 EID、{item['trajectory_count']} 条轨迹、"
            f"{item['sample_count']} 个点、{item['candidate_group_count']} 个候选组。"
        )
    projection_names = {"before_after": "r_before–r_after", "input_after": "r_i–r_after"}
    lines.extend(["", "## 稳定性推断聚合", ""])
    classification_names = {
        "pure_vertical": "仅有单一铅垂轨迹，不纳入聚合",
        "simple_exact": "全部稳定样本可由单一简单公式解释",
        "candidate_only": "仅提供后台局部候选",
        "degenerate_only": "仅有退化轨迹",
        "no_candidate": "没有满足门槛的候选",
    }
    for input_output, item in result["stable_aggregation"]["by_input_output"].items():
        signal = item["signal_condition"]
        signal_text = (
            "不适用"
            if signal["status"] == "not_applicable"
            else ", ".join(f"{key}={value}" for key, value in sorted(signal.get("values", {}).items()))
        )
        lines.extend(
            [
                f"### `{input_output}`",
                "",
                f"- 稳定源边：`{item['source_edge_ids']}`；{item['trajectory_count']} 条轨迹、"
                f"{item['sample_count']} 个真实点；信号适用条件：`{signal_text}`。",
            ]
        )
        for projection_name, projection in item["projections"].items():
            lines.append(
                f"- {projection_names[projection_name]}："
                f"{classification_names[projection['classification']]}。"
            )
        if item["final_candidates"]:
            for candidate in item["final_candidates"]:
                verification = candidate["verification"]
                lines.append(
                    f"- 聚合公式：`{candidate['formula']}`；层级 `{candidate['selection_tier']}`；"
                    f"逐点验证 {verification['matched_sample_count']}/{verification['sample_count']}。"
                )
        else:
            lines.append("- 当前没有通过全稳定样本验证的聚合公式。")
        lines.append("")
    new_stable = result["new_stable_inference"]
    yes_no = lambda value: "是" if value else "否"
    lines.extend(["## 新稳定推断", ""])
    lines.append(
        "本阶段只使用前序重划后完整、动态的新增长度1区域。先核对旧稳定三元组包含、主要方向和旧树逐点验证；"
        "全部成立时复用旧聚合公式，否则按相同 input/output 与 s 联合旧、新样本重新聚合。"
    )
    lines.append(
        "不同 s 分支公式相同时化简为单一公式，但分支样本来源和计数仍分别保存；只有公式不同时才生成信号根节点。"
    )
    lines.append("")
    for input_output, item in new_stable["by_input_output"].items():
        method = {
            "reused_old_aggregation": "复用旧稳定聚合",
            "same_signal_joint_reaggregation": "同信号旧、新样本联合重聚合",
        }.get(item.get("method"), item.get("method") or "未生成")
        lines.extend(
            [
                f"### `{input_output}`",
                "",
                f"- 方法：{method}；旧轨迹 {item['old_trajectory_count']} 条/{item['old_sample_count']} 点，"
                f"新轨迹 {item['new_trajectory_count']} 条/{item['new_sample_count']} 点。",
            ]
        )
        for signal_value, evidence in item.get("signal_evidence", {}).items():
            lines.append(
                f"- `s={signal_value}`：旧样本 {evidence['old_sample_count']} 点，"
                f"新样本 {evidence['new_sample_count']} 点；新轨迹 `{evidence['new_trajectory_ids']}`。"
            )
        for validation in item.get("trajectory_validations", []):
            edge = validation["edge"]
            lines.append(
                f"- `{validation['trajectory_id']}`：`{edge['source_state']}→{edge['target_state']}` "
                f"`{edge['logical_input']}/{edge['logical_output']}`；完整={yes_no(validation['complete_r3_r10'])}，"
                f"落入旧稳定轨迹={yes_no(validation['contained_in_old_stable_triples'])}，"
                f"方向一致={yes_no(validation['direction_consistent'])}，旧公式精确={yes_no(validation['old_formula_exact'])}。"
            )
        if item.get("final_candidates"):
            for candidate in item["final_candidates"]:
                verification = candidate["verification"]
                simplified = "；相同信号分支已合并" if candidate["identical_signal_branches_simplified"] else ""
                lines.append(
                    f"- 最终公式：`{candidate['formula']}`{simplified}；逐点验证 "
                    f"{verification['matched_sample_count']}/{verification['sample_count']}。"
                )
        else:
            lines.append(f"- 当前状态：`{item['status']}`；原因：`{item.get('failure_reason', '无')}`。")
        lines.append("")
    predecessor = result["predecessor_repartition"]
    pc = predecessor["counts"]
    lines.extend(
        [
            "## 前序最简归因与伪下行重划",
            "",
            "本阶段直接读取全部已选 I/O 的假设性观察区域；旧迁移状态只作审计，不参与筛选，也不读取旧前序反推候选。",
            "前序保持只把最近真实 KSI 下行的可观察值连续延伸到保持边；中间出现非延伸假设性边即中断，后续保持边不能自行恢复连续性或建立观察锚点。",
            f"输入清单共 {pc['hypothetical_trajectory_count']} 条轨迹；旧迁移状态计数 "
            f"`{pc['input_old_migration_statuses']}`，其中不满足长度2或动态门槛者仍保留排除原因。",
            f"长度2且R3–R10完整、循环内三元组动态变化的轨迹共 {pc['dynamic_length_two_trajectory_count']} 条："
            f"{pc['stable_match_count']} 条落入稳定三元组并集且主要方向一致，"
            f"{pc['reverse_preimage_count']} 条进入集合前像计算。",
            "",
            "### 动态长度2轨迹",
            "",
        ]
    )
    for item in predecessor["dynamic_length_two_trajectories"]:
        edge = item["predecessor_edge"]
        classification = (
            "支持前序最简不变推断"
            if item["stable_trajectory_match"]
            else "未落入稳定轨迹，执行集合前像计算"
        )
        signal = "{" + ", ".join(
            f"{key}={value}" for key, value in sorted(item["signal_context"].items())
        ) + "}" if item["signal_context"] else "不适用"
        lines.append(
            f"- `{item['trajectory_id']}`：前序 `{edge['edge_id']}` "
            f"`{edge['source_state']}→{edge['target_state']}` "
            f"`{edge['logical_input']}/{edge['logical_output']}`；信号 `{signal}`；{classification}；"
            f"旧迁移状态 `{item['old_migration_status']}`（仅审计）；"
            f"三元组 `{item['unique_triple_points']}`。"
        )
    lines.extend(["", "### 前序最简不变推断", ""])
    for item in predecessor["hold_inferences"]:
        edge = item["edge"]
        lines.append(
            f"- `{item['eid']}` `{edge['source_state']}→{edge['target_state']}` "
            f"`{edge['logical_input']}/{edge['logical_output']}`：`{item['formula']}`；"
            f"支持轨迹 `{item['support_trajectory_ids']}`；末端 I/O `{item['terminal_input_outputs']}`。"
        )
    lines.extend(["", "### 反向集合前像与候选赋值方案", ""])
    for item in predecessor["reverse_preimages"]:
        edge = item["predecessor_edge"]
        lines.append(
            f"- `{item['trajectory_id']}` 的前序 `{edge['edge_id']}` "
            f"`{edge['logical_input']}/{edge['logical_output']}`："
            "仅计算事件级伪 `r_after`，不生成边级公式。"
        )
        for candidate in item["candidate_preimages"]:
            per_repeat = "、".join(
                f"R{sample['repetition']}={sample['allowed_r_after_values']}"
                for sample in candidate["samples"]
            )
            lines.append(
                f"  - 来源稳定树 `{candidate['source_formula']}`；全值域逐轮前像：{per_repeat}；"
                f"跨R3–R10一致值 `{candidate['consistent_cycle_values']}`。"
            )
    for scenario in predecessor["assignment_scenarios"]:
        selections = "、".join(
            f"{item['trajectory_id']}:{item['predecessor_eid']}={item['value']}"
            for item in scenario["selections"]
        ) or "仅使用边级保持推断"
        lines.append(
            f"- 候选赋值方案 `{scenario['scenario_id']}`：{selections}；"
            "它是跨重复采用同一前像值的附加假设，不代表边级公式。"
        )
    lines.extend(["", "### 重划后的下一阶段入口", ""])
    lines.append(
        f"两个候选赋值方案中去重后共有 {pc['eligible_length_one_count']} 个真实、完整且动态的"
        "新增长度1区域；本阶段只标记资格，不重新拟合公式："
    )
    for item in predecessor["eligible_length_one_regions"]:
        edge = item["terminal_edge"]
        lines.append(
            f"- `{item['id']}`：`{edge['source_state']}→{edge['target_state']}` "
            f"`{edge['logical_input']}/{edge['logical_output']}`；候选赋值方案 `{item['scenario_ids']}`。"
        )
    lines.append("")
    lines.extend(
        [
        "## 边级候选",
        "",
        ]
    )
    for eid, edge_result in result["edges"].items():
        edge = edge_result["edge"]
        lines.extend(
            [
                f"### {eid}: {edge['source_state']}→{edge['target_state']}",
                "",
                f"- input/output：`{edge['logical_input']}/{edge['logical_output']}`；"
                f"候选等级仅作审计：`{next(item for item in result['trajectories'] if item['eid'] == eid).get('candidate_grade')}`。",
                f"- 观察到的信号上下文：`{edge_result['signal_contexts']}`；不参与本阶段拆分或拟合。",
                f"- 循环轨迹：{edge_result['trajectory_count']} 条；R3–R10 样本：{edge_result['sample_count']} 个。",
            ]
        )
        for projection_name, projection in edge_result["projections"].items():
            lines.append(f"- {projection_names[projection_name]}：")
            if projection["candidates"]:
                for candidate in projection["candidates"]:
                    unresolved = candidate["unresolved_points"]
                    suffix = (
                        f"；未解决铅垂点 `{unresolved}`" if unresolved else ""
                    )
                    lines.append(
                        f"  - `{candidate['formula']}`；作用域 `{candidate['scope']}`；"
                        f"证据 `{candidate['support_level']}/{candidate['evidence_grade']}`；"
                        f"覆盖 x=`{candidate['covered_x']}`，缺口=`{candidate['missing_x']}`；"
                        f"方向 `{candidate['direction']['majority']}`{suffix}。"
                    )
            else:
                reason = projection.get("no_formula_reason")
                text = "仅含静态点或纯铅垂退化轨迹" if reason == "degenerate_only" else "当前没有满足门槛的简单公式候选"
                lines.append(f"  - {text}；不生成更新公式。")
            for vertical in projection["vertical_components"]:
                lines.append(
                    f"  - 铅垂成分 x={vertical['x']}，y=`{vertical['distinct_y']}`，"
                    f"强度 `{vertical['strength']}`；它是结构证据，不是更新公式。"
                )
        lines.extend(["", "<details>", "<summary>展开全部循环的具体轨迹</summary>", ""])
        for trajectory_id in edge_result["trajectory_ids"]:
            trajectory = next(item for item in result["trajectories"] if item["id"] == trajectory_id)
            regions = " → ".join(
                f"R{point['repetition']}:({point['r_before']},"
                f"[ngksi_uplink={point['r_i']}],{point['r_after']})"
                + (f"[{point['input_source']}]" if point["input_source"] != "direct_observation" else "")
                for point in trajectory["points"]
            )
            signal = ",".join(
                f"{{{key}={value}}}" for key, value in sorted(trajectory.get("signal_context", {}).items())
            ) or "无信号"
            lines.append(f"- `{trajectory_id}`；{signal}：{regions}")
        lines.extend(["", "</details>", ""])
    lines.extend(["## 候选组反向索引", ""])
    for group in result["candidate_groups"]:
        lines.extend(
            [
                f"- `{group['candidate_id']}` / `{group['logical_input']}/{group['logical_output']}` / "
                f"{projection_names[group['projection']]} / `{group['formula']}`",
                f"  - 拥有者：`{group['owners']}`。",
                f"  - 核心拥有者：`{group['core_owners']}`。",
                f"  - 相容弱证据：`{group['compatible_eids']}`；仅部分分支相容：`{group['partial_compatible_eids']}`。",
            ]
        )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- 静态点和纯铅垂轨迹不产生公式、不进入拟合门槛或方向计票；与既有公式相符时只作相容证据，其余点保持未解决。",
            "- 常数候选必须由同一条轨迹内至少两个不同 x 构成的真实水平线支持；多个静态点不能拼成水平线。",
            "- 有缺口的候选只说明已观察 x 上精确成立，不把未观察 x 当作验证结果。",
            "- 第一阶段候选类型和信号上下文只作审计，不参与边级候选发现或候选组划分。",
            "- 稳定性推断聚合只联合相对稳定推断的源边；优先选择完整简单公式，仅在唯一输入寄存器值形成铅垂成分时构造并逐点验证跨投影模型树。",
            "- 旧稳定聚合中的 `s` 只作为公式适用条件。新稳定推断比较不同信号分支：公式相同即合并，只有不同且均精确时才生成信号根节点。",
            "- 前序最简阶段独立于旧迁移检验与旧前序反推，只把旧状态保留为审计字段；其页面入口暂时禁用，但 JSON 审计数据继续保留。",
            "- 前序不变推断是带稳定轨迹包含与主要方向前提的可反驳假设；反向集合前像只更新证据事件的伪 `r_after`，不产生前序边公式。",
            "- 伪下行只进行一轮观察区域重划；保持边仅连续延伸此前真实 KSI 下行，遇到非延伸假设性边即中断，不能自行建立观察锚点。伪边界不构成独立稳定证据，新增长度1区域本阶段不重新拟合公式。",
            "",
            "## 排版检查",
            "",
            "报告不用宽表格；长公式、EID 和循环轨迹放在可折叠段落中，公式与路径允许自然换行，窄屏不会由固定列宽造成横向溢出。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--cycle-cover", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--html")
    parser.add_argument(
        "--input-output",
        action="append",
        metavar="INPUT/OUTPUT",
        help="可重复指定；省略时分析默认三组 input/output",
    )
    args = parser.parse_args()

    input_outputs = []
    for value in args.input_output or ():
        if value.count("/") != 1 or not all(part.strip() for part in value.split("/")):
            parser.error(f"invalid --input-output value: {value!r}")
        logical_input, logical_output = (part.strip() for part in value.split("/", 1))
        input_outputs.append((logical_input, logical_output))
    if not input_outputs:
        input_outputs = list(DEFAULT_INPUT_OUTPUTS)

    paths = {name: Path(getattr(args, name)) for name in ("candidates", "trace", "cycle_cover", "config")}
    with paths["config"].open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    result = analyze(
        load_json(paths["candidates"]),
        load_jsonl(paths["trace"]),
        load_json(paths["cycle_cover"]),
        config,
        input_outputs,
    )
    result["provenance"] = {
        name: {"path": str(path), "sha256": sha256(path)}
        for name, path in paths.items()
    }
    output, report = Path(args.output), Path(args.report)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    report.write_text(render_report(result), encoding="utf-8", newline="\n")
    if args.html:
        from analysis.register_inference.experiments.visualize_trajectory_formula_candidates import render_html

        render_html(result, Path(args.html))


if __name__ == "__main__":
    main()
