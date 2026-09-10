import json
import traceback

import pandas as pd

import app_visualize as app
from iceberg_raw_reader import read_iceberg_raw_window


WORK_ORDER = "5001406566"


def clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)


vectors, metadata, node_index = app.load_library()
segments = app.load_segments()
work_order_values = metadata["work_order_no"].astype(str).str.replace(r"\.0$", "", regex=True)
matches = metadata.loc[work_order_values == WORK_ORDER]
print("METADATA_COLUMNS=" + json.dumps(list(metadata.columns), ensure_ascii=False))
print("MATCH_COUNT=" + str(len(matches)))

results = []
for index, row in matches.iterrows():
    case_id = clean(row.get("case_id"))
    train = clean(row.get("train"))
    car_no = app.normalize_car_no(row.get("car_no"))
    fault_utc = pd.Timestamp(row.get("fault_time_utc"))
    partition_cst = pd.Timestamp(row.get("fault_time_raw"))
    segment = segments.get(case_id) or []
    counts = app.segment_counts(segment, "dim", car_no=car_no, include_vcb_display=True)
    dims = list(counts)
    plot_codes = app.raw_codes_for_dims(dims, car_no)
    all_codes = app.gateway_metric_codes_for_car(car_no)
    item = {
        "metadata_index": int(index),
        "case_id": case_id,
        "train": train,
        "car_no": car_no,
        "fault_time_raw_cst": str(partition_cst),
        "fault_time_utc": str(fault_utc),
        "segment_record_count": len(segment),
        "dimensions": dims,
        "plot_codes": plot_codes,
        "all_gateway_code_count": len(all_codes),
        "queries": {},
    }
    for label, before, after, codes in (
        ("plot_60s_30s", 1.0, 0.5, plot_codes),
        ("all_codes_60s_30s", 1.0, 0.5, all_codes),
        ("all_codes_10m_10m", 10.0, 10.0, all_codes),
    ):
        try:
            frame = read_iceberg_raw_window(
                train=train,
                fault_time_utc=fault_utc,
                partition_time_cst=partition_cst,
                codes=codes,
                event_code="1757",
                before_minutes=before,
                after_minutes=after,
            )
            item["queries"][label] = {
                "row_count": int(len(frame)),
                "codes_found": sorted(frame["code"].astype(str).unique().tolist()) if not frame.empty else [],
                "time_min": str(frame["_time"].min()) if not frame.empty else None,
                "time_max": str(frame["_time"].max()) if not frame.empty else None,
                "rows_per_code": (
                    {str(key): int(value) for key, value in frame.groupby(frame["code"].astype(str)).size().items()}
                    if not frame.empty else {}
                ),
            }
        except Exception as exc:
            item["queries"][label] = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
    results.append(item)

print("RESULTS=" + json.dumps(results, ensure_ascii=False, indent=2))
