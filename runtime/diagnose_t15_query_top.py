import json
import traceback

import app_visualize as app


payload = app.download_query_data(
    None,
    {"search": "t15-check", "revision": "t15-check"},
    "T15",
    "02",
    "2026-05-06 08:46:54",
    "1757",
    60,
    30,
    10,
    0.0,
    ["cst"],
)
print("DOWNLOAD=" + json.dumps(payload, ensure_ascii=False, default=str))
if not payload.get("ok"):
    raise SystemExit(1)

analyzed = app.analyze_query_data(payload, [], "diagnose-t15-query")
query = app.get_session_query("diagnose-t15-query")
top = []
for rank, metadata_index in enumerate(query.get("top_idx") or [], start=1):
    row = query["metadata"].iloc[int(metadata_index)]
    top.append({
        "rank": rank,
        "metadata_index": int(metadata_index),
        "work_order": str(row.get("work_order_no")),
        "case_id": str(row.get("case_id")),
        "similarity": float(query["sims"][int(metadata_index)]),
        "train": str(row.get("train")),
        "car_no": str(row.get("car_no")),
        "fault_time_raw": str(row.get("fault_time_raw")),
        "fault_time_utc": str(row.get("fault_time_utc")),
        "segment_start": str(row.get("segment_start")),
        "segment_end": str(row.get("segment_end")),
        "segment_center": str(row.get("segment_center")),
        "center_minus_fault_seconds": str(row.get("center_minus_fault_seconds")),
    })
print("TOP=" + json.dumps(top, ensure_ascii=False, indent=2))

try:
    rank, metadata_index, resolved = app._comparison_work_order_row(query, "5001406566")
    selected = query["metadata"].iloc[int(metadata_index)]
    print("SELECTED=" + json.dumps({
        "rank": rank,
        "metadata_index": int(metadata_index),
        "resolved_work_order": resolved,
        "case_id": str(selected.get("case_id")),
        "fault_time_raw": str(selected.get("fault_time_raw")),
        "fault_time_utc": str(selected.get("fault_time_utc")),
    }, ensure_ascii=False, indent=2))
    figure, status = app.build_raw_overlay_comparison(query, "5001406566", compact=False)
    print("FIGURE=" + json.dumps({
        "trace_count": len(figure.data),
        "status": status,
    }, ensure_ascii=False))
except Exception as exc:
    print("FIGURE_ERROR=" + json.dumps({
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }, ensure_ascii=False))

compact_figure, compact_message = app.update_compact_raw_comparison(
    "data",
    "5001406566",
    query.get("revision"),
    "diagnose-t15-query",
)
detail_figure, detail_message = app.update_detail_raw_comparison(
    True,
    "data",
    "5001406566",
    query.get("revision"),
    "diagnose-t15-query",
)
print("USER_MESSAGES=" + json.dumps({
    "compact": compact_message,
    "detail": detail_message,
}, ensure_ascii=False))
