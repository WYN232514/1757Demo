# -*- coding: utf-8 -*-
"""Estimate the smallest live-download window from 历史故障列表.xls.

This is a read-only analysis of the current historical raw CSV files.  It does
not update the work-order library or download any gateway data.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd
import joblib

from gateway_download import load_metrics
from vcb_case_library_direct import (
    add_aux_features_to_segment,
    build_local_events_and_features,
    get_segment_time_range,
    get_ramping_finish_dims_for_car,
    get_vcb_target_dims_for_car,
    normalize_car_no,
    normalize_code,
    normalize_train,
    parse_time_to_utc,
    segment_has_target,
    select_main_segment,
    split_events_to_segments,
)


ROOT = Path(__file__).resolve().parent
FAULT_XLS = ROOT / "历史故障列表.xls"
DATA_DIR = ROOT / "01_数据整理"
DETAIL_CSV = ROOT / "历史故障下载窗口统计明细.csv"
SUMMARY_JSON = ROOT / "历史故障下载窗口统计摘要.json"
DEPOT = "何東樓車廠"
READ_BEFORE_MIN = 30
READ_AFTER_MIN = 5
CHUNKSIZE = 500_000
TEST_WINDOWS = {
    "symmetric_1m": (1, 1),
    "symmetric_2m": (2, 2),
    "symmetric_3m": (3, 3),
    "symmetric_4m": (4, 4),
    "symmetric_5m": (5, 5),
    "before5_after3": (5, 3),
}


def gateway_metric_codes_for_car(car_no: str) -> list[str]:
    node_index = joblib.load(ROOT / "case_library" / "node_index.joblib")
    node_names = tuple(str(node) for node in node_index)
    required_codes = {
        normalize_code(dim)
        for dim in (
            get_vcb_target_dims_for_car(car_no)
            | get_ramping_finish_dims_for_car(car_no)
        )
    }
    selected = []
    for metric in load_metrics(str(ROOT / "gateway_metrics.json"), str(DATA_DIR)):
        code = str(metric["code"])
        if code in required_codes or any(node.startswith(f"dir_{code}_") for node in node_names):
            selected.append(code)
    return selected


def load_faults() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_excel(FAULT_XLS, sheet_name="历史故障", header=2)
    current_station = raw["当前站"].astype("string").str.strip()
    next_station = raw["下一站"].astype("string").str.strip()
    keep = (
        current_station.notna()
        & next_station.notna()
        & current_station.ne("")
        & next_station.ne("")
        & current_station.ne(DEPOT)
        & next_station.ne(DEPOT)
    )
    filtered = raw.loc[keep].copy().reset_index(drop=True)
    filtered["fault_id"] = filtered.index
    filtered["train_norm"] = filtered["列号"].map(normalize_train)
    filtered["car_norm"] = filtered["车厢"].map(normalize_car_no)
    filtered["fault_time_cst"] = pd.to_datetime(filtered["事件时间"], errors="coerce")
    filtered["fault_time_utc"] = filtered["fault_time_cst"].map(
        lambda value: parse_time_to_utc(value, time_is_cst=True)
    )
    filtered["local_day"] = filtered["fault_time_cst"].dt.strftime("%Y-%m-%d")

    local_days = {path.stem[:10] for path in DATA_DIR.glob("*.csv")}
    available = filtered[filtered["local_day"].isin(local_days)].copy()
    return filtered, available


def relevant_faults_for_file(faults: pd.DataFrame, day: str) -> pd.DataFrame:
    day_start = pd.Timestamp(day)
    day_end = day_start + pd.Timedelta(days=1)
    local_start = faults["fault_time_cst"] - pd.Timedelta(minutes=READ_BEFORE_MIN)
    local_end = faults["fault_time_cst"] + pd.Timedelta(minutes=READ_AFTER_MIN)
    return faults[(local_start < day_end) & (local_end >= day_start)]


def scan_raw_windows(faults: pd.DataFrame) -> dict[int, list[pd.DataFrame]]:
    parts: dict[int, list[pd.DataFrame]] = defaultdict(list)
    wanted_codes = {
        car: set(gateway_metric_codes_for_car(car)) for car in ("02", "05", "09")
    }
    usecols = ["_time", "_value", "code", "equipInstance", "ts_date", "eventCode", "status"]

    for path in sorted(DATA_DIR.glob("*.csv")):
        day_faults = relevant_faults_for_file(faults, path.stem[:10])
        if day_faults.empty:
            continue
        print(f"扫描 {path.name}: 候选故障 {len(day_faults)} 条", flush=True)
        trains = set(day_faults["train_norm"])
        for chunk in pd.read_csv(path, usecols=lambda c: c in usecols, chunksize=CHUNKSIZE):
            chunk = chunk[chunk["equipInstance"].astype(str).isin(trains)].copy()
            if chunk.empty:
                continue
            chunk["_time"] = pd.to_datetime(chunk["_time"], errors="coerce")
            chunk["_value"] = pd.to_numeric(chunk["_value"], errors="coerce")
            chunk["code"] = chunk["code"].astype(str).str.strip()
            chunk["equipInstance"] = chunk["equipInstance"].astype(str)
            chunk = chunk.dropna(subset=["_time", "_value", "code"])
            if chunk.empty:
                continue

            for row in day_faults.itertuples(index=False):
                start = row.fault_time_utc - pd.Timedelta(minutes=READ_BEFORE_MIN)
                end = row.fault_time_utc + pd.Timedelta(minutes=READ_AFTER_MIN)
                mask = (
                    chunk["equipInstance"].eq(row.train_norm)
                    & chunk["_time"].between(start, end, inclusive="both")
                    & chunk["code"].isin(wanted_codes[row.car_norm])
                )
                if mask.any():
                    parts[int(row.fault_id)].append(chunk.loc[mask].copy())
    return parts


def select_for_window(df_raw: pd.DataFrame, row, before_min: int, after_min: int):
    start = row.fault_time_utc - pd.Timedelta(minutes=before_min)
    end = row.fault_time_utc + pd.Timedelta(minutes=after_min)
    window_raw = df_raw[df_raw["_time"].between(start, end, inclusive="both")]
    events, feature_tables = build_local_events_and_features(
        df_raw=window_raw,
        train=row.train_norm,
        fault_time_utc=row.fault_time_utc,
        analyze_before_min=before_min,
        analyze_after_min=after_min,
    )
    segments = split_events_to_segments(events)
    targets = [seg for seg in segments if segment_has_target(seg, car_no=row.car_norm)]
    targets = [
        add_aux_features_to_segment(seg, feature_tables, row.train_norm, car_no=row.car_norm)
        for seg in targets
    ]
    main_segment, candidates = select_main_segment(
        targets, row.fault_time_utc, car_no=row.car_norm
    )
    status = "ok" if main_segment is not None else (
        "no_target_segment" if not targets else "no_valid_main_segment"
    )
    return status, main_segment, candidates, len(events), len(targets), len(window_raw)


def segment_signature(main_segment, candidates: pd.DataFrame, car_no: str):
    if main_segment is None or candidates.empty:
        return None
    selected = candidates[candidates["selected_as_main"]]
    if selected.empty:
        return None
    transition = str(selected.iloc[0]["selection_transition"])
    expected = (1.0, 0.0) if transition == "vcb_1_to_0" else (0.0, 1.0)
    target_dims = get_vcb_target_dims_for_car(car_no)
    times = []
    for event in main_segment:
        if event[1] not in target_dims:
            continue
        try:
            if float(event[3]) == expected[0] and float(event[5]) == expected[1]:
                times.append(pd.Timestamp(event[4]))
        except (TypeError, ValueError):
            continue
    return transition, min(times) if times else None


def signatures_match(reference, candidate) -> bool:
    if reference is None or candidate is None or reference[0] != candidate[0]:
        return False
    if reference[1] is None or candidate[1] is None:
        return reference[1] is candidate[1]
    return abs((reference[1] - candidate[1]).total_seconds()) <= 1.0


def analyze_one(row, raw_parts: list[pd.DataFrame]) -> dict:
    base = {
        "fault_id": int(row.fault_id),
        "train": row.train_norm,
        "car_no": row.car_norm,
        "fault_time_cst": row.fault_time_cst,
        "current_station": row.当前站,
        "next_station": row.下一站,
    }
    if not raw_parts:
        return {**base, "status": "no_raw_data"}

    df_raw = pd.concat(raw_parts, ignore_index=True)
    df_raw = df_raw.drop_duplicates(subset=["_time", "code", "equipInstance"], keep="last")
    status, main_segment, candidates, event_count, target_count, raw_count = select_for_window(
        df_raw, row, READ_BEFORE_MIN, READ_AFTER_MIN
    )
    if main_segment is None:
        return {
            **base,
            "status": status,
            "raw_rows": raw_count,
            "event_count": event_count,
            "target_segment_count": target_count,
        }

    start, end = get_segment_time_range(main_segment)
    selected = candidates[candidates["selected_as_main"]].iloc[0]
    start_offset = float((start - row.fault_time_utc).total_seconds())
    end_offset = float((end - row.fault_time_utc).total_seconds())
    result = {
        **base,
        "status": "ok",
        "raw_rows": raw_count,
        "event_count": event_count,
        "target_segment_count": target_count,
        "main_segment_event_count": len(main_segment),
        "segment_start_utc": start,
        "segment_end_utc": end,
        "segment_start_offset_seconds": start_offset,
        "segment_end_offset_seconds": end_offset,
        "required_before_seconds": max(0.0, -start_offset),
        "required_after_seconds": max(0.0, end_offset),
        "segment_center_offset_seconds": float(selected["center_minus_fault_seconds"]),
        "selection_transition": selected["selection_transition"],
        "selection_rule": selected["selection_rule"],
    }
    reference_signature = segment_signature(main_segment, candidates, row.car_norm)
    for name, (before_min, after_min) in TEST_WINDOWS.items():
        test_status, test_main, test_candidates, *_ = select_for_window(
            df_raw, row, before_min, after_min
        )
        test_signature = segment_signature(test_main, test_candidates, row.car_norm)
        result[f"{name}_status"] = test_status
        result[f"{name}_same_segment"] = signatures_match(reference_signature, test_signature)
        result[f"{name}_transition"] = test_signature[0] if test_signature else ""
    return result


def percentile_seconds(series: pd.Series, percentile: float) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return None if clean.empty else round(float(clean.quantile(percentile)), 3)


def main() -> None:
    filtered, available = load_faults()
    print(
        f"站点过滤后 {len(filtered)} 条；本地日期可验证 {len(available)} 条，"
        f"覆盖 {available['local_day'].nunique()} 天",
        flush=True,
    )
    parts = scan_raw_windows(available)
    rows = [analyze_one(row, parts.get(int(row.fault_id), [])) for row in available.itertuples(index=False)]
    detail = pd.DataFrame(rows)
    detail.to_csv(DETAIL_CSV, index=False, encoding="utf-8-sig")

    ok = detail[detail["status"].eq("ok")].copy()
    before = ok.get("required_before_seconds", pd.Series(dtype=float))
    after = ok.get("required_after_seconds", pd.Series(dtype=float))
    summary = {
        "source_total_rows": 1081,
        "station_filtered_rows": len(filtered),
        "local_date_candidate_rows": len(available),
        "local_date_count": int(available["local_day"].nunique()),
        "status_counts": detail["status"].value_counts(dropna=False).to_dict(),
        "valid_segment_rows": len(ok),
        "required_before_seconds": {
            "p50": percentile_seconds(before, 0.50),
            "p90": percentile_seconds(before, 0.90),
            "p95": percentile_seconds(before, 0.95),
            "p99": percentile_seconds(before, 0.99),
            "max": percentile_seconds(before, 1.00),
        },
        "required_after_seconds": {
            "p50": percentile_seconds(after, 0.50),
            "p90": percentile_seconds(after, 0.90),
            "p95": percentile_seconds(after, 0.95),
            "p99": percentile_seconds(after, 0.99),
            "max": percentile_seconds(after, 1.00),
        },
        "covered_by_symmetric_window": {
            str(minutes): int(
                ((before <= minutes * 60) & (after <= minutes * 60)).sum()
            )
            for minutes in (1, 2, 3, 4, 5)
        },
        "resegmentation_results": {
            name: {
                "valid_segment": int(ok[f"{name}_status"].eq("ok").sum()),
                "same_as_30_before_5_after": int(ok[f"{name}_same_segment"].eq(True).sum()),
            }
            for name in TEST_WINDOWS
        },
        "note": "Statistics use only station-filtered faults whose local date CSV exists; no gateway data was downloaded.",
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"明细: {DETAIL_CSV}", flush=True)
    print(f"摘要: {SUMMARY_JSON}", flush=True)


if __name__ == "__main__":
    main()
