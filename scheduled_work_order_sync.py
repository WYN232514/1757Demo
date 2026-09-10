# -*- coding: utf-8 -*-
"""Periodically synchronize completed VCB work orders into the case library.

The workflow API is the source of work-order rows.  Raw telemetry is read
directly from Iceberg, while the existing extractor and vectorization logic are
reused unchanged.  A SQLite state file makes overlapping half-month queries
idempotent and keeps failed rows available for a later retry.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from gateway_download import load_metrics
from iceberg_raw_reader import read_iceberg_raw_window
from vcb_case_library_direct import (
    DEFAULT_QDRANT_COLLECTION,
    DEFAULT_QDRANT_URL,
    READ_AFTER_MIN,
    READ_BEFORE_MIN,
    backup_case_library,
    extract_case_inputs_from_rows,
    export_complete_qdrant_library,
    load_case_input_for_build,
    normalize_car_no,
    normalize_train,
    process_work_df,
    save_case_library_files,
    segment_to_node_counts,
    sync_library_to_qdrant,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = ROOT / "runtime" / "work_order_sync"
DEFAULT_LIBRARY_DIR = ROOT / "case_library"
DEFAULT_FULL_CASE_CSV = ROOT / "case_input_all_times.csv"
DEFAULT_METRICS_JSON = ROOT / "gateway_metrics.json"
DEFAULT_REFERENCE_JSON = (
    ROOT / "fault_extractor" / "fault_extractor" / "output"
    / "reference_data_with_cause_inferenced.json"
)
DEFAULT_LLM_CONFIG = (
    ROOT / "fault_extractor" / "fault_extractor" / "llm_api_config.json"
)

SOURCE_TO_XLSX = {
    "description": "Description",
    "long_description": "Long Description",
    "remark_description": "Remark Description",
    "failure_long_description": "Failure Long Description",
    "work_order_no": "Work Order NO.",
    "work_order_parent": "Work Order Parent",
    "location_location": "Location",
    "location": "Location",
    "location_description": "LOCATION.DESCRIPTION",
    "asset_num": "Asset",
    "asset": "Asset",
    "asset_description": "ASSET.DESCRIPTION",
    "status": "Status",
    "change_date": "Change Date",
    "work_type": "Work Type",
    "work_group": "Work Group",
    "job_plan": "Job Plan",
    "failure_code": "Failure Code",
    "problem_code": "Problem Code",
    "problem_desc": "Problem Desc",
    "cause_code": "Cause Code",
    "cause_desc": "Cause Desc",
    "remedy_code": "Remedy Code",
    "remedy_desc": "Remedy Desc",
    "failure_component": "Failure Component",
    "component_desc": "Component Desc",
    "replaced_component": "Replaced Component",
    "rep_comp_desc": "Rep.Comp Desc",
    "report_date": "Report Date",
    "act_finish_date": "Act Finish Date",
    "last_comp_date": "Last Comp. Date",
    "asset_classification": "Asset Classification",
    "asset_classification_description": "CLASSIFICATION.DESCRIPTION",
    "install_date": "Install Date",
    "equip_instance": "Trainset",
    "equip_unit": "CarNO.",
    "equip_model_name": "Equip Model",
    "update_time": "Update Time",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "nan", "nat", "none", "null"}:
        return ""
    return html.unescape(text)


def normalize_work_order_no(value: Any) -> str:
    text = clean_text(value).removesuffix(".0")
    match = re.search(r"\d{10}", text)
    return match.group(0) if match else text


def normalize_source_row(row: Mapping[str, Any]) -> dict[str, str]:
    normalized = {
        str(key).strip().lower().replace(" ", "_"): clean_text(value)
        for key, value in row.items()
    }
    normalized["work_order_no"] = normalize_work_order_no(
        normalized.get("work_order_no", "")
    )
    normalized["equip_instance"] = normalize_train(
        normalized.get("equip_instance", "")
    )
    normalized["equip_unit"] = normalize_car_no(normalized.get("equip_unit", ""))
    return normalized


def split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if not (text.startswith("|") and text.endswith("|")):
        raise ValueError(f"不是 Markdown 表格行：{line[:120]}")
    placeholder = "\x00PIPE\x00"
    text = text[1:-1].replace(r"\|", placeholder)
    return [html.unescape(cell.replace(placeholder, "|").strip()) for cell in text.split("|")]


def parse_markdown_table(text: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        raise ValueError("工作流 result 中没有可识别的 Markdown 表格")
    headers = [cell.strip() for cell in split_markdown_row(lines[0])]
    if not headers or any(not header for header in headers):
        raise ValueError(f"工作流表头无效：{headers}")

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cells = split_markdown_row(line)
        if len(cells) == len(headers) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError(
                f"Markdown 列数不一致：表头 {len(headers)} 列，数据 {len(cells)} 列。"
                "请让工作流改为输出 JSON 数组，避免描述中的竖线破坏表格。"
            )
        rows.append(normalize_source_row(dict(zip(headers, cells))))
    return [row for row in rows if row.get("work_order_no")]


def completed_half_month(now: pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    now = pd.Timestamp.now(tz="Asia/Shanghai") if now is None else pd.Timestamp(now)
    if now.tzinfo is not None:
        now = now.tz_convert("Asia/Shanghai").tz_localize(None)
    month_start = now.normalize().replace(day=1)
    month_mid = month_start.replace(day=16)
    if now >= month_mid:
        return month_start, month_mid
    previous_month_mid = (month_start - pd.Timedelta(days=1)).replace(day=16).normalize()
    return previous_month_mid, month_start


def resolve_interval(
    start: str | None,
    end: str | None,
    overlap_days: float,
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    if bool(start) != bool(end):
        raise ValueError("--start 和 --end 必须同时提供")
    base_start, base_end = (
        (pd.Timestamp(start), pd.Timestamp(end)) if start and end else completed_half_month()
    )
    if base_end <= base_start:
        raise ValueError("查询结束时间必须晚于开始时间")
    query_start = base_start - pd.Timedelta(days=max(0.0, float(overlap_days)))
    return base_start, base_end, query_start, base_end


def build_sql(query_start: pd.Timestamp, query_end: pd.Timestamp) -> str:
    start_text = query_start.strftime("%Y-%m-%d %H:%M:%S")
    end_text = query_end.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "SELECT * FROM WO_DETAIL "
        "WHERE status IN ('CLOSE', 'COMP') "
        "AND description LIKE '%vcb trip%' "
        f"AND report_date >= '{start_text}' "
        f"AND report_date < '{end_text}' "
        "ORDER BY report_date"
    )


def _parse_workflow_result(result: Any) -> list[dict[str, str]]:
    """Accept the response formats emitted by different workflow versions."""
    if isinstance(result, list):
        return [normalize_source_row(row) for row in result if isinstance(row, Mapping)]

    if isinstance(result, Mapping):
        if result.get("work_order_no") or result.get("Work Order NO."):
            return [normalize_source_row(result)]
        for key in ("rows", "data", "result", "records", "items"):
            if key in result:
                return _parse_workflow_result(result.get(key))
        raise ValueError(
            "工作流 outputs.result 是对象，但没有 rows/data/result/records/items 字段"
        )

    if not isinstance(result, str):
        raise ValueError("工作流 outputs.result 不是可识别的数组、对象或文本")

    stripped = result.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json|markdown|md)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if not stripped:
        return []

    try:
        return _parse_workflow_result(json.loads(stripped))
    except json.JSONDecodeError:
        pass

    if "|" in stripped:
        return parse_markdown_table(stripped)

    if re.search(
        r"(?:no\s+(?:matching\s+)?(?:data|records?|rows?)|"
        r"0\s+(?:records?|rows?)|未查询到|没有匹配|无匹配|暂无数据)",
        stripped,
        flags=re.I,
    ):
        return []
    raise ValueError(f"工作流 result 中没有可识别的数据：{stripped[:500]}")


def _call_workflow_once(sql: str, timeout: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    url = os.environ.get("WORK_ORDER_WORKFLOW_URL", "").strip().strip('"').strip("'")
    api_key = os.environ.get("WORK_ORDER_WORKFLOW_API_KEY", "").strip().strip('"').strip("'")
    user = os.environ.get("WORK_ORDER_WORKFLOW_USER", "lq-123").strip().strip('"').strip("'")
    if not url or not api_key:
        raise RuntimeError("缺少 WORK_ORDER_WORKFLOW_URL 或 WORK_ORDER_WORKFLOW_API_KEY")
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"inputs": {"sql": sql}, "response_mode": "blocking", "user": user},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"工单工作流 HTTP {response.status_code}: {response.text[:1000]}")
    payload = response.json()
    run_data = payload.get("data") or {}
    if run_data.get("status") != "succeeded":
        raise RuntimeError(f"工单工作流执行失败：{run_data.get('error') or run_data.get('status')}")
    outputs = run_data.get("outputs") or {}
    result = outputs.get("result", outputs.get("data", outputs.get("rows")))
    rows = _parse_workflow_result(result)
    return rows, payload


def call_workflow(
    sql: str,
    timeout: int,
    attempts: int = 3,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Call the work-order workflow with bounded retries for transient output failures."""
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _call_workflow_once(sql, timeout)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(4, 2 ** (attempt - 1))
            print(f"工作流第 {attempt} 次调用失败，{delay} 秒后重试：{exc}")
            time.sleep(delay)
    raise RuntimeError(f"工作流连续 {attempts} 次调用失败：{last_error}") from last_error


def source_hash(row: Mapping[str, Any]) -> str:
    body = json.dumps(dict(sorted(row.items())), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class SyncState:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                base_start TEXT NOT NULL,
                base_end TEXT NOT NULL,
                query_start TEXT NOT NULL,
                query_end TEXT NOT NULL,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS work_order_state (
                work_order_no TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                source_report_date TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                processed_at TEXT,
                last_error TEXT
            );
            """
        )
        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(work_order_state)").fetchall()
        }
        if "source_report_date" not in columns:
            self.connection.execute(
                "ALTER TABLE work_order_state ADD COLUMN source_report_date TEXT"
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def start_run(self, run_id: str, interval: Sequence[pd.Timestamp]) -> None:
        now = datetime.now().isoformat()
        self.connection.execute(
            "INSERT INTO sync_runs(run_id,started_at,base_start,base_end,query_start,query_end,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, now, *(str(value) for value in interval), "running"),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, error: str = "", **counts: int) -> None:
        assignments = ["finished_at=?", "status=?", "error=?"]
        values: list[Any] = [datetime.now().isoformat(), status, error]
        for key in ("fetched_count", "candidate_count", "success_count"):
            if key in counts:
                assignments.append(f"{key}=?")
                values.append(int(counts[key]))
        values.append(run_id)
        self.connection.execute(
            f"UPDATE sync_runs SET {', '.join(assignments)} WHERE run_id=?", values
        )
        self.connection.commit()

    def recovery_query_start(
        self,
        default_start: pd.Timestamp,
        current_end: pd.Timestamp,
    ) -> pd.Timestamp:
        """Extend an automatic query to cover earlier failed, unrecovered windows."""
        failed_runs = self.connection.execute(
            "SELECT started_at,query_start,query_end FROM sync_runs WHERE status='failed'"
        ).fetchall()
        recovery_start = pd.Timestamp(default_start)
        for failed in failed_runs:
            failed_start = pd.Timestamp(failed["query_start"])
            failed_end = pd.Timestamp(failed["query_end"])
            if failed_end > current_end:
                continue
            covered = self.connection.execute(
                "SELECT 1 FROM sync_runs "
                "WHERE started_at>? AND status IN ('success','no_changes') "
                "AND query_start<=? AND query_end>=? LIMIT 1",
                (failed["started_at"], str(failed_start), str(failed_end)),
            ).fetchone()
            if not covered:
                recovery_start = min(recovery_start, failed_start)
        return recovery_start

    def ingest(self, rows: Iterable[Mapping[str, Any]], force: bool = False) -> int:
        changed = 0
        now = datetime.now().isoformat()
        for row in rows:
            wo = normalize_work_order_no(row.get("work_order_no"))
            if not wo:
                continue
            digest = source_hash(row)
            old = self.connection.execute(
                "SELECT source_hash,status FROM work_order_state WHERE work_order_no=?", (wo,)
            ).fetchone()
            is_changed = force or old is None or old["source_hash"] != digest
            if is_changed:
                changed += 1
                status = "pending"
            else:
                status = old["status"]
            self.connection.execute(
                """
                INSERT INTO work_order_state(
                    work_order_no,source_hash,source_report_date,payload_json,status,
                    attempt_count,first_seen_at,last_seen_at,processed_at,last_error
                ) VALUES(?,?,?,?,?,0,?,?,NULL,'')
                ON CONFLICT(work_order_no) DO UPDATE SET
                    source_hash=excluded.source_hash,
                    source_report_date=excluded.source_report_date,
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    processed_at=CASE WHEN excluded.status='pending' THEN NULL ELSE work_order_state.processed_at END,
                    last_error=CASE WHEN excluded.status='pending' THEN '' ELSE work_order_state.last_error END
                """,
                (
                    wo,
                    digest,
                    clean_text(row.get("report_date")),
                    json.dumps(dict(row), ensure_ascii=False),
                    status,
                    now,
                    now,
                ),
            )
        self.connection.commit()
        return changed

    def pending_rows(self) -> list[dict[str, str]]:
        records = self.connection.execute(
            "SELECT payload_json FROM work_order_state WHERE status IN ('pending','failed') "
            "ORDER BY source_report_date,work_order_no"
        ).fetchall()
        return [json.loads(record["payload_json"]) for record in records]

    def mark(self, work_order_no: str, status: str, error: str = "") -> None:
        self.connection.execute(
            "UPDATE work_order_state SET status=?,attempt_count=attempt_count+1,"
            "processed_at=?,last_error=? WHERE work_order_no=?",
            (status, datetime.now().isoformat(), error[:4000], work_order_no),
        )
        self.connection.commit()


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, OSError) as exc:
        raise RuntimeError("已有一个工单同步任务正在运行") from exc
    finally:
        try:
            if os.name != "nt":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def rows_to_workbook(rows: Sequence[Mapping[str, Any]], path: Path) -> Path:
    output: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        converted: dict[str, str] = {"NO.": str(index)}
        for source, target in SOURCE_TO_XLSX.items():
            value = clean_text(row.get(source, ""))
            if target not in converted or value:
                converted[target] = value
        output.append(converted)
    frame = pd.DataFrame(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_excel(path, index=False, engine="openpyxl")
    return path


def extractor_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "work_order_no": normalize_work_order_no(row.get("work_order_no")),
            "train": normalize_train(row.get("equip_instance", "")),
            "car_no": normalize_car_no(row.get("equip_unit", "")),
            "description": clean_text(row.get("description", "")),
            "report_date": clean_text(row.get("report_date", "")),
            "change_date": clean_text(row.get("change_date", "")),
        }
        for row in rows
    ]


def merge_work_order_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    incoming = pd.DataFrame([dict(row) for row in rows]).fillna("")
    if path.exists():
        old = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        merged = pd.concat([old, incoming], ignore_index=True, sort=False)
    else:
        merged = incoming
    if "work_order_no" in merged.columns:
        merged["work_order_no"] = merged["work_order_no"].apply(normalize_work_order_no)
        merged = merged.drop_duplicates("work_order_no", keep="last")
    return merged.reset_index(drop=True)


def all_metric_codes(path: Path) -> list[str]:
    return sorted({str(item["code"]).strip() for item in load_metrics(str(path))})


def process_cases_from_iceberg(
    work_df: pd.DataFrame,
    metric_codes: Sequence[str],
    event_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, dict[str, int]], list[dict[str, Any]]]:
    all_ok: list[dict[str, Any]] = []
    all_logs: list[dict[str, Any]] = []
    all_segments: dict[str, Any] = {}
    all_counts: dict[str, dict[str, int]] = {}
    all_missing: list[dict[str, Any]] = []

    for _, row in work_df.iterrows():
        one = pd.DataFrame([row.to_dict()])
        fault_text = clean_text(row.get("fault_time", ""))
        if not fault_text:
            result = process_work_df(one, data_dir=".", time_is_cst=True)
        else:
            fault_cst = pd.Timestamp(fault_text)
            fault_utc = fault_cst - pd.Timedelta(hours=8)
            try:
                raw = read_iceberg_raw_window(
                    train=normalize_train(row.get("train", "")),
                    fault_time_utc=fault_utc,
                    partition_time_cst=fault_cst,
                    codes=metric_codes,
                    event_code=event_code,
                    before_minutes=READ_BEFORE_MIN,
                    after_minutes=READ_AFTER_MIN,
                )
                with tempfile.TemporaryDirectory(prefix="wo_iceberg_") as temp_name:
                    temp_dir = Path(temp_name)
                    dates = pd.date_range(
                        (fault_cst - pd.Timedelta(minutes=READ_BEFORE_MIN)).normalize(),
                        (fault_cst + pd.Timedelta(minutes=READ_AFTER_MIN)).normalize(),
                        freq="D",
                    )
                    for date in dates:
                        date_text = date.strftime("%Y-%m-%d")
                        if "ts_date" in raw.columns:
                            part = raw[raw["ts_date"].astype(str) == date_text]
                        else:
                            part = raw
                        part.to_csv(temp_dir / f"{date_text}.csv", index=False, encoding="utf-8")
                    result = process_work_df(one, data_dir=temp_dir, time_is_cst=True)
            except Exception as exc:
                log = {
                    "case_id": clean_text(row.get("case_id")),
                    "work_order_no": clean_text(row.get("work_order_no")),
                    "time_order": row.get("time_order", 0),
                    "remark": clean_text(row.get("remark")),
                    "train": normalize_train(row.get("train", "")),
                    "car_no": normalize_car_no(row.get("car_no", "")),
                    "fault_time_raw": fault_text,
                    "fault_time_utc": str(fault_utc),
                    "status": "iceberg_error",
                    "raw_rows": 0,
                    "event_count": 0,
                    "segments_all_count": 0,
                    "segments_target_count": 0,
                    "required_dates": "",
                    "missing_dates": "",
                    "error": str(exc),
                }
                result = ([], [log], {}, {}, [])

        ok, logs, segments, counts, missing = result
        for item in ok:
            item["data_source"] = "iceberg"
        for item in logs:
            item["data_source"] = "iceberg"
        all_ok.extend(ok)
        all_logs.extend(logs)
        all_segments.update(segments)
        all_counts.update(counts)
        all_missing.extend(missing)
    return all_ok, all_logs, all_segments, all_counts, all_missing


def remove_work_orders_from_library(stage: Path, work_orders: set[str]) -> None:
    metadata_path = stage / "metadata.csv"
    if metadata_path.exists():
        metadata = pd.read_csv(metadata_path, dtype=str).fillna("")
        removed_ids = set(
            metadata.loc[metadata["work_order_no"].astype(str).isin(work_orders), "case_id"].astype(str)
        )
        metadata = metadata[~metadata["work_order_no"].astype(str).isin(work_orders)]
        metadata.to_csv(metadata_path, index=False, encoding="utf-8-sig")
        for filename in ("segments.joblib", "node_counts.joblib"):
            path = stage / filename
            if path.exists():
                values = joblib.load(path)
                for case_id in removed_ids:
                    values.pop(case_id, None)
                joblib.dump(values, path)
    log_path = stage / "process_log.csv"
    if log_path.exists():
        log = pd.read_csv(log_path, dtype=str).fillna("")
        log = log[~log["work_order_no"].astype(str).isin(work_orders)]
        log.to_csv(log_path, index=False, encoding="utf-8-sig")


def build_staged_library(
    live_dir: Path,
    stage_dir: Path,
    work_df: pd.DataFrame,
    metric_codes: Sequence[str],
    event_code: str,
    full_case_csv: Path,
) -> tuple[set[str], dict[str, str]]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    if live_dir.exists():
        shutil.copytree(live_dir, stage_dir, dirs_exist_ok=True)
    ok, logs, new_segments, new_counts, missing = process_cases_from_iceberg(
        work_df, metric_codes, event_code
    )
    successful = {str(item["work_order_no"]) for item in ok}
    statuses: dict[str, str] = {}
    for wo, group in pd.DataFrame(logs).groupby("work_order_no") if logs else []:
        status_values = [clean_text(value) for value in group["status"].tolist()]
        statuses[str(wo)] = "success" if str(wo) in successful else ",".join(status_values)

    if not successful:
        return successful, statuses

    remove_work_orders_from_library(stage_dir, successful)
    metadata_old = (
        pd.read_csv(stage_dir / "metadata.csv") if (stage_dir / "metadata.csv").exists() else pd.DataFrame()
    )
    log_old = (
        pd.read_csv(stage_dir / "process_log.csv") if (stage_dir / "process_log.csv").exists() else pd.DataFrame()
    )
    segments = joblib.load(stage_dir / "segments.joblib") if (stage_dir / "segments.joblib").exists() else {}
    counts = joblib.load(stage_dir / "node_counts.joblib") if (stage_dir / "node_counts.joblib").exists() else {}
    old_node_index = joblib.load(stage_dir / "node_index.joblib") if (stage_dir / "node_index.joblib").exists() else {}

    # The compact deployment bundle historically contained only metadata,
    # vectors, node_index and segments.  Restore the missing count dictionaries
    # from those segments before rebuilding, otherwise every old vector would
    # silently become an all-zero vector on the first scheduled update.
    if not metadata_old.empty and "case_id" in metadata_old.columns:
        for _, meta in metadata_old.iterrows():
            case_id = str(meta.get("case_id", ""))
            if case_id and case_id not in counts and case_id in segments:
                counts[case_id] = segment_to_node_counts(
                    segments[case_id], car_no=meta.get("car_no", "")
                )

    metadata = pd.concat([metadata_old, pd.DataFrame(ok)], ignore_index=True, sort=False)
    process_log = pd.concat([log_old, pd.DataFrame(logs)], ignore_index=True, sort=False)
    segments.update(new_segments)
    counts.update(new_counts)
    missing_df = pd.DataFrame(missing)
    save_case_library_files(
        save_dir=stage_dir,
        metadata_df=metadata,
        process_log_df=process_log,
        segments_store=segments,
        node_counts_store=counts,
        data_dir="iceberg",
        case_csv=full_case_csv,
        time_is_cst=True,
        missing_date_df=missing_df,
        update_info={
            "mode": "scheduled-workflow-iceberg",
            "successful_work_orders": sorted(successful),
            "old_vector_dim": len(old_node_index),
            "event_code": event_code,
        },
    )
    return successful, statuses


def update_structured_reference(
    rows: Sequence[Mapping[str, Any]],
    workbook_path: Path,
    live_reference: Path,
    staged_reference: Path,
    config_path: Path,
) -> None:
    extractor_dir = ROOT / "fault_extractor" / "fault_extractor"
    sys.path.insert(0, str(extractor_dir))
    try:
        from work_order_reference_parser import build_reference_data_json
    finally:
        sys.path.pop(0)
    if live_reference.exists():
        staged_reference.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live_reference, staged_reference)
    summary = build_reference_data_json(
        xlsx_path=workbook_path,
        output_json_path=staged_reference,
        config_path=config_path,
        regenerate_all=False,
        print_progress=True,
        progress_every=1,
        checkpoint_every=1,
        repair_attempts=2,
        use_rule_fallback=True,
    )
    if summary.get("failed"):
        raise RuntimeError(f"结构化工单仍有失败项：{summary.get('failed_work_orders')}")


def atomic_copy_files(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for source in source_dir.iterdir():
        if not source.is_file():
            continue
        temp = destination_dir / f".{source.name}.syncing"
        shutil.copy2(source, temp)
        os.replace(temp, destination_dir / source.name)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="定期查询已完成 VCB 工单并更新案例库")
    parser.add_argument("--start", help="手动区间开始（CST，含）")
    parser.add_argument("--end", help="手动区间结束（CST，不含）")
    parser.add_argument("--overlap-days", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--workflow-attempts", type=int, default=3)
    parser.add_argument("--runtime-dir", default=os.getenv("WORK_ORDER_SYNC_RUNTIME", str(DEFAULT_RUNTIME_DIR)))
    parser.add_argument("--library-dir", default=os.getenv("CASE_LIBRARY_DIR", str(DEFAULT_LIBRARY_DIR)))
    parser.add_argument("--full-case-csv", default=os.getenv("FULL_CASE_CSV", str(DEFAULT_FULL_CASE_CSV)))
    parser.add_argument("--metrics-json", default=str(DEFAULT_METRICS_JSON))
    parser.add_argument("--reference-json", default=os.getenv("FAULT_REFERENCE_DATA_PATH", str(DEFAULT_REFERENCE_JSON)))
    parser.add_argument("--llm-config", default=os.getenv("WORK_ORDER_LLM_CONFIG", str(DEFAULT_LLM_CONFIG)))
    parser.add_argument("--event-code", default=os.getenv("HISTORICAL_EVENT_CODE", "1757"))
    parser.add_argument("--force-reprocess", action="store_true")
    parser.add_argument("--query-only", action="store_true")
    parser.add_argument("--skip-qdrant", action="store_true")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    load_dotenv(ROOT / ".env", override=False)
    args = parse_args(argv)
    runtime = Path(args.runtime_dir).expanduser().resolve()
    library = Path(args.library_dir).expanduser().resolve()
    full_case_csv = Path(args.full_case_csv).expanduser().resolve()
    reference_json = Path(args.reference_json).expanduser().resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    state = SyncState(runtime / "state.sqlite3")
    interval = resolve_interval(args.start, args.end, args.overlap_days)
    base_start, base_end, query_start, query_end = interval
    if not args.start:
        recovered_start = state.recovery_query_start(query_start, query_end)
        if recovered_start < query_start:
            print(f"检测到尚未回补的失败同步区间，查询起点由 {query_start} 提前到 {recovered_start}")
            query_start = recovered_start
            interval = base_start, base_end, query_start, query_end
    sql = build_sql(query_start, query_end)
    print(f"同步区间：[{base_start}, {base_end})；实际查询从 {query_start} 开始")

    state.start_run(run_id, interval)
    fetched_count = candidate_count = success_count = 0
    try:
        rows, response_payload = call_workflow(
            sql,
            args.timeout,
            attempts=args.workflow_attempts,
        )
        fetched_count = len(rows)
        archive_dir = runtime / "raw"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f"{run_id}.json").write_text(
            json.dumps(
                {"sql": sql, "rows": rows, "workflow_response": response_payload},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"工作流返回 {fetched_count} 条有效工单。")
        if args.query_only:
            state.finish_run(run_id, "query_only", fetched_count=fetched_count)
            return 0

        state.ingest(rows, force=args.force_reprocess)
        candidates = state.pending_rows()
        candidate_count = len(candidates)
        print(f"本次待处理（含历史失败重试）{candidate_count} 条。")
        if not candidates:
            state.finish_run(run_id, "no_changes", fetched_count=fetched_count, candidate_count=0)
            return 0

        with exclusive_lock(runtime / "sync.lock"):
            with tempfile.TemporaryDirectory(prefix="wo_sync_", dir=runtime) as temp_name:
                temp = Path(temp_name)
                qdrant_base = temp / "qdrant_base"
                export_complete_qdrant_library(
                    destination_dir=qdrant_base,
                    qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL),
                    collection_name=os.getenv("QDRANT_COLLECTION", DEFAULT_QDRANT_COLLECTION),
                )
                workbook = rows_to_workbook(candidates, temp / "work_orders.xlsx")
                update_case_csv = temp / "update_case_input.csv"
                update_no_time_csv = temp / "update_no_time.csv"
                extracted = extract_case_inputs_from_rows(
                    extractor_rows(candidates),
                    out_case_csv=update_case_csv,
                    out_no_time_csv=update_no_time_csv,
                    config_path=Path(args.llm_config),
                    use_llm=True,
                    fallback_to_regex=True,
                    print_progress=True,
                )
                work_df = load_case_input_for_build(update_case_csv, desc_csv=None)
                source_by_wo = {row["work_order_no"]: row for row in candidates}
                work_df["long_description"] = work_df["work_order_no"].map(
                    lambda wo: clean_text(source_by_wo.get(str(wo), {}).get("long_description", ""))
                )

                staged_library = temp / "case_library"
                successful, statuses = build_staged_library(
                    qdrant_base,
                    staged_library,
                    work_df,
                    all_metric_codes(qdrant_base / "gateway_metrics.json"),
                    str(args.event_code),
                    full_case_csv,
                )
                success_count = len(successful)

                staged_reference = temp / reference_json.name
                update_structured_reference(
                    candidates,
                    workbook,
                    qdrant_base / "structured_work_orders.json",
                    staged_reference,
                    Path(args.llm_config),
                )

                merged_work_orders = merge_work_order_csv(qdrant_base / "work_orders.csv", candidates)
                merged_work_orders.to_csv(
                    staged_library / "work_orders.csv", index=False, encoding="utf-8-sig"
                )

                if successful:
                    if (qdrant_base / "metadata.csv").exists():
                        backup_case_library(
                            library_dir=qdrant_base,
                            case_csv=full_case_csv,
                            backup_dir=runtime / "backups",
                            keep_last=10,
                        )
                    if not args.skip_qdrant:
                        sync_library_to_qdrant(
                            library_dir=staged_library,
                            qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL),
                            collection_name=os.getenv("QDRANT_COLLECTION", DEFAULT_QDRANT_COLLECTION),
                            recreate=True,
                            reference_data_path=staged_reference,
                            work_order_records_path=staged_library / "work_orders.csv",
                        )

                if full_case_csv.exists():
                    old_full = pd.read_csv(full_case_csv, dtype=str, encoding="utf-8-sig")
                    updated_orders = set(extracted["work_order_no"].astype(str))
                    old_full = old_full[~old_full["work_order_no"].astype(str).isin(updated_orders)]
                    full = pd.concat([old_full, extracted], ignore_index=True, sort=False)
                else:
                    full = extracted
                full_temp = full_case_csv.with_name(f".{full_case_csv.name}.syncing")
                full.to_csv(full_temp, index=False, encoding="utf-8-sig")
                os.replace(full_temp, full_case_csv)

                for row in candidates:
                    wo = normalize_work_order_no(row.get("work_order_no"))
                    status = statuses.get(wo, "")
                    if wo in successful:
                        state.mark(wo, "success")
                    elif status == "no_explicit_fault_time":
                        state.mark(wo, "no_case", "工单描述没有可提取的明确故障时间")
                    else:
                        state.mark(wo, "failed", status or "未生成有效 segment")

        state.finish_run(
            run_id,
            "success",
            fetched_count=fetched_count,
            candidate_count=candidate_count,
            success_count=success_count,
        )
        print(f"同步完成：成功写入案例 {success_count} 个；前端容器需重启后加载新版。")
        return 0
    except Exception as exc:
        state.finish_run(
            run_id,
            "failed",
            error=str(exc),
            fetched_count=fetched_count,
            candidate_count=candidate_count,
            success_count=success_count,
        )
        print(f"同步失败，旧案例库保持可用：{exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(run())
