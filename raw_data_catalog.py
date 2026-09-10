# -*- coding: utf-8 -*-
"""Persistent catalog for downloaded raw time-series windows.

The large point data stays in partitioned CSV files. SQLite keeps the durable
mapping from a query/work order to its data directory so the UI can find and
reuse those files after a restart or after the project folder is moved.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG_PATH = Path(
    os.getenv("RAW_DATA_CATALOG_PATH", str(PROJECT_ROOT / "raw_data_catalog.sqlite3"))
).expanduser()


def _connect(db_path: str | Path = DEFAULT_CATALOG_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_windows (
            window_key TEXT PRIMARY KEY,
            work_order_no TEXT NOT NULL DEFAULT '',
            train TEXT NOT NULL,
            car_no TEXT NOT NULL DEFAULT '',
            fault_code TEXT NOT NULL DEFAULT '',
            fault_time_raw TEXT NOT NULL,
            fault_time_utc TEXT NOT NULL DEFAULT '',
            data_dir TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            metric_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_windows_work_order ON raw_windows(work_order_no)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_windows_train_time ON raw_windows(train, fault_time_raw)"
    )
    connection.commit()
    return connection


def _time_text(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return ""
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _window_key(train: Any, fault_time_raw: Any) -> str:
    identity = f"{str(train).strip().upper()}|{_time_text(fault_time_raw)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _stored_path(data_dir: str | Path) -> str:
    path = Path(data_dir).resolve()
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_data_dir(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def register_raw_window(
    *,
    train: Any,
    fault_time_raw: Any,
    data_dir: str | Path,
    work_order_no: Any = "",
    car_no: Any = "",
    fault_code: Any = "",
    fault_time_utc: Any = "",
    row_count: int = 0,
    metric_count: int = 0,
    db_path: str | Path = DEFAULT_CATALOG_PATH,
) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    raw_time = _time_text(fault_time_raw)
    if not raw_time:
        raise ValueError("fault_time_raw 不能为空")
    payload = {
        "window_key": _window_key(train, raw_time),
        "work_order_no": str(work_order_no or "").strip(),
        "train": str(train or "").strip().upper(),
        "car_no": str(car_no or "").strip(),
        "fault_code": str(fault_code or "").strip().upper(),
        "fault_time_raw": raw_time,
        "fault_time_utc": _time_text(fault_time_utc) if str(fault_time_utc or "").strip() else "",
        "data_dir": _stored_path(data_dir),
        "row_count": int(row_count or 0),
        "metric_count": int(metric_count or 0),
        "created_at": now,
        "updated_at": now,
    }
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO raw_windows (
                window_key, work_order_no, train, car_no, fault_code,
                fault_time_raw, fault_time_utc, data_dir, row_count,
                metric_count, created_at, updated_at
            ) VALUES (
                :window_key, :work_order_no, :train, :car_no, :fault_code,
                :fault_time_raw, :fault_time_utc, :data_dir, :row_count,
                :metric_count, :created_at, :updated_at
            )
            ON CONFLICT(window_key) DO UPDATE SET
                work_order_no = CASE
                    WHEN excluded.work_order_no <> '' THEN excluded.work_order_no
                    ELSE raw_windows.work_order_no
                END,
                car_no = CASE WHEN excluded.car_no <> '' THEN excluded.car_no ELSE raw_windows.car_no END,
                fault_code = CASE
                    WHEN excluded.fault_code <> '' THEN excluded.fault_code
                    ELSE raw_windows.fault_code
                END,
                fault_time_utc = CASE
                    WHEN excluded.fault_time_utc <> '' THEN excluded.fault_time_utc
                    ELSE raw_windows.fault_time_utc
                END,
                data_dir = excluded.data_dir,
                row_count = excluded.row_count,
                metric_count = excluded.metric_count,
                updated_at = excluded.updated_at
            """,
            payload,
        )
    return payload


def find_raw_window(
    *,
    work_order_no: Any = "",
    train: Any = "",
    fault_time_raw: Any = "",
    db_path: str | Path = DEFAULT_CATALOG_PATH,
) -> dict[str, Any] | None:
    clauses: list[str] = []
    values: list[Any] = []
    work_order = str(work_order_no or "").strip()
    if work_order:
        clauses.append("work_order_no = ?")
        values.append(work_order)
    train_text = str(train or "").strip().upper()
    time_text = _time_text(fault_time_raw) if str(fault_time_raw or "").strip() else ""
    if train_text and time_text:
        clauses.append("(train = ? AND fault_time_raw = ?)")
        values.extend([train_text, time_text])
    if not clauses or not Path(db_path).exists():
        return None

    with _connect(db_path) as connection:
        row = connection.execute(
            f"SELECT * FROM raw_windows WHERE {' OR '.join(clauses)} ORDER BY updated_at DESC LIMIT 1",
            values,
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    resolved = resolve_data_dir(result["data_dir"])
    if not resolved.exists() or not any(resolved.glob("*.csv")):
        return None
    result["data_dir"] = str(resolved)
    return result
