import json

import app_visualize as app


payload = app.download_query_data(
    None,
    {"search": "candidate", "revision": "signal-union"},
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
assert payload.get("ok"), payload
app.analyze_query_data(payload, [], "verify-signal-union")
query = app.get_session_query("verify-signal-union")

results = []
for work_order in ("5001505712", "5001331067", "5001340533"):
    figure, status = app.build_raw_overlay_comparison(query, work_order, compact=False)
    history_traces = [
        trace for trace in figure.data
        if (trace.meta or {}).get("comparison_role") == "history"
    ]
    current_traces = [
        trace for trace in figure.data
        if (trace.meta or {}).get("comparison_role") == "current"
    ]
    results.append({
        "work_order": work_order,
        "subplot_count": len(figure.layout.annotations),
        "current_trace_count": len(current_traces),
        "history_trace_count": len(history_traces),
        "titles": [annotation.text for annotation in figure.layout.annotations],
        "alignment_shifts_seconds": figure.layout.meta.get("alignment_shifts_seconds"),
        "status": status,
    })

print(json.dumps(results, ensure_ascii=False, indent=2))
