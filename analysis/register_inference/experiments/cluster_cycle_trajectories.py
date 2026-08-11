"""Cluster ordered direct-region trajectories from completed register inference."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
import yaml
MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
from trajectory_clustering import analyze

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def breakable_ids(items: list[str]) -> str:
    return "、<wbr>".join(f"`{item}`" for item in items)

def ordered_observation_text(sample: dict) -> str:
    """Render the stored observation_items without changing their event order."""
    items = []
    for observation in sample.get('observation_items', []):
        if observation.get('kind') == 'signal':
            items.append(f"{{{observation.get('signal_id', 'signal')}={observation.get('value')}}}")
        elif observation.get('kind') == 'numeric_input':
            items.append(f"[{observation.get('input_register_id', observation.get('definition_id', 'input'))}={observation.get('value')}]")
    return ''.join(items) or '∅'

def report(result: dict, candidates: Path) -> str:
    counts = result['counts']
    input_text = str(candidates).replace('\\', '\\<wbr>')
    lines = ["# 循环轨迹相似度与聚类", "", "本报告只消费已完成的寄存器推断 `candidates.json`；不参与候选推断。比较对象为同一 `cycle_id + sequence_line` 的有序直接区域样本集合。", "", "## 输入与边界", "", f"- 输入：<span style=\"overflow-wrap:anywhere\">{input_text}</span>", f"- 提取轨迹：{counts['extracted_total']}；低辨别力排除：{counts['low_discriminability']}；实际聚类：{counts['actually_clustered']}（相对稳定推断 {counts['stable_clustered']}、假设性候选 {counts['hypothetical_clustered']}）。", f"- 迁移成功轨迹：总计 {counts['migration_succeeded_total']}，参与聚类 {counts['migration_succeeded_clustered']}，未参与 {counts['migration_succeeded_not_clustered']}。", "- 以 R3–R9 为七点基本周期；R10 仅用于 R3 同相位严格核验或缺失 `i` 补齐，第二周期其余点是模式补齐，不是 Open5GS 实测数据。", "- 所有比较按完全相同的 `input/output/isInitMsg` 切片；`s` 不进入距离，跨切片不自动匹配。", "", "## 自动聚类", ""]
    for tier, analyses in result['tiers'].items():
        lines += [f"### { {'stable_internal':'相对稳定推断内部','hypothetical_internal':'假设性候选内部','joint':'相对稳定推断与假设性候选联合'}[tier]}", ""]
        for item in analyses:
            members = " ｜<br>".join(breakable_ids(cluster) for cluster in item['clusters'])
            over_split = "；相对稳定推断被进一步细分" if tier == 'stable_internal' and item['selected_k'] > 1 else ("；相对稳定推断未被进一步细分" if tier == 'stable_internal' else "")
            lines += [f"- `{item['name'].replace('/', '/<br>')}`：k={item['selected_k']}：{members}" + over_split + "。"]
    lines += ["", "## 迁移成功一致性", ""]
    for item in result['migration_success_consistency']:
        lines.append(f"- `{item['trajectory_id']}`：{'一致' if item['consistent'] else '聚类不一致'}（{breakable_ids(item['cluster'])}）；`s={item['signal_slice']}`；匹配相对稳定推断组 {item['matched_group']}。")
    low = [item for item in result['trajectories'] if item.get('clustering_status') == 'low_discriminability']
    low_stable = [item['id'] for item in low if item['origin'] == 'stable']
    low_hypothetical = [item['id'] for item in low if item['origin'] == 'hypothetical']
    low_migration = [item['id'] for item in low if item.get('migration_status') == 'migration_succeeded']
    lines += ["", "## 低辨别力排除", "", f"- 共 {len(low)} 条：相对稳定推断 {len(low_stable)} 条，假设性候选 {len(low_hypothetical)} 条；它们只作为灰色背景轨迹，不进入距离矩阵、层次聚类、簇数选择或迁移一致性。", f"- 相对稳定推断：{breakable_ids(low_stable)}。", f"- 假设性候选：{breakable_ids(low_hypothetical)}。", f"- 迁移成功但退出聚类统计：{breakable_ids(low_migration) if low_migration else '无'}。"]
    lines += ["", "## 轨迹证据", ""]
    for trajectory in result['trajectories']:
        edge = trajectory['edge']
        lines.append(f"### `{trajectory['id']}`")
        lines.append("")
        lines.append(f"- EID：`{edge.get('edge_id')}`；`src/dst`：`{edge.get('source_state')}/{edge.get('target_state')}`；`input/output`：`{edge.get('logical_input')}/{edge.get('logical_output')}`。")
        observations = []
        for sample in trajectory['samples']:
            before = sample.get('previous_output', {}).get('value')
            after = sample.get('terminal_output', {}).get('value')
            observations.append(f"R{sample.get('repetition')} ({before},{ordered_observation_text(sample)},{after})")
        lines.append("- 映射观察区域：" + "；<wbr>".join(observations) + "。")
    lines += ["", "## 阶段性局限", "", "- 不跨 `s` 切片自动匹配簇。", "- 聚类仅比较轨迹形状，轨迹形状簇尚不能等同于函数逻辑簇。", "- 当前结果保留启发式比较的缺口，留待后续处理。", "", "## 可读性检查", "", "消息对在 `/` 后显式换行；路径、成员 ID 和观察区域分隔处设置可换行点；本报告不使用宽表，距离矩阵无损保存在 JSON。"]
    return "\n".join(lines) + "\n"

def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', required=True); parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True); parser.add_argument('--report', required=True)
    args=parser.parse_args(argv)
    try:
        source=Path(args.candidates).resolve(); config_path=Path(args.config).resolve()
        settings=yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        if not isinstance(settings, dict): raise ValueError('configuration must be a YAML mapping')
        result=analyze(json.loads(source.read_text(encoding='utf-8')), settings)
        result['input_artifact']={'path':str(source),'sha256':digest(source)}
        output=Path(args.output).resolve(); report_path=Path(args.report).resolve(); output.parent.mkdir(parents=True,exist_ok=True); report_path.parent.mkdir(parents=True,exist_ok=True)
        report_path.write_text(report(result, source),encoding='utf-8'); result['report_artifact']={'path':str(report_path),'sha256':digest(report_path)}
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding='utf-8')
    except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f'trajectory-clustering error: {exc}',file=sys.stderr); return 2
    print(f"Wrote {result['counts']['actually_clustered']} clustered trajectories to {output} / {report_path}")
    return 0
if __name__=='__main__': raise SystemExit(main())
