"""Build a standalone three-column VCB graph without changing the canonical graph."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "knowledge_graph" / "vcb_trip_knowledge_graph.json"
OUTPUT = ROOT / "knowledge_graph" / "vcb_work_order_action_component_graph.html"
OUTPUT_JSON = ROOT / "knowledge_graph" / "vcb_work_order_action_component_graph.json"

ACTION_ORDER = (
    "reset",
    "checked",
    "function tested",
    "tested",
    "monitored",
    "replaced",
    "swapped",
    "isolated",
    "repaired",
)
ACTION_LABELS = {
    "reset": "复位",
    "checked": "检查",
    "function tested": "功能测试",
    "tested": "测试",
    "monitored": "持续监测",
    "replaced": "更换",
    "swapped": "互换测试",
    "isolated": "隔离",
    "repaired": "修复",
}


def unique_extend(target: list[Any], values: Any) -> None:
    for value in values if isinstance(values, list) else [values]:
        if value not in (None, "") and value not in target:
            target.append(value)


def action_keys(node: dict[str, Any]) -> list[str]:
    present = {str(value).strip().casefold() for value in node.get("member_actions", [])}
    return [key for key in ACTION_ORDER if key in present]


def build_display_data() -> dict[str, Any]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    node_by_id = {node["id"]: node for node in source["nodes"]}
    work_nodes: OrderedDict[str, dict[str, Any]] = OrderedDict()
    method_nodes: OrderedDict[str, dict[str, Any]] = OrderedDict()
    component_nodes: OrderedDict[str, dict[str, Any]] = OrderedDict()
    edges: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    method_id_map: dict[str, str] = {}

    def connect(source_id: str, target_id: str, relationship_type: str, evidence: dict[str, Any]) -> None:
        edge = edges.setdefault(
            (source_id, target_id),
            {
                "id": f"display:{source_id}->{target_id}",
                "source": source_id,
                "target": target_id,
                "type": relationship_type,
                "work_orders": [],
                "canonical_edge_ids": [],
            },
        )
        unique_extend(edge["work_orders"], evidence.get("work_orders", []))
        unique_extend(edge["canonical_edge_ids"], evidence.get("id"))

    maintenance_to_work = [edge for edge in source["edges"] if edge.get("type") == "maintenance_to_work_order"]
    equipment_to_maintenance = [edge for edge in source["edges"] if edge.get("type") == "equipment_to_maintenance"]

    for edge in maintenance_to_work:
        canonical_method = node_by_id.get(edge.get("source"))
        canonical_work = node_by_id.get(edge.get("target"))
        if not canonical_method or not canonical_work:
            continue
        keys = action_keys(canonical_method)
        category_key = "|".join(keys) or "other"
        method_id = f"method_category:{category_key}"
        method_id_map[canonical_method["id"]] = method_id
        method = method_nodes.setdefault(
            method_id,
            {
                "id": method_id,
                "type": "method",
                "label": " / ".join(ACTION_LABELS[key] for key in keys) or "维修处置",
                "work_orders": [],
                "canonical_node_ids": [],
                "member_methods": [],
                "affected_equipment": [],
            },
        )
        unique_extend(method["work_orders"], edge.get("work_orders", []))
        unique_extend(method["canonical_node_ids"], canonical_method["id"])
        unique_extend(method["member_methods"], canonical_method.get("member_methods", []))
        unique_extend(method["affected_equipment"], canonical_method.get("affected_equipment", []))

        work_id = canonical_work["id"]
        if work_id not in work_nodes:
            work_nodes[work_id] = {
                "id": work_id,
                "type": "work_order",
                "label": f"工单 {canonical_work.get('work_order_no') or work_id.removeprefix('work_order:')}",
                "work_order": canonical_work.get("work_order_no") or work_id.removeprefix("work_order:"),
                "train": canonical_work.get("train") or "—",
                "car_no": canonical_work.get("car_no") or "—",
                "fault": canonical_work.get("fault") or "",
            }
        connect(work_id, method_id, "work_order_to_method", edge)

    active_work_orders = set(work_nodes)
    for edge in equipment_to_maintenance:
        method_id = method_id_map.get(str(edge.get("target") or ""))
        canonical_component = node_by_id.get(edge.get("source"))
        if not method_id or not canonical_component:
            continue
        evidence_orders = [
            value for value in edge.get("work_orders", [])
            if f"work_order:{value}" in active_work_orders
        ]
        if not evidence_orders:
            continue
        component_id = canonical_component["id"]
        component = component_nodes.setdefault(
            component_id,
            {
                "id": component_id,
                "type": "component",
                "label": canonical_component.get("label") or "未命名部件",
                "work_orders": [],
                "member_equipment": list(canonical_component.get("member_equipment") or []),
            },
        )
        unique_extend(component["work_orders"], evidence_orders)
        connect(method_id, component_id, "method_to_component", {**edge, "work_orders": evidence_orders})

    return {
        "meta": {
            "title": "VCB Trip 工单维修知识图谱",
            "source_file": SOURCE.name,
            "generated_at": source.get("meta", {}).get("generated_at"),
            "work_order_count": len(work_nodes),
            "method_count": len(method_nodes),
            "component_count": len(component_nodes),
            "edge_count": len(edges),
        },
        "nodes": [*work_nodes.values(), *method_nodes.values(), *component_nodes.values()],
        "edges": list(edges.values()),
    }


def build_import_data(display_data: dict[str, Any]) -> dict[str, Any]:
    root_id = "vcb_trip"
    nodes = [
        {
            "id": root_id,
            "type": "root",
            "label": "vcb_trip",
            "name": "vcb_trip",
        },
        *display_data["nodes"],
    ]
    root_edges = [
        {
            "id": f"root:{root_id}->{node['id']}",
            "source": root_id,
            "target": node["id"],
            "type": "root_to_work_order",
            "work_orders": [node["work_order"]],
        }
        for node in display_data["nodes"]
        if node["type"] == "work_order"
    ]
    return {
        "meta": {
            **display_data["meta"],
            "graph_scope": "vcb_trip",
            "root_id": root_id,
            "node_count": len(nodes),
            "edge_count": len(root_edges) + len(display_data["edges"]),
        },
        "nodes": nodes,
        "edges": [*root_edges, *display_data["edges"]],
    }


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VCB Trip 工单维修知识图谱</title>
<style>
:root{--panel:rgba(8,28,46,.52);--inner:rgba(88,116,134,.30);--border:rgba(205,230,242,.14);--text:#f2f8fb;--muted:rgba(205,222,231,.62);--accent:#79d5fa}
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden}body{color:var(--text);background:#04101c;font:13px/1.5 "HarmonyOS Sans SC","PingFang SC","Microsoft YaHei","Segoe UI",sans-serif}
body::before{position:fixed;z-index:-2;inset:-6px;background:linear-gradient(rgba(2,11,20,.22),rgba(3,15,27,.38)),url("../assets/dashboard-bg-user.png") center/cover no-repeat;filter:blur(3px) brightness(1.08) saturate(1.05);transform:scale(1.02);content:""}
.app{display:grid;width:100%;height:100%;padding:14px;grid-template-rows:64px minmax(0,1fr);gap:10px}.header,.graph-shell{position:relative;overflow:hidden;border:1px solid var(--border);border-radius:12px;background:linear-gradient(135deg,rgba(255,255,255,.055),transparent 32%),var(--panel);backdrop-filter:blur(16px) saturate(125%);box-shadow:0 14px 32px rgba(0,0,0,.24),inset 0 1px 0 rgba(255,255,255,.065)}
.header::after,.graph-shell::after{position:absolute;inset:0;pointer-events:none;background:linear-gradient(115deg,rgba(255,255,255,.11),transparent 18%,transparent 78%,rgba(121,213,250,.045));content:""}.header{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:9px 18px}.title{text-align:center}.title h1{margin:0;font-size:25px;font-weight:500;letter-spacing:.5px}.title small{color:var(--muted);font-size:9px;letter-spacing:1.2px}.meta{display:flex;gap:16px;color:var(--muted);font-size:11px}.meta.right{justify-content:flex-end}.meta b{color:#bdeaff;font-weight:500}
.graph-shell{display:grid;min-height:0;grid-template-rows:58px 56px minmax(0,1fr)}.toolbar{position:relative;z-index:1;display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;padding:9px 16px;border-bottom:1px solid rgba(205,230,242,.09)}.toolbar-group{display:flex;align-items:center;gap:8px}.toolbar input,.toolbar select,.toolbar button{height:34px;border:1px solid rgba(205,230,242,.13);border-radius:7px;background:rgba(16,42,60,.55);color:#dceaf0;font:11px inherit}.toolbar input{width:250px;padding:0 11px}.toolbar select{width:180px;padding:0 9px}.toolbar button{padding:0 13px;cursor:pointer}.toolbar button:hover{border-color:rgba(121,213,250,.38);background:rgba(40,78,101,.50)}.hint{color:var(--muted);font-size:10px}
.column-heads{position:relative;z-index:1;display:grid;grid-template-columns:29% 31% 40%;align-items:center;padding:0 24px;border-bottom:1px solid rgba(205,230,242,.08)}.column-head{display:flex;align-items:center;gap:10px}.column-head:nth-child(2){justify-content:center}.column-head:nth-child(3){justify-content:flex-end;padding-right:20px}.column-icon{display:grid;width:30px;height:30px;place-items:center;border:1px solid rgba(121,213,250,.22);border-radius:8px;background:rgba(55,93,115,.28);color:#a9e5fb}.column-head b{font-size:15px;font-weight:500}.column-head small{display:block;color:rgba(164,187,200,.48);font-size:8px;letter-spacing:.7px}
.viewport{position:relative;min-height:0;overflow:auto;background:transparent;scrollbar-width:thin;scrollbar-color:rgba(121,213,250,.28) transparent}.viewport svg{display:block;width:100%;min-width:1100px;height:auto}.edge{fill:none;stroke:rgba(194,224,239,.22);stroke-width:.85}.edge.highlight{stroke:rgba(120,215,255,.90);stroke-width:1.7}.node{cursor:pointer}.frame{fill:rgba(88,116,134,.32);stroke:rgba(210,235,247,.16);stroke-width:.8;filter:drop-shadow(0 8px 13px rgba(0,0,0,.22));transition:fill .15s ease,stroke .15s ease}.rim{fill:none;stroke:rgba(255,255,255,.10);stroke-width:.7;pointer-events:none}.node:hover .frame{fill:rgba(104,136,156,.38);stroke:rgba(210,235,247,.25)}.node.selected .frame{fill:rgba(75,132,164,.58);stroke:rgba(125,220,255,.78);stroke-width:1.4}.node.faded,.edge.faded{opacity:.14}.port{fill:rgba(173,221,241,.52)}.icon-disc{fill:rgba(32,62,80,.55);stroke:rgba(205,230,242,.15)}.icon-line{fill:none;stroke:#d8e8ef;stroke-width:1.2;stroke-linecap:round;stroke-linejoin:round}.node-title{fill:rgba(247,251,253,.95);font-size:12.5px;font-weight:500}.node-sub{fill:rgba(211,225,233,.62);font-size:9.5px}.empty{display:grid;height:100%;place-items:center;color:var(--muted)}
@media(max-width:1180px){.app{width:1180px}.title h1{font-size:22px}}
</style>
</head>
<body>
<main class="app">
  <header class="header">
    <div class="meta"><span>数据源 <b id="source"></b></span><span>原始图谱保持不变</span></div>
    <div class="title"><h1>VCB Trip 工单维修知识图谱</h1><small>WORK ORDER · MAINTENANCE ACTION · RELATED COMPONENT</small></div>
    <div class="meta right"><span>工单 <b id="workCount"></b></span><span>措施 <b id="methodCount"></b></span><span>部件 <b id="componentCount"></b></span></div>
  </header>
  <section class="graph-shell">
    <div class="toolbar">
      <div class="toolbar-group"><input id="search" placeholder="搜索工单、维修措施或相关部件"><select id="workOrder"><option value="">全部工单</option></select><button id="reset" type="button">重置</button></div>
      <div class="hint">点击工单可多选并高亮对应维修路径</div>
    </div>
    <div class="column-heads">
      <div class="column-head"><span class="column-icon">▤</span><span><b>工单</b><small>WORK ORDERS</small></span></div>
      <div class="column-head"><span class="column-icon">⌕</span><span><b>维修措施</b><small>MAINTENANCE ACTIONS</small></span></div>
      <div class="column-head"><span class="column-icon">◇</span><span><b>相关部件</b><small>RELATED COMPONENTS</small></span></div>
    </div>
    <div class="viewport"><svg id="graph" role="img" aria-label="工单维修知识图谱"></svg></div>
  </section>
</main>
<script>
const DATA=__DATA__;
const NS="http://www.w3.org/2000/svg",svg=document.getElementById("graph"),search=document.getElementById("search"),workOrder=document.getElementById("workOrder");
const nodeById=new Map(DATA.nodes.map(node=>[node.id,node]));let selectedNode="";const selectedWorkOrders=new Set();
function el(name,attrs={},text=""){const node=document.createElementNS(NS,name);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value));if(text)node.textContent=text;return node}
function unique(values){return [...new Set(values)]}function short(value,n=30){const text=String(value||"");return text.length>n?text.slice(0,n-1)+"…":text}
function setup(){source.textContent=DATA.meta.source_file;workCount.textContent=DATA.meta.work_order_count;methodCount.textContent=DATA.meta.method_count;componentCount.textContent=DATA.meta.component_count;DATA.nodes.filter(node=>node.type==="work_order").sort((a,b)=>a.work_order.localeCompare(b.work_order)).forEach(node=>{const option=document.createElement("option");option.value=node.work_order;option.textContent=`${node.work_order} · ${node.train}/${node.car_no}`;workOrder.appendChild(option)});render()}
function visible(){const q=search.value.trim().toLowerCase(),wo=workOrder.value;let ids=new Set(DATA.nodes.filter(node=>!wo||(node.work_orders||[node.work_order]).includes(wo)).map(node=>node.id));if(q){const matched=new Set(DATA.nodes.filter(node=>JSON.stringify(node).toLowerCase().includes(q)).map(node=>node.id));const expanded=new Set(matched);DATA.edges.forEach(edge=>{if(matched.has(edge.source)||matched.has(edge.target)){expanded.add(edge.source);expanded.add(edge.target)}});ids=new Set([...ids].filter(id=>expanded.has(id)))}return ids}
function distribute(count,height){if(!count)return[];if(count===1)return[(height-66)/2+16];const gap=Math.max(76,(height-92)/(count-1));return Array.from({length:count},(_,index)=>16+index*gap)}
function addText(group,x,y,text,cls,max=27,limit=2){const words=String(text||"").split(" / "),lines=[];let line="";for(const word of words){const next=line?`${line} / ${word}`:word;if(next.length<=max)line=next;else{if(line)lines.push(line);line=word}}if(line)lines.push(line);if(lines.length>limit){lines.length=limit;lines[limit-1]=short(lines[limit-1],max)}const node=el("text",{x,y,class:cls});lines.forEach((value,index)=>node.appendChild(el("tspan",{x,dy:index?14:0},value)));group.appendChild(node)}
function render(){const ids=visible(),columns={work_order:[],method:[],component:[]};DATA.nodes.filter(node=>ids.has(node.id)).forEach(node=>columns[node.type].push(node));columns.work_order.sort((a,b)=>a.work_order.localeCompare(b.work_order));columns.method.sort((a,b)=>a.label.localeCompare(b.label,"zh-CN"));columns.component.sort((a,b)=>a.label.localeCompare(b.label,"zh-CN"));const max=Math.max(1,...Object.values(columns).map(column=>column.length)),height=Math.max(620,max*78+28),width=1200,x={work_order:34,method:480,component:900},w={work_order:230,method:220,component:260};svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.innerHTML="";const positions=new Map();Object.entries(columns).forEach(([type,nodes])=>{const ys=distribute(nodes.length,height);nodes.forEach((node,index)=>positions.set(node.id,{x:x[type],y:ys[index]}))});DATA.edges.filter(edge=>ids.has(edge.source)&&ids.has(edge.target)).forEach(edge=>{const a=positions.get(edge.source),b=positions.get(edge.target);if(!a||!b)return;const x1=a.x+w[nodeById.get(edge.source).type],x2=b.x,y1=a.y+33,y2=b.y+33,m=(x1+x2)/2;const path=el("path",{class:"edge",d:`M${x1} ${y1} C${m} ${y1},${m} ${y2},${x2} ${y2}`});path.dataset.id=edge.id;path.dataset.source=edge.source;path.dataset.target=edge.target;svg.appendChild(path)});Object.values(columns).flat().forEach(node=>createNode(node,positions.get(node.id),w[node.type]));applyHighlight()}
function createNode(node,pos,width){const group=el("g",{class:`node ${node.type}`,transform:`translate(${pos.x},${pos.y})`});group.dataset.id=node.id;group.append(el("rect",{class:"frame",width,height:66,rx:8}),el("rect",{class:"rim",x:.8,y:.8,width:width-1.6,height:64.4,rx:8}),el("circle",{class:"port",cx:width-2,cy:33,r:2.4}));if(node.type!=="work_order")group.appendChild(el("circle",{class:"port",cx:2,cy:33,r:2.4}));group.appendChild(el("circle",{class:"icon-disc",cx:25,cy:25,r:12}));const icon=node.type==="work_order"?"▤":node.type==="method"?"⌕":"◇";group.appendChild(el("text",{x:25,y:29,"text-anchor":"middle",class:"node-title"},icon));addText(group,45,node.type==="work_order"?24:node.label.includes(" / ")?24:35,node.label,"node-title",node.type==="component"?30:25,2);if(node.type==="work_order")group.appendChild(el("text",{x:45,y:49,class:"node-sub"},`车号 ${node.train} · 车厢 ${node.car_no}`));group.addEventListener("click",()=>selectNode(node));svg.appendChild(group)}
function selectNode(node){if(node.type==="work_order"){selectedNode="";selectedWorkOrders.has(node.work_order)?selectedWorkOrders.delete(node.work_order):selectedWorkOrders.add(node.work_order)}else{selectedWorkOrders.clear();selectedNode=selectedNode===node.id?"":node.id}applyHighlight()}
function applyHighlight(){const nodes=[...svg.querySelectorAll(".node")],edges=[...svg.querySelectorAll(".edge")];if(selectedWorkOrders.size){const activeNodes=new Set(),activeEdges=new Set();DATA.edges.forEach(edge=>{if((edge.work_orders||[]).some(workOrder=>selectedWorkOrders.has(workOrder))){activeNodes.add(edge.source);activeNodes.add(edge.target);activeEdges.add(edge.id)}});nodes.forEach(element=>{const node=nodeById.get(element.dataset.id),selected=node?.type==="work_order"&&selectedWorkOrders.has(node.work_order);element.classList.toggle("selected",selected);element.classList.toggle("faded",!activeNodes.has(element.dataset.id))});edges.forEach(element=>{element.classList.toggle("highlight",activeEdges.has(element.dataset.id));element.classList.toggle("faded",!activeEdges.has(element.dataset.id))});return}if(selectedNode){const neighbours=new Set([selectedNode]);edges.forEach(edge=>{if(edge.dataset.source===selectedNode)neighbours.add(edge.dataset.target);if(edge.dataset.target===selectedNode)neighbours.add(edge.dataset.source)});nodes.forEach(element=>{element.classList.toggle("selected",element.dataset.id===selectedNode);element.classList.toggle("faded",!neighbours.has(element.dataset.id))});edges.forEach(edge=>{const active=edge.dataset.source===selectedNode||edge.dataset.target===selectedNode;edge.classList.toggle("highlight",active);edge.classList.toggle("faded",!active)});return}nodes.forEach(element=>element.classList.remove("selected","faded"));edges.forEach(element=>element.classList.remove("highlight","faded"))}
search.addEventListener("input",()=>{selectedNode="";selectedWorkOrders.clear();render()});workOrder.addEventListener("change",()=>{selectedNode="";selectedWorkOrders.clear();render()});document.getElementById("reset").addEventListener("click",()=>{search.value="";workOrder.value="";selectedNode="";selectedWorkOrders.clear();render()});setup();
</script>
</body>
</html>'''


def main() -> None:
    data = build_display_data()
    embedded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    OUTPUT.write_text(HTML.replace("__DATA__", embedded), encoding="utf-8")
    import_data = build_import_data(data)
    OUTPUT_JSON.write_text(json.dumps(import_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data["meta"], ensure_ascii=False, indent=2))
    print(f"新图谱：{OUTPUT}")
    print(f"Neo4j 导入数据：{OUTPUT_JSON}")


if __name__ == "__main__":
    main()
