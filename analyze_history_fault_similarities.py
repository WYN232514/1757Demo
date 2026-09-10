# -*- coding: utf-8 -*-
"""Batch-score station-filtered 1757 faults against the work-order library."""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

import app_visualize as app
from gateway_download import download_fault_window


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "历史故障列表 (1).xls"
DEFAULT_OUTPUT_DIR = ROOT / "历史故障相似度分析"
DEPOT = "何東樓車廠"
ANALYSIS_WINDOWS = (
    (60, 30),
    (120, 60),
    (180, 180),
    (300, 300),
)


def load_filtered_faults(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="历史故障", header=2)
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
    filtered["事件时间"] = pd.to_datetime(filtered["事件时间"], errors="coerce")
    filtered = filtered.dropna(subset=["列号", "车厢", "事件时间"]).copy()
    filtered["train"] = filtered["列号"].map(app.normalize_train)
    filtered["car_no"] = filtered["车厢"].map(app.normalize_car_no)
    filtered["fault_time"] = filtered["事件时间"].dt.strftime("%Y-%m-%d %H:%M:%S")
    filtered["fault_id"] = range(1, len(filtered) + 1)
    return filtered


def download_one(
    row: dict[str, Any],
    metric_codes_by_car: dict[str, list[str]],
    output_root: Path,
) -> dict[str, Any]:
    car_no = str(row["car_no"])
    try:
        result = download_fault_window(
            train=str(row["train"]),
            fault_time=str(row["fault_time"]),
            time_is_cst=True,
            before_min=5,
            after_min=5,
            fault_code=str(row.get("事件代码", "1757")),
            metric_codes=metric_codes_by_car[car_no],
            output_root=output_root / f"car_{car_no}",
            workers=1,
            reuse=True,
        )
        return {
            "fault_id": int(row["fault_id"]),
            "ok": True,
            "data_dir": str(result.data_dir),
            "row_count": int(result.row_count),
            "reused": bool(result.reused),
        }
    except Exception as exc:
        return {
            "fault_id": int(row["fault_id"]),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def score_one(row: pd.Series, download: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = {
        "fault_id": int(row["fault_id"]),
        "train": str(row["train"]),
        "car_no": str(row["car_no"]),
        "fault_time": str(row["fault_time"]),
        "current_station": str(row["当前站"]),
        "next_station": str(row["下一站"]),
        "event_code": str(row.get("事件代码", "")),
        "download_rows": int(download.get("row_count", 0)),
        "download_reused": bool(download.get("reused", False)),
    }
    if not download.get("ok"):
        return {
            **base,
            "status": "download_failed",
            "error": str(download.get("error", "")),
        }, []

    query: dict[str, Any] | None = None
    messages: list[str] = []
    used_before = used_after = 0
    for before_seconds, after_seconds in ANALYSIS_WINDOWS:
        used_before, used_after = before_seconds, after_seconds
        query = app.compute_query(
            train=str(row["train"]),
            car_no=str(row["car_no"]),
            fault_time=str(row["fault_time"]),
            time_is_cst=True,
            top_k=10_000,
            similarity_threshold=0.0,
            fault_code=str(row.get("事件代码", "1757")),
            data_dir=str(download["data_dir"]),
            download_info={
                "before_min": before_seconds / 60,
                "after_min": after_seconds / 60,
            },
        )
        if query.get("ok"):
            break
        messages.append(str(query.get("message", "")))

    if not query or not query.get("ok"):
        return {
            **base,
            "status": "no_usable_segment",
            "window_before_seconds": used_before,
            "window_after_seconds": used_after,
            "error": " | ".join(dict.fromkeys(messages)),
        }, []

    metadata = query["metadata"]
    best_by_work_order: dict[str, tuple[int, float]] = {}
    for idx, similarity in enumerate(query["sims"]):
        work_order = str(metadata.iloc[idx].get("work_order_no", ""))
        score = float(similarity)
        previous = best_by_work_order.get(work_order)
        if previous is None or score > previous[1]:
            best_by_work_order[work_order] = (idx, score)

    ordered = sorted(best_by_work_order.values(), key=lambda item: item[1], reverse=True)
    scores = [score for _, score in ordered]
    detail_rows: list[dict[str, Any]] = []
    for rank, (idx, score) in enumerate(ordered, start=1):
        meta = metadata.iloc[idx]
        query_time = pd.Timestamp(row["fault_time"])
        work_order_time = pd.to_datetime(meta.get("fault_time_raw", ""), errors="coerce")
        time_difference_seconds = (
            abs(float((work_order_time - query_time).total_seconds()))
            if not pd.isna(work_order_time)
            else None
        )
        same_incident = bool(
            str(meta.get("train", "")) == str(row["train"])
            and app.normalize_car_no(meta.get("car_no", "")) == str(row["car_no"])
            and time_difference_seconds is not None
            and time_difference_seconds <= 300
        )
        detail_rows.append(
            {
                "fault_id": int(row["fault_id"]),
                "train": str(row["train"]),
                "car_no": str(row["car_no"]),
                "fault_time": str(row["fault_time"]),
                "current_station": str(row["当前站"]),
                "next_station": str(row["下一站"]),
                "rank": rank,
                "work_order_no": str(meta.get("work_order_no", "")),
                "case_id": str(meta.get("case_id", "")),
                "work_order_train": str(meta.get("train", "")),
                "work_order_car_no": str(meta.get("car_no", "")),
                "work_order_fault_time": str(meta.get("fault_time_raw", "")),
                "time_difference_seconds": (
                    round(time_difference_seconds, 3)
                    if time_difference_seconds is not None
                    else None
                ),
                "same_incident_within_5min": same_incident,
                "similarity": round(score, 6),
            }
        )

    top3 = scores[:3]
    top5 = scores[:5]
    novel_rows = [item for item in detail_rows if not item["same_incident_within_5min"]]
    novel_scores = [float(item["similarity"]) for item in novel_rows]
    return {
        **base,
        "status": "ok",
        "window_before_seconds": used_before,
        "window_after_seconds": used_after,
        "query_rf": str(query.get("query_rf_label", "")),
        "node_count": int(query.get("node_count", 0)),
        "unknown_node_count": int(query.get("unknown_node_count", 0)),
        "max_similarity": round(scores[0], 6) if scores else 0.0,
        "top3_mean_similarity": round(sum(top3) / len(top3), 6) if top3 else 0.0,
        "top5_mean_similarity": round(sum(top5) / len(top5), 6) if top5 else 0.0,
        "work_orders_ge_0_8": sum(score >= 0.8 for score in scores),
        "work_orders_ge_0_7": sum(score >= 0.7 for score in scores),
        "work_orders_ge_0_6": sum(score >= 0.6 for score in scores),
        "work_orders_ge_0_5": sum(score >= 0.5 for score in scores),
        "top_work_order": detail_rows[0]["work_order_no"] if detail_rows else "",
        "top_case_id": detail_rows[0]["case_id"] if detail_rows else "",
        "novel_max_similarity": max(novel_scores) if novel_scores else 0.0,
        "novel_work_orders_ge_0_8": sum(score >= 0.8 for score in novel_scores),
        "novel_work_orders_ge_0_7": sum(score >= 0.7 for score in novel_scores),
        "novel_work_orders_ge_0_6": sum(score >= 0.6 for score in novel_scores),
        "novel_work_orders_ge_0_5": sum(score >= 0.5 for score in novel_scores),
        "top_novel_work_order": novel_rows[0]["work_order_no"] if novel_rows else "",
    }, detail_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--download-workers", type=int, default=6)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    download_root = ROOT / "gateway_downloads" / "history_list_1"

    faults = load_filtered_faults(args.input.resolve())
    faults.to_csv(output_dir / "01_站点筛选后故障.csv", index=False, encoding="utf-8-sig")
    print(f"站点筛选后：{len(faults)} 条", flush=True)

    app.load_library()
    app.load_segments()
    metric_codes_by_car = {
        car_no: app.gateway_metric_codes_for_car(car_no)
        for car_no in sorted(faults["car_no"].unique())
    }

    records = faults.to_dict(orient="records")
    downloads: dict[int, dict[str, Any]] = {}
    print(f"开始并行下载：{len(records)} 条，并发 {args.download_workers}", flush=True)
    progress_lock = threading.Lock()
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.download_workers)) as executor:
        futures = {
            executor.submit(download_one, row, metric_codes_by_car, download_root): row
            for row in records
        }
        for future in as_completed(futures):
            result = future.result()
            downloads[int(result["fault_id"])] = result
            with progress_lock:
                completed += 1
                print(
                    f"下载进度 {completed}/{len(records)}："
                    f"{'成功' if result.get('ok') else '失败'}",
                    flush=True,
                )

    summary_rows: list[dict[str, Any]] = []
    match_rows: list[dict[str, Any]] = []
    for position, (_, row) in enumerate(faults.iterrows(), start=1):
        summary, matches = score_one(row, downloads[int(row["fault_id"])])
        summary_rows.append(summary)
        match_rows.extend(matches)
        print(f"相似度进度 {position}/{len(faults)}：{summary['status']}", flush=True)

    summary_df = pd.DataFrame(summary_rows)
    matches_df = pd.DataFrame(match_rows)
    summary_df.to_csv(output_dir / "02_逐故障相似度汇总.csv", index=False, encoding="utf-8-sig")
    matches_df.to_csv(output_dir / "03_逐故障工单匹配明细.csv", index=False, encoding="utf-8-sig")

    ok = summary_df[summary_df["status"].eq("ok")].copy()
    ranking = ok.sort_values(
        [
            "work_orders_ge_0_7",
            "work_orders_ge_0_6",
            "max_similarity",
            "top3_mean_similarity",
        ],
        ascending=False,
    )
    ranking.to_csv(output_dir / "04_高相似且高相似工单较多案例.csv", index=False, encoding="utf-8-sig")
    novel_ranking = ok.sort_values(
        [
            "novel_work_orders_ge_0_7",
            "novel_work_orders_ge_0_6",
            "novel_max_similarity",
        ],
        ascending=False,
    )
    novel_ranking.to_csv(
        output_dir / "06_排除同一次故障后的高相似案例.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result = {
        "input": str(args.input.resolve()),
        "source_rows": 1749,
        "station_filtered_rows": len(faults),
        "status_counts": summary_df["status"].value_counts(dropna=False).to_dict(),
        "cases_with_work_order_ge_0_8": int((ok["work_orders_ge_0_8"] > 0).sum()) if not ok.empty else 0,
        "cases_with_work_order_ge_0_7": int((ok["work_orders_ge_0_7"] > 0).sum()) if not ok.empty else 0,
        "cases_with_work_order_ge_0_6": int((ok["work_orders_ge_0_6"] > 0).sum()) if not ok.empty else 0,
        "output_dir": str(output_dir),
    }
    (output_dir / "05_分析摘要.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
