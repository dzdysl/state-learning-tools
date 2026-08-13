"""Create an offline, deterministic H14 directed-polyline family explorer."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any
import math

import yaml
from plotly.offline.offline import get_plotlyjs


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        if path.suffix == ".json":
            return json.load(handle)
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in handle if line.strip()]
        return yaml.safe_load(handle)


def _whitelist(scope: dict[str, Any]) -> set[tuple[str, str, int]]:
    """Read only the explicitly permitted trajectory identity fields."""
    return {(str(t["edge"]["edge_id"]), str(t["cycle_id"]), int(t["sequence_line"]))
            for t in scope.get("trajectories", [])}


def _formula(edge_result: dict[str, Any], groups: list[dict[str, Any]], signal_slice: str) -> tuple[str, list[str], str]:
    edge = edge_result["edge"]
    if edge["edge_id"] == "E0073":
        return "假设性候选（观察区域归因）", [], "combined_sample_fit_failed"
    matching = [g for g in groups
                if edge["edge_id"] in g.get("source_edge_ids", [])
                and g.get("logical_input") == edge.get("logical_input")
                and g.get("logical_output") == edge.get("logical_output")
                and (not g.get("signal_context") if signal_slice == "not_applicable"
                     else {str(x.get("value")) for x in g.get("signal_context", [])} == {signal_slice})]
    if matching:
        texts = [c.get("update_tree_text") or c.get("candidate_update_tree_text", "")
                 for g in matching for c in g.get("candidates", [])]
        return "相对稳定推断", [text for text in texts if text], "available"
    resolution = edge_result.get("hypothetical_candidate_resolution") or {}
    if resolution.get("combined_sample_fit_failed"):
        return "假设性候选（观察区域归因）", [], "combined_sample_fit_failed"
    texts = [c.get("update_tree_text", "") for c in edge_result.get("candidates", [])]
    return "假设性候选（观察区域归因）", [text for text in texts if text], "available"


def _region_point(region: dict[str, Any]) -> dict[str, Any] | None:
    before, after = region.get("previous_output"), region.get("terminal_output")
    if not before or not after:
        return None
    value = (region.get("input_register_values") or {}).get("ngksi_uplink")
    return {"r_before": before.get("value"), "r_after": after.get("value"),
            "r_i": value.get("value") if value else None,
            "source": "direct_observation" if value else "carried_from_R2"}


def _at_path(record: dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _carried_inputs(trace: Any, config: dict[str, Any] | None, cycle_cover: dict[str, Any] | None) -> dict[int, Any]:
    """Replay numeric-input writes in trace order; the first R3 read carries R2 once."""
    definitions = (config or {}).get("mapping", {}).get("numeric_input_definitions", [])
    values: dict[int, Any] = {}
    cycle_entries = {(str(c["cycle_id"]), int(v["line_number"])): c
                     for c in ((cycle_cover or {}).get("sequence_export", {}).get("cycles", []))
                     for v in c.get("variants", [])}
    if trace and not cycle_entries:
        raise ValueError("cycle_cover sequence_export.cycles is required for sequence-local carry replay")
    sequences: dict[Any, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for line, record in enumerate(trace or [], 1):
        sequences[record.get("sequence_id", line)].append((line, record))
    # Every sequence starts with a fresh register.  The cycle-cover export is
    # consulted here to reject malformed per-sequence loop shapes rather than
    # silently carrying a value across a different variant.
    for rows in sequences.values():
        current: Any = None
        for line, record in rows:
            if record.get("symbol_index") == 1:
                current = None
            symbol = (record.get("abstract_io") or {}).get("input")
            for definition in definitions:
                if symbol in definition.get("match", {}).get("input_symbols", []):
                    observed = _at_path(record, definition.get("path", ""))
                    if observed is not None:
                        current = int(observed)
            values[line] = current
    return values


def visual_segments(points: list[dict[str, Any]], view: str) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic directional and modular-wrap geometry for one trajectory."""
    axes = {"3d": ("r_before", "r_i", "r_after"), "ba": ("r_before", "r_after"),
            "ia": ("r_i", "r_after")}[view]
    arrows, wraps = [], []
    for index, (left, right) in enumerate(zip(points, points[1:])):
        start, end = [left[a] for a in axes], [right[a] for a in axes]
        delta = [b - a for a, b in zip(start, end)]
        if any(delta):
            arrows.append({"index": index, "tail": start, "head": end, "vector": delta,
                           "midpoint": [(a + b) / 2 for a, b in zip(start, end)],
                           "plotly_up_angle": plotly_up_angle(*delta) if len(delta) == 2 else None})
        wrapped_axes = range(3) if view == "3d" else range(2)
        if any(start[i] == 6 and end[i] == 0 for i in wrapped_axes):
            wraps.append({"index": index, "tail": start, "head": end})
    return {"arrows": arrows, "wraps": wraps}


def plotly_up_angle(dx: float, dy: float) -> float:
    """Plotly ``angleref='up'`` angle: up=0°, clockwise positive in data space."""
    return math.degrees(math.atan2(dx, dy))


def build_payload(candidates: dict[str, Any], scope: dict[str, Any], trace: Any = None,
                  cycle_cover: Any = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build chart data from direct regions only; trace inputs are accepted for auditable CLI parity."""
    allowed = _whitelist(scope)
    groups = candidates.get("relatively_stable_inference", {}).get("groups", [])
    trace_carry = _carried_inputs(trace, config, cycle_cover)
    members: list[dict[str, Any]] = []
    for result in candidates.get("results", []):
        edge = result.get("edge", {})
        eid = str(edge.get("edge_id", ""))
        by_member: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for region in result.get("direct_regions", []):
            key = (eid, str(region.get("cycle_id")), int(region.get("sequence_line", -1)))
            if key not in allowed or not (3 <= int(region.get("repetition", 0)) <= 10):
                continue
            point = _region_point(region)
            if point:
                signals = {str(x.get("value")).lower().replace("true", "1").replace("false", "0")
                           for x in region.get("signals", []) if x.get("signal_id") == "isInitMsg"}
                signal_slice = "not_applicable" if not signals else (signals.pop() if len(signals) == 1 else "mixed")
                if signal_slice == "mixed":
                    raise ValueError(f"mixed isInitMsg in {eid}:{region.get('cycle_id')}:L{region.get('sequence_line')}")
                if point["r_i"] is None:
                    terminal_line = int((region.get("terminal_output") or {}).get("trace_line", 0))
                    point["r_i"] = trace_carry.get(terminal_line)
                point["R"] = int(region["repetition"])
                if point["source"] == "carried_from_R2" and point["R"] != 3:
                    point["source"] = "carried_from_previous_input"
                point["signal_slice"] = signal_slice
                by_member[(key[1], key[2])].append(point)
        for (cycle_id, line), points in sorted(by_member.items()):
            points.sort(key=lambda p: p["R"])
            if len(points) != 8 or [p["R"] for p in points] != list(range(3, 11)):
                continue
            slices = {point["signal_slice"] for point in points}
            if len(slices) != 1:
                raise ValueError(f"mixed isInitMsg across R3-R10 for {eid}:{cycle_id}:L{line}")
            signal_slice = slices.pop()
            kind, formulas, formula_status = _formula(result, groups, signal_slice)
            template_key = tuple((p["r_before"], p["r_after"], p["r_i"]) for p in points)
            members.append({"id": f"{eid}:{cycle_id}:L{line}", "edge": edge, "cycle_id": cycle_id,
                            "sequence_line": line, "kind": kind, "formulas": formulas,
                            "formula_status": formula_status, "points": points,
                            "signal_slice": signal_slice, "template_key": template_key,
                            "visuals": {view: visual_segments(points, view) for view in ("3d", "ba", "ia")}})
    families: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for item in members:
        families[(item["edge"]["edge_id"], item["kind"], item["template_key"])].append(item)
    output = []
    for number, (_, items) in enumerate(sorted(families.items(), key=lambda pair: repr(pair[0])), 1):
        for item in items:
            item["template"] = f"T{number:02d}"
            item["template_type"] = "静态模板" if len(set(item["template_key"])) == 1 else "动态模板"
            output.append(item)
    return {"members": sorted(output, key=lambda x: x["id"]), "count": len(output)}


def render_html(payload: dict[str, Any], output: Path) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plotly = get_plotlyjs()
    page = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>H14 有向折线族</title><style>
*{box-sizing:border-box}body{margin:0;background:#f6f8fb;color:#162235;font:14px system-ui,"Microsoft YaHei",sans-serif}main{max-width:1800px;margin:auto;padding:20px}h1{margin:0;font-size:26px}.sub{color:#53657b}.bar,.summary,.formula,.canvas,.members{background:#fff;border:1px solid #d8e1ec;border-radius:12px;padding:14px;margin:14px 0;box-shadow:0 1px 2px #14213d0d}.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:end}label{display:grid;gap:4px;color:#506176;font-size:12px}select{min-width:160px;padding:8px;border:1px solid #b9c8d8;border-radius:7px;background:white}.formula pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:7px 0}.canvas{height:690px}#plot{width:100%;height:100%}.template-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}.template-card{border:1px solid #d8e1ec;border-left:6px solid var(--template-color);border-radius:10px;padding:12px;background:#fbfdff}.template-card h3{margin:0 0 7px;font-size:15px}.template-path{margin:7px 0;padding:8px;background:#f1f5f9;border-radius:7px;line-height:1.65;overflow-wrap:anywhere}.member-detail{margin:8px 0;padding:7px;border-top:1px solid #e5edf5}.member-detail summary{overflow-wrap:anywhere}.member-meta{color:#53657b;font-size:12px;margin:5px 0}.tag{display:inline-block;padding:3px 7px;border-radius:99px;background:#e7f0fa;color:#145a8d;margin-right:5px}details summary{cursor:pointer;font-weight:650}@media(max-width:700px){main{padding:12px}.bar{display:grid;grid-template-columns:1fr}select{width:100%}.canvas{height:500px}.template-grid{grid-template-columns:1fr}}
</style></head><body><main><h1>H14 有向折线族</h1><p class="sub">真实 R3–R10 闭合轨迹；不含距离聚类。方向箭头表示时间顺序，红虚线为 6→0 模 7 回绕，黑菱形为 i=7。</p><section class="bar" aria-label="层级筛选"><label>候选类型<select id="kind"></select></label><label>输入/输出<select id="io"></select></label><label>s<select id="signal"></select></label><label>EID<select id="eid"></select></label><label>模板类型<select id="template_type"></select></label><label>模板<select id="template"></select></label><label>cycle_id / sequence_line<select id="member"></select></label><label>视图<select id="view"><option value="3d">3D r_before/r_i/r_after</option><option value="ba">2D r_before-r_after</option><option value="ia">2D r_i-r_after</option></select></label></section><section class="summary" id="summary"></section><section class="formula"><details open><summary>公式区</summary><div id="formula"></div></details></section><section class="canvas"><div id="plot"></div></section><section class="members"><details open><summary>模板成员详情</summary><div id="members"></div></details></section></main><script>__PLOTLY__</script><script>
const DATA=__DATA__,$=id=>document.getElementById(id),uniq=a=>[...new Set(a)].sort(),esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));const GLOBAL_RANGES=(()=>{let p=DATA.members.flatMap(x=>x.points),range=k=>{let v=p.map(q=>q[k]),lo=Math.min(...v),hi=Math.max(...v),pad=Math.max(.35,(hi-lo)*.05);return [lo-pad,hi+pad]};return {r_before:range('r_before'),r_i:range('r_i'),r_after:range('r_after')}})();
const order=['kind','io','signal_slice','eid','template_type','template','member'],ids={signal_slice:'signal'};
function el(id){return $(ids[id]||id)} function field(id,x){if(id==='io')return `${x.edge.logical_input}/${x.edge.logical_output}`;if(id==='eid')return x.edge.edge_id;if(id==='member')return x.id;return x[id]}
function opts(id,values){let node=el(id),old=node.value;node.innerHTML='<option value="">全部</option>'+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');node.value=values.includes(old)?old:''}
function prior(n){let a=DATA.members;for(let id of order.slice(0,n)){let v=el(id).value;if(v)a=a.filter(x=>field(id,x)===v)}return a}
function list(){return prior(order.length)}
function refresh(changed=-1){for(let n=Math.max(0,changed+1);n<order.length;n++){let a=prior(n),id=order[n];opts(id,uniq(a.map(x=>field(id,x))))}draw()}
function wrapTrace(geometry,mode,legendgroup){if(!geometry.wraps.length)return null;let coords=[[],[],[]];geometry.wraps.forEach(w=>{w.tail.forEach((v,i)=>coords[i].push(v,w.head[i],null))});let base={mode:'lines',line:{color:'#d62728',dash:'dash',width:6},hoverinfo:'skip',name:'6→0 模7回绕',legendgroup,showlegend:false};return mode==='3d'?{...base,type:'scatter3d',x:coords[0],y:coords[1],z:coords[2]}:{...base,type:'scatter',x:coords[0],y:coords[1]}}
function directionTrace(geometry,mode,color,legendgroup){if(!geometry.arrows.length)return null;if(mode==='3d')return {type:'cone',x:geometry.arrows.map(a=>a.tail[0]),y:geometry.arrows.map(a=>a.tail[1]),z:geometry.arrows.map(a=>a.tail[2]),u:geometry.arrows.map(a=>a.vector[0]),v:geometry.arrows.map(a=>a.vector[1]),w:geometry.arrows.map(a=>a.vector[2]),anchor:'tail',sizemode:'absolute',sizeref:.32,colorscale:[[0,color],[1,color]],showscale:false,hoverinfo:'skip',name:'时间方向',legendgroup,showlegend:false};return {type:'scatter',mode:'markers',x:geometry.arrows.map(a=>a.midpoint[0]),y:geometry.arrows.map(a=>a.midpoint[1]),marker:{symbol:'arrow',size:10,color,angle:geometry.arrows.map(a=>a.plotly_up_angle),angleref:'up'},hoverinfo:'skip',name:'时间方向',legendgroup,showlegend:false}}
function detailPath(points){return points.map(q=>`R${q.R}:(${q.r_before},${q.r_i},${q.r_after})`).join(' → ')}function draw(){let a=list(),mode=$('view').value,palette=['#0072B2','#E69F00','#009E73','#CC79A7','#56B4E9','#D55E00'],traces=[],wrapLegend=false,i7Legend=false;a.forEach(x=>{let p=x.points,color=palette[(+x.template.slice(1)-1)%palette.length],hover=p.map(q=>`EID=${x.edge.edge_id}<br>src/dst=${x.edge.source_state}/${x.edge.target_state}<br>input/output=${x.edge.logical_input}/${x.edge.logical_output}<br>s=${x.signal_slice}<br>cycle=${x.cycle_id}; line=${x.sequence_line}<br>R${q.R}: (${q.r_before}, ${q.r_i}, ${q.r_after})<br>source=${q.source}`),dash=x.template_type==='静态模板'?'solid':'dot',common={name:x.id,mode:'lines+markers',line:{color,width:3,dash},marker:{color:p.map(q=>q.r_i===7?'#111827':color),size:7,symbol:p.map(q=>q.r_i===7?'diamond':'circle')},text:hover,hoverinfo:'text'};if(mode==='3d')traces.push({...common,type:'scatter3d',x:p.map(q=>q.r_before),y:p.map(q=>q.r_i),z:p.map(q=>q.r_after)});else traces.push({...common,type:'scatter',x:p.map(q=>mode==='ba'?q.r_before:q.r_i),y:p.map(q=>q.r_after)});let direction=directionTrace(x.visuals[mode],mode,color);if(direction)traces.push(direction);let wrap=wrapTrace(x.visuals[mode],mode);if(wrap){wrap.showlegend=!wrapLegend;wrapLegend=true;traces.push(wrap)}if(!i7Legend&&p.some(q=>q.r_i===7)){let legend={mode:'markers',marker:{symbol:'diamond',color:'#111827',size:9},name:'i=7 特殊值',hoverinfo:'skip',visible:'legendonly'};traces.push(mode==='3d'?{...legend,type:'scatter3d',x:[0],y:[7],z:[0]}:{...legend,type:'scatter',x:[0],y:[0]});i7Legend=true}});let layout=mode==='3d'?{margin:{l:0,r:0,t:25,b:0},scene:{xaxis:{title:'r_before',dtick:1},yaxis:{title:'r_i (7 特殊值)',dtick:1},zaxis:{title:'r_after',dtick:1}}}:{margin:{l:50,r:20,t:25,b:50},xaxis:{title:mode==='ba'?'r_before':'r_i (7 特殊值)',dtick:1},yaxis:{title:'r_after',dtick:1},showlegend:true};Plotly.react('plot',traces,layout,{responsive:true,displaylogo:false});$('summary').textContent=`${a.length} 条成员；线型=模板类型，颜色=精确模板。`;let e=$('eid').value;if(!e)$('formula').textContent='请先选择 EID 查看该边公式。';else{let f=uniq(a.flatMap(x=>x.formulas)),failed=a.some(x=>x.formula_status==='combined_sample_fit_failed');$('formula').innerHTML=f.length?f.map(x=>'<pre>'+esc(x)+'</pre>').join(''):failed?'联合拟合失败，当前无公式':'当前无公式'}$('members').innerHTML='<div class="template-grid">'+uniq(a.map(x=>x.template)).map(t=>{let z=a.filter(x=>x.template===t),sample=z[0],color=palette[(+t.slice(1)-1)%palette.length],memberDetails=z.map(x=>`<details class="member-detail"><summary>${esc(x.id)} — ${esc(x.cycle_id)} / L${x.sequence_line}</summary><p class="member-meta">s=${esc(x.signal_slice)}；每点来源：</p><div class="template-path">${x.points.map(q=>`R${q.R}:(${q.r_before},${q.r_i},${q.r_after}) [${esc(q.source)}]`).join(' → ')}</div></details>`).join('');return `<article class="template-card" style="--template-color:${color}"><h3><span class="tag">${esc(sample.template_type)}</span>${esc(t)}</h3><div class="template-path">${detailPath(sample.points)}</div><div class="member-meta">模板成员 ${z.length} 条；每位成员保留独立 signal 与来源。</div>${memberDetails}</article>`}).join('')+'</div>'}
order.forEach((id,n)=>el(id).addEventListener('change',()=>refresh(n)));$('view').addEventListener('change',draw);refresh();
</script></body></html>'''.replace("__PLOTLY__", plotly).replace("__DATA__", data)
    page = page.replace(
        "common={name:x.id,mode:",
        "common={name:x.id,legendgroup:`member:${x.id}`,mode:",
    ).replace(
        "directionTrace(x.visuals[mode],mode,color)",
        "directionTrace(x.visuals[mode],mode,color,`member:${x.id}`)",
    ).replace(
        "wrapTrace(x.visuals[mode],mode)",
        "wrapTrace(x.visuals[mode],mode,`member:${x.id}`)",
    ).replace(
        "Plotly.react('plot',traces,layout",
        "if(mode==='3d'){Object.assign(layout.scene.xaxis,{range:GLOBAL_RANGES.r_before,fixedrange:true});Object.assign(layout.scene.yaxis,{range:GLOBAL_RANGES.r_i,fixedrange:true});Object.assign(layout.scene.zaxis,{range:GLOBAL_RANGES.r_after,fixedrange:true});layout.scene.aspectmode='data'}else{let globalX=mode==='ba'?'r_before':'r_i';Object.assign(layout.xaxis,{range:GLOBAL_RANGES[globalX],fixedrange:true,scaleanchor:'y',scaleratio:1,constrain:'domain'});Object.assign(layout.yaxis,{range:GLOBAL_RANGES.r_after,fixedrange:true,constrain:'domain'})};layout.legend={groupclick:'togglegroup'};Plotly.react('plot',traces,layout",
    )
    output.write_text(page, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("candidates", "trajectory_scope", "trace", "cycle_cover", "config", "output_html"):
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    args = parser.parse_args()
    candidates, scope = _load(Path(args.candidates)), _load(Path(args.trajectory_scope))
    payload = build_payload(candidates, scope, _load(Path(args.trace)), _load(Path(args.cycle_cover)), _load(Path(args.config)))
    render_html(payload, Path(args.output_html))


if __name__ == "__main__":
    main()
