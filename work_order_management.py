"""Persistent, recoverable work-order exclusions owned by this application."""

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def exclusions(path, *, recommendation_only=False):
    path = Path(path)
    if not path.exists():
        return set()
    with closing(sqlite3.connect(str(path), timeout=10)) as connection:
        sql = "SELECT work_order FROM exclusions"
        if recommendation_only:
            sql += " WHERE exclude_recommendation = 1"
        return {row[0] for row in connection.execute(sql)}


def exclude_work_order(path, work_order, actor, *, exclude_recommendation):
    work_order = str(work_order).strip()
    if not re.fullmatch(r"\d{10}", work_order):
        raise ValueError("工单号必须是10位数字")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=10)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS exclusions ("
            "work_order TEXT PRIMARY KEY, exclude_recommendation INTEGER NOT NULL, "
            "deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO exclusions VALUES (?, ?, ?, ?)",
            (work_order, int(exclude_recommendation),
             datetime.now(timezone.utc).isoformat(), str(actor)),
        )
