import json
import sqlite3
import sys

sys.path.insert(0, "/app")

from app_visualize import _DETAIL_METADATA


connection = sqlite3.connect("/runtime/work_order_sync/state.sqlite3")
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT work_order_no,status,last_error,attempt_count,processed_at "
    "FROM work_order_state "
    "WHERE work_order_no IN ('5001756769','5001843907','5001860453') "
    "ORDER BY work_order_no"
).fetchall()
print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))

matched = _DETAIL_METADATA[
    _DETAIL_METADATA["work_order_no"].astype(str).eq("5001860453")
]
print("qdrant_case_rows=", len(matched))
if len(matched):
    print(matched[["work_order_no", "fault_time_raw"]].to_string(index=False))
