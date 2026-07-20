# -*- coding: utf-8 -*-
"""VCB/1757 故障分析 HTTP API。"""

from __future__ import annotations

import hmac
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from app_visualize import compute_query
from gateway_download import download_fault_window, load_metrics


API_VERSION = "1.0.0"
METRICS_JSON = Path(os.getenv("GATEWAY_METRICS_JSON", ROOT / "gateway_metrics.json"))
QUERY_ROOT = Path(os.getenv("GATEWAY_QUERY_ROOT", ROOT / "gateway_downloads" / "api_queries"))
CASE_LIBRARY_DIR = Path(os.getenv("CASE_LIBRARY_DIR", ROOT / "case_library"))
_QUERY_LOCK = threading.Lock()


class FaultAnalysisRequest(BaseModel):
    train: str = Field(description="车号，例如 T9")
    car_no: str = Field(description="故障车厢号：02、05 或 09")
    fault_time: str = Field(description="故障时间，例如 2026-07-16 10:10:00")
    fault_code: str = Field(description="故障标识：eventCode（例如 1757）或 P 故障码（例如 P026）")
    top_k: int = Field(default=5, ge=1, le=20)
    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    time_is_cst: bool = Field(default=True, description="默认输入为北京时间")
    force_download: bool = Field(default=False, description="忽略相同窗口缓存并重新下载")

    @field_validator("train")
    @classmethod
    def validate_train(cls, value: str) -> str:
        text = str(value).strip().upper()
        if text.isdigit():
            text = f"T{text}"
        if not re.fullmatch(r"T\d+", text):
            raise ValueError("train 格式错误，示例：T9")
        return text

    @field_validator("car_no")
    @classmethod
    def validate_car_no(cls, value: str) -> str:
        text = str(value).strip()
        if text.isdigit():
            text = f"{int(text):02d}"
        if text not in {"02", "05", "09"}:
            raise ValueError("car_no 必须是 02、05 或 09")
        return text

    @field_validator("fault_time")
    @classmethod
    def validate_fault_time(cls, value: str) -> str:
        text = str(value).strip().replace("T", " ")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("fault_time 格式应为 YYYY-MM-DD HH:MM:SS") from exc
        if parsed.tzinfo is not None:
            raise ValueError("fault_time 不要携带时区；请通过 time_is_cst 指定时间口径")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    @field_validator("fault_code")
    @classmethod
    def validate_fault_code(cls, value: str) -> str:
        text = re.sub(r"\s+", "", str(value)).upper()
        if re.fullmatch(r"\d+", text):
            return text
        match = re.fullmatch(r"P0*(\d+)", text)
        if not match:
            raise ValueError("fault_code 格式错误，示例：1757 或 P026")
        return f"P{int(match.group(1)):03d}"


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("API_KEY", "").strip()
    if expected and (not x_api_key or not hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="无效或缺少 X-API-Key")


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def similar_work_orders(q: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metadata = q["metadata"]
    for rank, idx in enumerate(q["top_idx"], start=1):
        idx = int(idx)
        meta = metadata.iloc[idx]
        fields = {
            "rank": rank,
            "case_id": meta.get("case_id", ""),
            "work_order_no": meta.get("work_order_no", ""),
            "train": meta.get("train", ""),
            "car_no": meta.get("car_no", ""),
            "fault_time": meta.get("fault_time_raw", ""),
            "fault_time_utc": meta.get("fault_time_utc", ""),
            "similarity": q["sims"][idx],
            "raw_similarity": q["raw_sims"][idx],
            "detail_similarity": q["detail_sims"][idx],
            "shared_non_core_count": q["shared_non_core_counts"][idx],
            "ramping_finish_status": q["case_rf_labels"][idx],
            "remark": meta.get("remark", ""),
            "long_description": meta.get("long_description", ""),
            "segment_start": meta.get("segment_start", ""),
            "segment_end": meta.get("segment_end", ""),
            "selection_transition": meta.get("selection_transition", ""),
            "selection_rule": meta.get("selection_rule", ""),
            "selection_note": meta.get("selection_note", ""),
        }
        rows.append({key: json_safe(value) for key, value in fields.items()})
    return rows


def execute_fault_analysis(request: FaultAnalysisRequest) -> dict[str, Any]:
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    with _QUERY_LOCK:
        try:
            download = download_fault_window(
                train=request.train,
                fault_time=request.fault_time,
                fault_code=request.fault_code,
                time_is_cst=request.time_is_cst,
                before_min=30,
                after_min=30,
                metrics_json=str(METRICS_JSON),
                output_root=QUERY_ROOT,
                reuse=not request.force_download,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"网关下载失败: {exc}") from exc

        try:
            q = compute_query(
                train=request.train,
                car_no=request.car_no,
                fault_time=request.fault_time,
                fault_code=request.fault_code,
                data_dir=download.data_dir,
                download_info=download,
                time_is_cst=request.time_is_cst,
                top_k=request.top_k,
                similarity_threshold=request.similarity_threshold,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=f"案例库文件缺失: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"相似工单分析失败: {exc}") from exc

    if not q.get("ok"):
        raise HTTPException(status_code=422, detail=q.get("message", "无法生成故障向量"))

    matches = similar_work_orders(q)
    return {
        "ok": True,
        "request_id": request_id,
        "request": {
            "train": request.train,
            "car_no": request.car_no,
            "fault_time": request.fault_time,
            "fault_code": request.fault_code,
            "time_is_cst": request.time_is_cst,
        },
        "download": {
            "reused": download.reused,
            "metric_count": download.metric_count,
            "row_count": download.row_count,
            "start_utc": json_safe(download.requested_start_utc),
            "end_utc": json_safe(download.requested_end_utc),
        },
        "analysis": {
            "fault_time_utc": q["fault_time_utc"],
            "node_count": q["node_count"],
            "unknown_node_count": q["unknown_node_count"],
            "excluded_query_node_count": q["excluded_query_node_count"],
            "ramping_finish_status": q["query_rf_label"],
            "similarity_threshold": request.similarity_threshold,
            "match_count": len(matches),
        },
        "similar_work_orders": matches,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


app = FastAPI(
    title="VCB/1757 Fault Analysis API",
    description="输入车号、车厢号、故障时间和故障码，返回相似历史工单。",
    version=API_VERSION,
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "VCB/1757 Fault Analysis API",
        "version": API_VERSION,
        "health": "/health",
        "docs": "/docs",
        "query": "/api/v1/fault-analysis",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    required = ["vectors.npy", "metadata.csv", "node_index.joblib", "segments.joblib"]
    missing = [name for name in required if not (CASE_LIBRARY_DIR / name).exists()]
    try:
        metric_count = len(load_metrics(str(METRICS_JSON)))
    except Exception:
        metric_count = 0
    gateway_endpoint_configured = bool(os.getenv("RAILIVERSE_ENDPOINT", "").strip())
    gateway_credentials_configured = bool(
        os.getenv("RAILIVERSE_CLIENT_ID", "").strip()
        and os.getenv("RAILIVERSE_CLIENT_SECRET", "").strip()
    )
    return {
        "status": "ok" if (
            not missing and metric_count > 0
            and gateway_endpoint_configured and gateway_credentials_configured
        ) else "degraded",
        "version": API_VERSION,
        "case_library_dir": str(CASE_LIBRARY_DIR),
        "missing_case_library_files": missing,
        "metric_count": metric_count,
        "gateway_endpoint_configured": gateway_endpoint_configured,
        "gateway_credentials_configured": gateway_credentials_configured,
    }


@app.post("/api/v1/fault-analysis", dependencies=[Depends(require_api_key)])
async def fault_analysis(request: FaultAnalysisRequest) -> dict[str, Any]:
    return await run_in_threadpool(execute_fault_analysis, request)
