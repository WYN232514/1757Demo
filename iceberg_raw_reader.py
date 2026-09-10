# -*- coding: utf-8 -*-
"""Read a narrow historical raw-data window directly from Iceberg.

The interactive application must never materialize a whole ``ts_date``
partition.  Every scan is restricted by date, event code, train, UTC time and
the exact metric codes required by the plot.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

import pandas as pd
import yaml
from pyiceberg.catalog import load_catalog
from pyiceberg.expressions import (
    And,
    EqualTo,
    GreaterThanOrEqual,
    In,
    LessThanOrEqual,
)


DEFAULT_WAREHOUSE = "s3://mtr-inf-backup/R-Train/iceberg/fault_related_data"
DEFAULT_DATABASE = "iceberg_db"
DEFAULT_TABLE = "fault_related_data"
DEFAULT_REGION = "ap-east-1"
SELECTED_FIELDS = (
    "_time",
    "_value",
    "code",
    "equipInstance",
    "status",
    "ts_date",
    "eventCode",
)

_TABLE_LOCK = RLock()


class IcebergRawDataError(RuntimeError):
    """Raised when a historical raw-data window cannot be read."""


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def iceberg_enabled() -> bool:
    return _truthy(os.getenv("ICEBERG_RAW_ENABLED"), default=True)


def _max_attempts() -> int:
    try:
        retries = max(0, int(os.getenv("ICEBERG_QUERY_RETRIES", "2")))
    except ValueError:
        retries = 2
    return retries + 1


def _local_config() -> Mapping[str, Any]:
    """Load the ignored local config only as a developer-machine fallback.

    Production should use an EC2 IAM role and environment variables.  The
    config file is deliberately not copied into the Docker image.
    """
    configured = os.getenv("ICEBERG_CONFIG_FILE", "").strip()
    path = Path(configured).expanduser() if configured else Path(__file__).with_name("config.yaml")
    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise IcebergRawDataError(f"读取 Iceberg 配置失败：{exc}") from exc
    return value if isinstance(value, Mapping) else {}


def _setting(name: str, config: Mapping[str, Any], default: str = "") -> str:
    return str(os.getenv(name) or config.get(name) or default).strip()


@lru_cache(maxsize=1)
def get_iceberg_table():
    """Create and cache the Glue catalog table object for this worker."""
    if not iceberg_enabled():
        raise IcebergRawDataError("ICEBERG_RAW_ENABLED 已关闭")

    config = _local_config()
    region = _setting("AWS_REGION", config, DEFAULT_REGION)
    warehouse = _setting("ICEBERG_WAREHOUSE", config, DEFAULT_WAREHOUSE)
    database = _setting("ICEBERG_DB", config, DEFAULT_DATABASE)
    table_name = _setting("ICEBERG_TABLE", config, DEFAULT_TABLE)

    catalog_options: dict[str, str] = {
        "type": "glue",
        "warehouse": warehouse,
        "client.region": region,
    }

    # Local-only fallback.  On EC2 these values should be absent so boto3 uses
    # the instance role automatically.
    access_key = _setting("AWS_ACCESS_KEY_ID", config) or _setting("AWS_AK", config)
    secret_key = _setting("AWS_SECRET_ACCESS_KEY", config) or _setting("AWS_SK", config)
    session_token = _setting("AWS_SESSION_TOKEN", config)
    if access_key and secret_key:
        catalog_options["client.access-key-id"] = access_key
        catalog_options["client.secret-access-key"] = secret_key
    if session_token:
        catalog_options["client.session-token"] = session_token

    last_error: Exception | None = None
    with _TABLE_LOCK:
        for attempt in range(_max_attempts()):
            try:
                catalog = load_catalog("glue", **catalog_options)
                return catalog.load_table(f"{database}.{table_name}")
            except Exception as exc:
                last_error = exc
                if attempt + 1 < _max_attempts():
                    time.sleep(attempt + 1)
    raise IcebergRawDataError(f"连接 Iceberg/Glue 失败：{last_error}") from last_error


def _date_values(local_start: pd.Timestamp, local_end: pd.Timestamp) -> set[str]:
    start = pd.Timestamp(local_start).normalize()
    end = pd.Timestamp(local_end).normalize()
    return {value.strftime("%Y-%m-%d") for value in pd.date_range(start, end, freq="D")}


def _clean_codes(codes: Iterable[Any]) -> set[str]:
    return {str(code).strip() for code in codes if str(code).strip()}


def read_iceberg_raw_window(
    *,
    train: Any,
    fault_time_utc: Any,
    partition_time_cst: Any,
    codes: Iterable[Any],
    event_code: Any = "1757",
    before_minutes: float = 10,
    after_minutes: float = 10,
) -> pd.DataFrame:
    """Return the same columns/types expected from ``read_local_raw_data``.

    ``_time`` is filtered in UTC, while ``ts_date`` follows the existing CST
    partition convention.  All dates touched by the requested local window are
    included, so queries crossing midnight remain correct.
    """
    train_text = str(train).strip()
    event_text = str(event_code).strip()
    code_values = _clean_codes(codes)
    if not train_text:
        raise IcebergRawDataError("Iceberg 查询缺少车组")
    if not event_text:
        raise IcebergRawDataError("Iceberg 查询缺少故障码")
    if not code_values:
        raise IcebergRawDataError("Iceberg 查询没有需要绘制的测点")

    before = float(before_minutes)
    after = float(after_minutes)
    if before < 0 or after < 0:
        raise IcebergRawDataError("Iceberg 查询时间窗口不能为负数")

    fault_utc = pd.Timestamp(fault_time_utc)
    partition_cst = pd.Timestamp(partition_time_cst)
    utc_start = fault_utc - pd.Timedelta(minutes=before)
    utc_end = fault_utc + pd.Timedelta(minutes=after)
    local_start = partition_cst - pd.Timedelta(minutes=before)
    local_end = partition_cst + pd.Timedelta(minutes=after)
    partition_dates = _date_values(local_start, local_end)

    # The Iceberg column is an ISO-like string. Lexicographic comparison is
    # therefore equivalent to timestamp comparison for this normalized format.
    row_filter = And(
        And(In("ts_date", partition_dates), EqualTo("eventCode", event_text)),
        And(
            EqualTo("equipInstance", train_text),
            And(
                And(
                    GreaterThanOrEqual("_time", utc_start.strftime("%Y-%m-%d %H:%M:%S")),
                    LessThanOrEqual("_time", utc_end.strftime("%Y-%m-%d %H:%M:%S.%f")),
                ),
                In("code", code_values),
            ),
        ),
    )

    frame = None
    last_error: Exception | None = None
    for attempt in range(_max_attempts()):
        try:
            table = get_iceberg_table()
            frame = table.scan(
                row_filter=row_filter,
                selected_fields=SELECTED_FIELDS,
            ).to_pandas()
            break
        except Exception as exc:
            last_error = exc
            # A stale table object or a transient Glue/S3 connection should not
            # poison later requests in this worker.
            get_iceberg_table.cache_clear()
            if attempt + 1 < _max_attempts():
                time.sleep(attempt + 1)
    if frame is None:
        if isinstance(last_error, IcebergRawDataError):
            raise last_error
        raise IcebergRawDataError(f"Iceberg 原始数据查询失败：{last_error}") from last_error

    if frame is None or frame.empty:
        return pd.DataFrame(columns=SELECTED_FIELDS)

    frame = frame.copy()
    frame["_time"] = pd.to_datetime(frame["_time"], errors="coerce")
    frame["_value"] = pd.to_numeric(frame["_value"], errors="coerce")
    frame = frame.dropna(subset=["_time", "_value", "code"])
    frame = frame[
        (frame["_time"] >= utc_start)
        & (frame["_time"] <= utc_end)
        & frame["code"].astype(str).isin(code_values)
    ]
    return frame.sort_values(["code", "_time"]).reset_index(drop=True)
