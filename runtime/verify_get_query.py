import json
import time

import app_visualize as app


started = time.perf_counter()
payload = app.download_query_data(
    None,
    {"search": "?equipInstance=T25&equipUnit=09&eventTime=2026-05-18%2021%3A25%3A52&eventCode=1757", "revision": "server-verification"},
    "T25",
    "09",
    "2026-05-18 21:25:52",
    "1757",
    60,
    30,
    3,
    0.7,
    ["cst"],
)
print("DOWNLOAD=" + json.dumps({
    "ok": payload.get("ok"),
    "message": payload.get("message"),
    "row_count": payload.get("row_count"),
    "metric_count": payload.get("metric_count"),
    "elapsed_seconds": round(time.perf_counter() - started, 2),
}, ensure_ascii=False))

if payload.get("ok"):
    analyzed = app.analyze_query_data(payload, [], "verify-get-interface")
    rows = analyzed[1]
    print("ANALYSIS=" + json.dumps({
        "result_count": len(rows),
        "work_orders": [row.get("work_order_no") for row in rows],
        "similarities": [row.get("similarity") for row in rows],
        "status": analyzed[2],
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False))
    query = app.get_session_query("verify-get-interface")
    graph = app.build_top3_work_order_knowledge_graph_payload(query)
    component_labels = [
        node.get("label")
        for node in graph.get("nodes", [])
        if node.get("type") == "component"
    ]
    grouped_labels = {group[1] for group in app._KG_COMPONENT_GROUPS}
    nodes_by_id = {
        node.get("id"): node
        for node in graph.get("nodes", [])
        if node.get("id")
    }
    replacement_targets = {}
    action_targets = {}
    for work_order in ("5001505712", "5001331067", "5001340533"):
        labels = []
        per_action = {}
        for edge in graph.get("edges", []):
            if work_order not in (edge.get("work_orders") or []):
                continue
            source = nodes_by_id.get(edge.get("source"), {})
            target = nodes_by_id.get(edge.get("target"), {})
            if source.get("type") == "method" and target.get("type") == "component":
                per_action.setdefault(source.get("label"), []).append(target.get("label"))
            if source.get("type") == "method" and source.get("label") == "更换" and target.get("type") == "component":
                labels.append(target.get("label"))
        replacement_targets[work_order] = sorted(set(labels))
        action_targets[work_order] = {
            action: sorted(set(targets))
            for action, targets in sorted(per_action.items())
        }
    print("COMPONENTS=" + json.dumps({
        "count": len(component_labels),
        "labels": component_labels,
        "grouped_labels_present": sorted(set(component_labels) & grouped_labels),
        "replacement_targets": replacement_targets,
        "action_targets": action_targets,
    }, ensure_ascii=False))
    selected_work_order = "5001505712"
    compact_figure, compact_status = app.build_raw_overlay_comparison(
        query,
        selected_work_order,
        compact=True,
    )
    detail_figure, detail_status = app.build_raw_overlay_comparison(
        query,
        selected_work_order,
        compact=False,
    )
    print("RAW_COMPARISON=" + json.dumps({
        "work_order": selected_work_order,
        "compact_traces": len(compact_figure.data),
        "compact_height": compact_figure.layout.height,
        "compact_status": compact_status,
        "detail_traces": len(detail_figure.data),
        "detail_height": detail_figure.layout.height,
        "detail_status": detail_status,
        "alignment_shifts_seconds": detail_figure.layout.meta.get("alignment_shifts_seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False))
