# -*- coding: utf-8 -*-
"""Railiverse Gateway 时序数据下载与现有分析格式转换。

主要入口是 :func:`download_fault_window`：输入车号和故障时间，下载该车组
故障前后指定窗口内的全部测点，并转换为 ``01_数据整理`` 使用的长表格式。

认证信息只从环境变量读取：
    RAILIVERSE_ENDPOINT
    RAILIVERSE_CLIENT_ID
    RAILIVERSE_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import requests


GATEWAY = "https://phm.railiverse.com/gateway/"
DEFAULT_DATA_DIR = "01_数据整理"
DEFAULT_OUTPUT_ROOT = "gateway_downloads/queries"
DEFAULT_BEFORE_MIN = 30
DEFAULT_AFTER_MIN = 30
DEFAULT_BATCH_SIZE = 25
STANDARD_COLUMNS = [
    "_time",
    "_value",
    "code",
    "equipInstance",
    "status",
    "ts_date",
    "eventCode",
]


@dataclass(frozen=True)
class FaultDownloadResult:
    data_dir: Path
    row_count: int
    metric_count: int
    requested_start_utc: pd.Timestamp
    requested_end_utc: pd.Timestamp
    files: tuple[Path, ...]
    reused: bool = False


def yesterday_text() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def normalize_train(value: Any) -> str:
    text = str(value).strip().upper()
    if not text:
        raise ValueError("车号不能为空")
    if text.isdigit():
        text = f"T{text}"
    if not re.fullmatch(r"T\d+", text):
        raise ValueError(f"车号格式错误: {value!r}，示例: T9")
    return text


def normalize_endpoint(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("请设置 RAILIVERSE_ENDPOINT 环境变量")
    if not value.startswith(("http://", "https://")):
        return GATEWAY + value.lstrip("/")
    return value


def credentials() -> tuple[str, str]:
    client_id = os.getenv("RAILIVERSE_CLIENT_ID", "").strip()
    client_secret = os.getenv("RAILIVERSE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "请设置 RAILIVERSE_CLIENT_ID 和 RAILIVERSE_CLIENT_SECRET 环境变量"
        )
    return client_id, client_secret


def coach_from_code(code: str) -> str:
    """从 code 末尾提取车厢号；列车级变量使用 coach=0。"""
    match = re.search(r"_(\d{2})_(?:sd|sdr)$", str(code), flags=re.IGNORECASE)
    return str(int(match.group(1))) if match else "0"


@lru_cache(maxsize=8)
def discover_metrics(data_dir: str = DEFAULT_DATA_DIR) -> tuple[tuple[str, str], ...]:
    """从最近一个历史日 CSV 的 code 列发现完整测点清单。"""
    root = Path(data_dir)
    candidates = sorted(root.glob("????-??-??.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"{root} 中没有可用于提取 code 的日期 CSV")

    last_error: Exception | None = None
    for path in candidates:
        codes: set[str] = set()
        try:
            for chunk in pd.read_csv(path, usecols=["code"], chunksize=500_000):
                values = chunk["code"].dropna().astype(str).str.strip()
                codes.update(value for value in values if value)
            if codes:
                return tuple((code, coach_from_code(code)) for code in sorted(codes))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"无法从 {root} 提取 code 列: {last_error}")


def load_metrics(
    path: str | None = None,
    data_dir: str = DEFAULT_DATA_DIR,
) -> list[dict[str, str]]:
    if path:
        value: Any = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, list) or not value:
            raise ValueError("metrics JSON 必须是非空数组")
        result: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                code = item.strip()
                if not code:
                    raise ValueError("metrics JSON 中不能包含空 code")
                result.append({"code": code, "coach": coach_from_code(code)})
                continue
            if not isinstance(item, dict) or "code" not in item:
                raise ValueError("每个 metric 必须是 code 字符串或至少包含 code 的对象")
            code = str(item["code"]).strip()
            coach = str(item.get("coach", coach_from_code(code))).strip()
            result.append({"code": code, "coach": coach})
        return result
    return [
        {"code": code, "coach": coach}
        for code, coach in discover_metrics(str(Path(data_dir).resolve()))
    ]


def batched(values: Sequence[Any], size: int) -> Iterable[list[Any]]:
    if size <= 0:
        yield list(values)
        return
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def post_csv(
    session: requests.Session,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
    retries: int,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            response = session.post(endpoint, json=payload, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                detail = response.text[:1000].replace("\n", " ")
                message = f"HTTP {response.status_code}: {detail}"
                if response.status_code != 429 and response.status_code < 500:
                    raise ValueError(message)
                raise RuntimeError(message)
            return response
        except ValueError:
            raise
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt <= retries:
                time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"请求最终失败: {last_error}") from last_error


def decode_csv_content(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _find_gateway_header(lines: list[str], expected_codes: set[str]) -> int:
    for idx, line in enumerate(lines[:20]):
        cells = {cell.strip().strip('"') for cell in line.split(",")}
        if cells.intersection(expected_codes):
            return idx
    raise ValueError(
        "网关响应中找不到 code 表头；响应前五行: "
        + " | ".join(lines[:5])[:1000]
    )


def parse_gateway_csv(
    content: bytes | str,
    metrics: Sequence[dict[str, str]],
    train: str,
) -> pd.DataFrame:
    """把网关的宽表响应转换为分析引擎使用的标准长表。"""
    text = decode_csv_content(content) if isinstance(content, bytes) else content
    lines = text.splitlines()
    expected_codes = {str(item["code"]) for item in metrics}
    header_idx = _find_gateway_header(lines, expected_codes)
    wide = pd.read_csv(StringIO(text), skiprows=header_idx)
    wide.columns = [str(col).strip() for col in wide.columns]

    present_codes = [code for code in sorted(expected_codes) if code in wide.columns]
    missing_codes = sorted(expected_codes.difference(present_codes))
    if missing_codes:
        raise ValueError(
            f"网关响应缺少 {len(missing_codes)} 个测点: " + ", ".join(missing_codes[:10])
        )

    lower_cols = {str(col).strip().lower(): col for col in wide.columns}
    date_col = next(
        (lower_cols[name] for name in ("date", "日期") if name in lower_cols),
        wide.columns[0] if len(wide.columns) > 0 else None,
    )
    time_col = next(
        (lower_cols[name] for name in ("time", "时间") if name in lower_cols),
        wide.columns[1] if len(wide.columns) > 1 else None,
    )
    if date_col is None or time_col is None:
        raise ValueError(f"网关响应缺少日期/时间列: {list(wide.columns)}")

    timestamps = pd.to_datetime(
        wide[date_col].astype(str).str.strip()
        + " "
        + wide[time_col].astype(str).str.strip(),
        errors="coerce",
    )
    values = wide[present_codes].apply(pd.to_numeric, errors="coerce")
    values.insert(0, "_time", timestamps)
    long_df = values.melt(id_vars=["_time"], var_name="code", value_name="_value")
    long_df = long_df.dropna(subset=["_time", "_value"]).copy()
    long_df["equipInstance"] = normalize_train(train)
    long_df["status"] = 0
    long_df["ts_date"] = (long_df["_time"] + pd.Timedelta(hours=8)).dt.strftime("%Y-%m-%d")
    long_df["eventCode"] = 1757
    return long_df[STANDARD_COLUMNS].sort_values(["code", "_time"]).reset_index(drop=True)


def _query_key(train: str, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> str:
    raw = f"{train}|{start_utc.isoformat()}|{end_utc.isoformat()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{train}_{start_utc:%Y%m%d_%H%M%S}_{digest}"


def _manifest_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(current.get(key) == value for key, value in expected.items())


def _write_standard_partitions(
    df: pd.DataFrame,
    output_dir: Path,
    local_start: pd.Timestamp,
    local_end: pd.Timestamp,
) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    required_dates = pd.date_range(local_start.normalize(), local_end.normalize(), freq="D")
    files: list[Path] = []
    for day in required_dates:
        day_text = day.strftime("%Y-%m-%d")
        path = output_dir / f"{day_text}.csv"
        part = df[df["ts_date"] == day_text]
        part.to_csv(path, index=False, encoding="utf-8-sig")
        files.append(path)
    return tuple(files)


def download_fault_window(
    train: str,
    fault_time: Any,
    *,
    time_is_cst: bool = True,
    before_min: int = DEFAULT_BEFORE_MIN,
    after_min: int = DEFAULT_AFTER_MIN,
    fault_code: str = "",
    endpoint: str | None = None,
    metrics_json: str | None = None,
    metrics_data_dir: str = DEFAULT_DATA_DIR,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    train_type: str = "Rotem",
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: int = 180,
    retries: int = 2,
    reuse: bool = True,
    session: requests.Session | None = None,
) -> FaultDownloadResult:
    """下载单个故障窗口的全部变量并保存为标准日期 CSV。"""
    train = normalize_train(train)
    fault_local = pd.Timestamp(fault_time)
    if pd.isna(fault_local):
        raise ValueError(f"故障时间格式错误: {fault_time!r}")
    fault_utc = fault_local - pd.Timedelta(hours=8) if time_is_cst else fault_local
    start_utc = fault_utc - pd.Timedelta(minutes=int(before_min))
    end_utc = fault_utc + pd.Timedelta(minutes=int(after_min))
    local_start = fault_local - pd.Timedelta(minutes=int(before_min))
    local_end = fault_local + pd.Timedelta(minutes=int(after_min))

    metrics = load_metrics(metrics_json, metrics_data_dir)
    output_dir = Path(output_root) / _query_key(train, start_utc, end_utc)
    manifest_path = output_dir / "manifest.json"
    manifest_base = {
        "train": train,
        "fault_time": str(fault_local),
        "time_is_cst": bool(time_is_cst),
        "start_utc": str(start_utc),
        "end_utc": str(end_utc),
        "before_min": int(before_min),
        "after_min": int(after_min),
        "metric_count": len(metrics),
        "fault_code": str(fault_code or "").strip(),
    }
    expected_files = tuple(
        output_dir / f"{day:%Y-%m-%d}.csv"
        for day in pd.date_range(local_start.normalize(), local_end.normalize(), freq="D")
    )
    if reuse and _manifest_matches(manifest_path, manifest_base) and all(p.exists() for p in expected_files):
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
        return FaultDownloadResult(
            data_dir=output_dir,
            row_count=int(current.get("row_count", 0)),
            metric_count=len(metrics),
            requested_start_utc=start_utc,
            requested_end_utc=end_utc,
            files=expected_files,
            reused=True,
        )

    endpoint_value = normalize_endpoint(endpoint or os.getenv("RAILIVERSE_ENDPOINT", ""))
    client_id, client_secret = credentials()
    headers = {
        "X-Client-Id": client_id,
        "X-Client-Secret": client_secret,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "text/csv, */*",
        "Connection": "close",
    }

    frames: list[pd.DataFrame] = []
    own_session = session is None
    active_session = session or requests.Session()
    try:
        groups = list(batched(metrics, int(batch_size)))
        for index, group in enumerate(groups, start=1):
            payload = {
                "trainType": train_type,
                "bucket": train,
                "startTime": start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                "endTime": end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                "metrics": group,
                "paramType": "METRIC",
            }
            print(f"网关下载 [{index}/{len(groups)}] {train}，测点 {len(group)} 个")
            response = post_csv(
                active_session, endpoint_value, headers, payload, int(timeout), int(retries)
            )
            frames.append(parse_gateway_csv(response.content, group, train))
    finally:
        if own_session:
            active_session.close()

    if not frames:
        raise RuntimeError("网关没有返回任何数据")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["_time", "code", "equipInstance"], keep="last")
    combined = combined.sort_values(["code", "_time"]).reset_index(drop=True)
    if combined.empty:
        raise RuntimeError("网关响应解析成功，但故障窗口内没有有效测点数据")

    files = _write_standard_partitions(combined, output_dir, local_start, local_end)
    manifest = dict(manifest_base)
    manifest.update({"row_count": len(combined), "files": [p.name for p in files]})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return FaultDownloadResult(
        data_dir=output_dir,
        row_count=len(combined),
        metric_count=len(metrics),
        requested_start_utc=start_utc,
        requested_end_utc=end_utc,
        files=files,
        reused=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载故障窗口内全部 Railiverse 测点")
    parser.add_argument("--train", required=True, help="车号，如 T9")
    parser.add_argument("--fault-time", required=True, help="故障时间 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--fault-code", default="", help="故障码，仅写入查询清单")
    parser.add_argument("--time-is-utc", action="store_true", help="输入时间已经是 UTC；默认按北京时间")
    parser.add_argument("--before-min", type=int, default=DEFAULT_BEFORE_MIN)
    parser.add_argument("--after-min", type=int, default=DEFAULT_AFTER_MIN)
    parser.add_argument("--endpoint", default=os.getenv("RAILIVERSE_ENDPOINT", ""))
    parser.add_argument("--metrics-json")
    parser.add_argument("--metrics-data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--train-type", default="Rotem")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--force", action="store_true", help="忽略已有同窗口缓存，重新下载")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = download_fault_window(
        train=args.train,
        fault_time=args.fault_time,
        fault_code=args.fault_code,
        time_is_cst=not args.time_is_utc,
        before_min=args.before_min,
        after_min=args.after_min,
        endpoint=args.endpoint,
        metrics_json=args.metrics_json,
        metrics_data_dir=args.metrics_data_dir,
        output_root=args.output_root,
        train_type=args.train_type,
        batch_size=args.batch_size,
        timeout=args.timeout,
        retries=args.retries,
        reuse=not args.force,
    )
    action = "复用缓存" if result.reused else "下载完成"
    print(
        f"{action}：{result.metric_count} 个变量，{result.row_count} 行，"
        f"目录 {result.data_dir.resolve()}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
