"""Render immutable v2 trajectory clusters as offline HTML and SVG figures."""
from __future__ import annotations
import argparse, json, math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import plotly.graph_objects as go
from plotly.offline.offline import get_plotlyjs

COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
PLOT_DASHES = ["solid", "dash", "dot", "dashdot", "longdash"]
MPL_DASHES = ["-", "--", ":", "-."]
_CJK_FONT = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")
if _CJK_FONT.exists():
    font_manager.fontManager.addfont(_CJK_FONT)
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=_CJK_FONT).get_name()
plt.rcParams["svg.hashsalt"] = "register-trajectory-clustering-v2"

def _key(item: dict) -> tuple[str, str, str]:
    edge = item["edge"]
    return str(edge.get("logical_input")), str(edge.get("logical_output")), str(item.get("signal_slice"))

def _tier_origin(tier: str) -> set[str]:
    return {"stable"} if tier == "stable_internal" else ({"hypothetical"} if tier == "hypothetical_internal" else {"stable", "hypothetical"})

def panel_members(data: dict, tier: str) -> dict[tuple[str, str, str], dict[str, int]]:
    """Return eligible cluster labels per tier panel; labels are never shared across tiers."""
    items = {item["id"]: item for item in data["trajectories"]}; panels: dict[tuple[str, str, str], dict[str, int]] = {}
    for analysis in data["tiers"].get(tier, []):
        labels = {identifier: number for number, group in enumerate(analysis["clusters"], 1) for identifier in group}
        if labels:
            key = _key(items[next(iter(labels))]); panels[key] = labels
    return panels

def panel_trajectories(data: dict, tier: str) -> dict[tuple[str, str, str], list[dict]]:
    panels = panel_members(data, tier); origins = _tier_origin(tier); output = {key: [] for key in panels}
    for item in data["trajectories"]:
        if item["origin"] not in origins or _key(item) not in output: continue
        if item["id"] in panels[_key(item)] or item["clustering_status"] == "low_discriminability": output[_key(item)].append(item)
    return output

def _wrap(left: dict, right: dict) -> bool:
    return any(left.get(field) == 6 and right.get(field) == 0 for field in ("r_before", "r_after", "i"))

def _cycle_style(cycle_id: str, styles: list[str]) -> str:
    return styles[sum(ord(character) for character in cycle_id) % len(styles)]

def _has_numeric_input(item: dict) -> bool:
    """Use a z axis only when the plotted I/O itself observes a numeric input."""
    return any(sample.get("inputs") or sample.get("effective_region_snapshot", {}).get("numeric_inputs")
               for sample in item.get("samples", []))

def _panel_uses_3d(items: list[dict]) -> bool:
    return bool(items) and all(_has_numeric_input(item) and
                               all(point["i"] is not None for point in item.get("analysis_points", []))
                               for item in items)

def _hover(item: dict, point: dict, cluster: int | None) -> str:
    edge = item["edge"]
    return (f"EID={edge.get('edge_id')}<br>src/dst={edge.get('source_state')}/{edge.get('target_state')}"
            f"<br>input/output={edge.get('logical_input')}/{edge.get('logical_output')}"
            f"<br>cycle ID={item['cycle_id']}; sequence line={item['sequence_line']}"
            f"<br>R{point.get('source_repetition')} ({point['source']}); position={point['cycle_position']}/{point['cycle']}"
            f"<br>r_before={point['r_before']}; r_after={point['r_after']}; i={point['i']}; s={item.get('signal_slice')}"
            f"<br>migration={item.get('migration_status')}; cluster={cluster}; same-phase-imputed={point['same_phase_imputed']}; pattern-completed={point['pattern_completed']}")

def _panel_figure(tier: str, key: tuple[str, str, str], labels: dict[str, int], items: list[dict]) -> dict:
    """Build one dimension-pure Plotly panel.  It is never combined with another panel."""
    is_3d = _panel_uses_3d(items); figure = go.Figure()
    for item in items:
        points = item.get("analysis_points", []); cluster = labels.get(item["id"]); low = item["clustering_status"] == "low_discriminability"
        color = "#9ca3af" if low else COLORS[(cluster - 1) % len(COLORS)]
        meta = {"tier": tier, "io": f"{key[0]}/{key[1]}", "s": key[2], "cluster": str(cluster or "low"), "cycle": item["cycle_id"], "low": low, "role": "trajectory"}
        common = dict(name=item["id"], meta=meta, text=[_hover(item, point, cluster) for point in points], hoverinfo="text", showlegend=False, opacity=.38 if low else 1.0)
        if is_3d:
            figure.add_trace(go.Scatter3d(x=[p["r_before"] for p in points], y=[p["r_after"] for p in points], z=[p["i"] for p in points], mode="lines+markers", line=dict(color=color, width=4, dash=_cycle_style(item["cycle_id"], PLOT_DASHES)), marker=dict(color=["#111111" if p["i"] == 7 else color for p in points], size=4, symbol=["diamond" if p["i"] == 7 else "circle" for p in points]), **common))
            figure.add_trace(go.Scatter3d(
                x=[p["r_before"] for p in points], y=[p["r_after"] for p in points], z=[p["i"] for p in points],
                # WebGL may composite near-transparent markers against a white
                # background.  Use fully transparent, borderless geometry so
                # this hit area stays invisible while retaining hover picking.
                mode="markers", marker=dict(size=16, color="rgba(0,0,0,0)", opacity=0, line=dict(width=0)),
                meta={**meta, "role": "hover-target"}, text=common["text"], hoverinfo="text",
                showlegend=False, name=f"{item['id']} hover target",
            ))
        else:
            figure.add_trace(go.Scatter(x=[p["r_before"] for p in points], y=[p["r_after"] for p in points], mode="lines+markers", line=dict(color=color, dash=_cycle_style(item["cycle_id"], PLOT_DASHES)), marker=dict(color=color, size=7), **common))
            figure.add_trace(go.Scatter(
                x=[p["r_before"] for p in points], y=[p["r_after"] for p in points],
                mode="markers", marker=dict(size=20, color="rgba(0,0,0,0)", opacity=0, line=dict(width=0)),
                meta={**meta, "role": "hover-target"}, text=common["text"], hoverinfo="text",
                showlegend=False, name=f"{item['id']} hover target",
            ))
        if low: continue
        segments = [(left, right) for left, right in zip(points, points[1:])]
        for left, right in segments:
            if not _wrap(left, right): continue
            wrap_meta = {**meta, "role": "wrap"}
            if is_3d: figure.add_trace(go.Scatter3d(x=[left["r_before"],right["r_before"]], y=[left["r_after"],right["r_after"]], z=[left["i"],right["i"]], mode="lines", line=dict(color="#dc2626",dash="dash",width=6), hoverinfo="skip",showlegend=False,meta=wrap_meta,name="6→0 回绕"))
            else: figure.add_trace(go.Scatter(x=[left["r_before"],right["r_before"]], y=[left["r_after"],right["r_after"]], mode="lines", line=dict(color="#dc2626",dash="dash",width=5), hoverinfo="skip",showlegend=False,meta=wrap_meta,name="6→0 回绕"))
        arrows = [(left,right) for left,right in segments if (left["r_before"],left["r_after"],left.get("i")) != (right["r_before"],right["r_after"],right.get("i"))]
        arrow_meta = {**meta, "role": "direction"}
        if is_3d and arrows: figure.add_trace(go.Cone(x=[a["r_before"] for a,_ in arrows],y=[a["r_after"] for a,_ in arrows],z=[a["i"] for a,_ in arrows],u=[b["r_before"]-a["r_before"] for a,b in arrows],v=[b["r_after"]-a["r_after"] for a,b in arrows],w=[b["i"]-a["i"] for a,b in arrows],anchor="tail",sizemode="absolute",sizeref=.32,showscale=False,colorscale=[[0,color],[1,color]],hoverinfo="skip",showlegend=False,meta=arrow_meta,name="方向"))
        if not is_3d and arrows: figure.add_trace(go.Scatter(x=[(a["r_before"]+b["r_before"])/2 for a,b in arrows],y=[(a["r_after"]+b["r_after"])/2 for a,b in arrows],mode="markers",marker=dict(symbol="arrow",size=9,color=color,angle=[math.degrees(math.atan2(b["r_after"]-a["r_after"],b["r_before"]-a["r_before"]))-90 for a,b in arrows]),hoverinfo="skip",showlegend=False,meta=arrow_meta,name="方向"))
    slice_label = "不适用" if str(key[2]) == "not_applicable" else str(key[2])
    title = f"{key[0]} / {key[1]} · s={slice_label}"
    if is_3d:
        figure.add_trace(go.Mesh3d(x=[0,7,7,0],y=[0,0,7,7],z=[7,7,7,7],opacity=.12,color="#64748b",hoverinfo="skip",showlegend=False,meta={"tier":tier,"io":f"{key[0]}/{key[1]}","s":key[2],"role":"plane","low":False},name="i=7 特殊层"))
        figure.update_layout(template="plotly_white",title=title,scene=dict(xaxis_title="r_before",yaxis_title="r_after",zaxis_title="i",zaxis=dict(range=[0,7]),camera=dict(eye=dict(x=1.45,y=1.45,z=1.15))))
    else:
        # The surrounding HTML gives a 2-D panel a square drawing box.  Keeping
        # both explicit ranges avoids Plotly expanding one axis to fill a wide
        # container when scaleanchor is active.
        figure.update_layout(
            template="plotly_white", title=title, autosize=True,
            xaxis_title="r_before", yaxis_title="r_after",
            xaxis=dict(range=[-.2, 7.2], constrain="domain"),
            yaxis=dict(range=[-.2, 7.2], scaleanchor="x", scaleratio=1,
                       constrain="domain"),
        )
    figure.update_layout(margin=dict(l=40,r=20,t=60,b=45),hovermode="closest",hoverdistance=35,hoverlabel=dict(bgcolor="#0f172a",font=dict(color="#f8fafc",size=13),bordercolor="#334155"),uirevision=f"{tier}|{key[0]}|{key[1]}|{key[2]}")
    return figure.to_plotly_json()

def _html_panels(data: dict) -> dict[str, dict]:
    panels = {}
    item_by_id = {item["id"]: item for item in data["trajectories"]}
    for tier in ("stable_internal", "hypothetical_internal", "joint"):
        for key, items in panel_trajectories(data, tier).items():
            identity = f"{tier}|{key[0]}|{key[1]}|{key[2]}"
            labels = panel_members(data, tier)[key]
            analysis = next((entry for entry in data["tiers"].get(tier, [])
                             if entry.get("trajectory_ids") and _key(item_by_id[entry["trajectory_ids"][0]]) == key), {})
            candidates = [{"k": candidate.get("k"), "silhouette": candidate.get("silhouette"), "merge_gap": candidate.get("merge_gap")}
                          for candidate in analysis.get("candidates", [])]
            panels[identity] = {"tier":tier,"io":f"{key[0]}/{key[1]}","s":key[2],"is3d":_panel_uses_3d(items),"figure":_panel_figure(tier,key,labels,items),"summary":{"selected_k":analysis.get("selected_k"),"eligible":len(labels),"low":sum(item.get("clustering_status") == "low_discriminability" for item in items),"candidates":candidates}}
    return panels


def _html_page(serialized_panels: str, counts: dict, plotly_js: str) -> str:
    """Return the self-contained panel switcher used by the offline HTML file.

    A single dimension-pure Plotly canvas is intentionally reused.  In
    particular, a 2-D axis must never be rendered in a leftover 3-D scene.
    """
    page = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>循环轨迹可视化</title>
<style>
:root{font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif;color:#0f172a;background:#f1f5f9}
body{margin:0}.shell{max-width:1600px;margin:auto;padding:24px}.head{display:flex;justify-content:space-between;gap:20px;align-items:end}.head h1{margin:0;font-size:27px}.sub{color:#475569}.cards,.controls{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.card,.control,.canvas,.cluster-summary{background:#fff;border:1px solid #dbe3ef;border-radius:12px;box-shadow:0 1px 3px #0f172a12}.card{padding:12px 16px;min-width:115px}.card b{display:block;font-size:22px}.control{padding:12px;display:flex;gap:10px;align-items:center}.control label{font-size:13px;color:#475569}.control select,.seg button{border:1px solid #cbd5e1;border-radius:8px;background:#fff;padding:7px 9px;color:#0f172a}.seg button.active{background:#0f766e;color:#fff;border-color:#0f766e}.canvas{padding:12px;min-height:704px;display:flex;justify-content:center;align-items:flex-start;overflow:hidden}.canvas.two-d{min-height:0}.canvas #trajectory-canvas{width:100%;height:680px}.canvas.two-d #trajectory-canvas{width:min(100%,760px);height:auto;aspect-ratio:1/1}.summary{padding:10px 2px;color:#334155}.cluster-summary{padding:12px;margin:0 0 12px}.cluster-summary details{margin-top:8px}.cluster-summary table{border-collapse:collapse;margin-top:8px;font-size:13px}.cluster-summary th,.cluster-summary td{padding:4px 9px;border:1px solid #dbe3ef;text-align:right}.warning{color:#9a3412;background:#fff7ed;padding:8px;border-radius:7px}.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:#475569}.red{color:#dc2626}.gray{color:#6b7280}@media(max-width:700px){.shell{padding:14px}.head{display:block}.canvas{min-height:524px}.canvas #trajectory-canvas{height:500px}.canvas.two-d{min-height:0}.canvas.two-d #trajectory-canvas{height:auto;width:100%}}
</style></head><body><main class="shell"><header class="head"><div><h1>循环寄存器轨迹</h1><p class="sub">每次只显示一个 I/O 与信号量切片。i=7 是特殊层；红色虚线为任意坐标的 6→0 模7回绕。</p></div><div class="legend"><span class="red">━ ━ 6→0 回绕</span><span class="gray">━ 灰色：低辨别力（无箭头）</span></div></header><section class="cards" id="cards"></section><section class="controls" aria-label="轨迹筛选"><div class="control seg" id="tier-buttons"></div><div class="control"><label>I/O<br><select id="io"></select></label></div><div class="control"><label>信号量 s<br><select id="slice"></select></label></div><div class="control"><label>簇<br><select id="cluster"></select></label></div><div class="control"><label>循环<br><select id="cycle"></select></label></div><div class="control"><label><input id="low" type="checkbox" checked> 显示低辨别力背景</label></div></section><div class="summary" id="summary" aria-live="polite"></div><section class="cluster-summary" id="cluster-summary"></section><section class="canvas" id="canvas-shell"><div id="trajectory-canvas"></div></section></main><script>__PLOTLY__</script><script>
const PANELS=__PANELS__, COUNTS=__COUNTS__;
const tiers=['stable_internal','hypothetical_internal','joint']; let tier='stable_internal', current=null;
const $=id=>document.getElementById(id), uniq=a=>[...new Set(a)].sort();
const text={all:'全部',not_applicable:'不适用',low:'低辨别力（未聚类）',stable_internal:'相对稳定推断',hypothetical_internal:'假设性候选',joint:'联合'};
const display=(value,kind='')=>kind==='cluster'&&value!=='all'&&value!=='low'?`簇 ${value}`:(text[value]??String(value));
function opts(el,values,value,kind=''){el.replaceChildren(...values.map(v=>new Option(display(v,kind),v)));el.value=value&&values.includes(value)?value:values[0]}
function visibleFigure(panel){const c=$('cluster').value,cy=$('cycle').value,low=$('low').checked,fig=structuredClone(panel.figure);fig.data=fig.data.filter(t=>{const m=t.meta||{};return (!m.role||m.role==='plane'||((c==='all'||m.cluster===c)&&(cy==='all'||m.cycle===cy)&&(low||!m.low)))});return fig}
function clusterSummary(panel){const x=panel.summary,gap=x.candidates.some(c=>!Number.isFinite(Number(c.merge_gap))||Number(c.merge_gap)>1e6);const rows=x.candidates.map(c=>`<tr><td>${c.k}</td><td>${Number(c.silhouette).toFixed(4)}</td><td>${Number(c.merge_gap).toExponential(3)}</td></tr>`).join('');const body=rows||'<tr><td colspan="3">没有可供自动分簇比较的候选。</td></tr>';$('cluster-summary').innerHTML=`<b>当前面板聚类摘要</b>：自动选择 ${x.selected_k??'—'} 簇；参与聚类 ${x.eligible} 条；低辨别力背景 ${x.low} 条。${gap?'<p class="warning">候选表存在接近零的合并高度，导致 merge gap 数值放大；该提示不改变聚类标签，解读时请结合聚类报告。</p>':''}<details><summary>查看自动选簇指标</summary><table><thead><tr><th>候选簇数</th><th>silhouette</th><th>merge gap</th></tr></thead><tbody>${body}</tbody></table></details>`}
function setCanvasMode(is3d){const shell=$('canvas-shell'),canvas=$('trajectory-canvas');shell.classList.toggle('two-d',!is3d);canvas.style.width=is3d?'100%':'';canvas.style.height=is3d?'680px':'auto';canvas.style.aspectRatio=is3d?'auto':'1 / 1'}
function resize(){requestAnimationFrame(()=>requestAnimationFrame(()=>Plotly.Plots.resize('trajectory-canvas')))}
function render(){const panel=PANELS[current],fig=visibleFigure(panel);setCanvasMode(panel.is3d);Plotly.react('trajectory-canvas',fig.data,fig.layout,{responsive:true,displaylogo:false}).then(resize);const trajectories=fig.data.filter(t=>(t.meta||{}).role==='trajectory').length;$('summary').textContent=`当前面板：${panel.io} · s=${display(panel.s)} · ${panel.is3d?'三维 r_before / r_after / i':'二维 r_before / r_after'} · 显示 ${trajectories} 条轨迹`;clusterSummary(panel)}
function choosePanel(){const io=$('io').value,s=$('slice').value;current=Object.keys(PANELS).find(k=>PANELS[k].tier===tier&&PANELS[k].io===io&&String(PANELS[k].s)===s)||Object.keys(PANELS).find(k=>PANELS[k].tier===tier);const p=PANELS[current],tr=p.figure.data.filter(t=>(t.meta||{}).role==='trajectory');opts($('cluster'),['all',...uniq(tr.map(t=>String(t.meta.cluster)))],'all','cluster');opts($('cycle'),['all',...uniq(tr.map(t=>String(t.meta.cycle)))],'all');render()}
function rebuild(){const choices=Object.values(PANELS).filter(p=>p.tier===tier);opts($('io'),uniq(choices.map(p=>p.io)),$('io').value);opts($('slice'),uniq(choices.filter(p=>p.io===$('io').value).map(p=>String(p.s))),$('slice').value);choosePanel()}
function init(){const cards=[['提取',COUNTS.extracted_total],['实际聚类',COUNTS.actually_clustered],['低辨别力',COUNTS.low_discriminability],['迁移成功',COUNTS.migration_succeeded_total]];$('cards').innerHTML=cards.map(x=>`<div class="card"><span>${x[0]}</span><b>${x[1]??'—'}</b></div>`).join('');tiers.forEach(t=>{const b=document.createElement('button');b.textContent=display(t);b.onclick=()=>{tier=t;[...$('tier-buttons').children].forEach(x=>x.classList.toggle('active',x===b));rebuild()};$('tier-buttons').append(b)});$('tier-buttons').firstChild.classList.add('active');['io','slice'].forEach(id=>$(id).onchange=rebuild);['cluster','cycle','low'].forEach(id=>$(id).onchange=render);window.addEventListener('resize',resize);rebuild()}init();
</script></body></html>"""
    return (page.replace("__PLOTLY__", plotly_js)
            .replace("__PANELS__", serialized_panels)
            .replace("__COUNTS__", json.dumps(counts, ensure_ascii=False,
                                                 sort_keys=True, separators=(",", ":"))))


def html(data: dict, output: Path) -> None:
    panels = _html_panels(data); serialized = json.dumps(panels, ensure_ascii=False, sort_keys=True, separators=(",",":"))
    counts = data.get("counts", {}); plotly_js = get_plotlyjs()
    output.write_text(_html_page(serialized, counts, plotly_js), encoding="utf-8")

def _svg_line(axis, points, color, low, has_input, cycle_id):
    for left, right in zip(points, points[1:]):
        wrapped = _wrap(left, right); style = "--" if wrapped else _cycle_style(cycle_id, MPL_DASHES); line_color = "#dc2626" if wrapped else color
        if has_input: axis.plot([left["r_before"], right["r_before"]], [left["r_after"], right["r_after"]], [left["i"], right["i"]], color=line_color, linestyle=style, alpha=.38 if low else 1)
        else: axis.plot([left["r_before"], right["r_before"]], [left["r_after"], right["r_after"]], color=line_color, linestyle=style, alpha=.38 if low else 1)
        if not low:
            if has_input: axis.quiver(left["r_before"], left["r_after"], left["i"], right["r_before"]-left["r_before"], right["r_after"]-left["r_after"], right["i"]-left["i"], color=color, arrow_length_ratio=.12, linewidth=.6)
            else: axis.annotate("", (right["r_before"], right["r_after"]), (left["r_before"], left["r_after"]), arrowprops={"arrowstyle":"->", "color":color, "lw":.7})

def svg(data: dict, tier: str, output: Path) -> None:
    labels = panel_members(data, tier); panels = panel_trajectories(data, tier); count = max(1, len(panels)); fig = plt.figure(figsize=(6*count, 5))
    for index, (key, items) in enumerate(panels.items(), 1):
        all_3d = _panel_uses_3d(items)
        axis = fig.add_subplot(1, count, index, projection="3d" if all_3d else None)
        if all_3d:
            axis.add_collection3d(Poly3DCollection([[(0,0,7),(7,0,7),(7,7,7),(0,7,7)]], alpha=.10, facecolor="gray"))
        for item in items:
            points = item.get("analysis_points", []); low = item["clustering_status"] == "low_discriminability"; cluster = labels[key].get(item["id"]); color = "#a0a0a0" if low else COLORS[(cluster-1)%len(COLORS)]
            has_input = all_3d
            _svg_line(axis, points, color, low, has_input, item["cycle_id"])
            if has_input: axis.scatter([p["r_before"] for p in points], [p["r_after"] for p in points], [p["i"] for p in points], c=["black" if p["i"] == 7 else color for p in points], marker="o", s=12, alpha=.38 if low else 1)
            else: axis.scatter([p["r_before"] for p in points], [p["r_after"] for p in points], c=color, s=12, alpha=.38 if low else 1)
        axis.set_title(f"{key[0]}/{key[1]}, s={key[2]}"); axis.set_xlabel("r_before"); axis.set_ylabel("r_after")
        if all_3d: axis.set_zlabel("i"); axis.set_zlim(0,7)
    fig.legend(handles=[Line2D([0],[0],color="#a0a0a0",label="低辨别力（无箭头）"), Line2D([0],[0],color="#dc2626",linestyle="--",label="6→0：模7回绕"), Line2D([0],[0],marker="o",color="black",linestyle="",label="i=7 特殊层")], loc="lower center", ncol=3)
    fig.tight_layout(rect=(0,.08,1,1)); fig.savefig(output, format="svg", metadata={"Date": None}); plt.close(fig)

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--input", required=True); parser.add_argument("--output-html"); parser.add_argument("--output-svg-dir"); parser.add_argument("--output-dir")
    args = parser.parse_args(argv); data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if data.get("schema") != "register-trajectory-clustering-v2": raise ValueError("可视化只支持 register-trajectory-clustering-v2")
    if args.output_dir and (args.output_html or args.output_svg_dir): raise ValueError("--output-dir 不能与分别指定的输出同时使用")
    directory = Path(args.output_dir) if args.output_dir else None; html_path = Path(args.output_html) if args.output_html else (directory / "trajectory-visualization.html" if directory else None); svg_dir = Path(args.output_svg_dir) if args.output_svg_dir else directory
    if not html_path or not svg_dir: raise ValueError("必须指定 --output-html 和 --output-svg-dir，或兼容的 --output-dir")
    html_path.parent.mkdir(parents=True, exist_ok=True); svg_dir.mkdir(parents=True, exist_ok=True); html(data, html_path)
    for tier, filename in (("stable_internal","stable.svg"),("hypothetical_internal","hypothetical.svg"),("joint","joint.svg")): svg(data, tier, svg_dir / filename)
    return 0
if __name__ == "__main__": raise SystemExit(main())
