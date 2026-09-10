# -*- coding: utf-8 -*-
"""
VCB/1757 相似工单可视化 —— 维度变化热力图版（Plotly Dash）

功能：
    输入一条新故障（车号 + 故障时间）-> 生成故障指纹向量 -> 检索 Top5 最相似历史工单 ->
    画一张「维度变化热力图」：
        行 = 新故障 + Top5 工单（共 6 行）
        列 = 各原始数据变量维度（dir_xxx 那些 code）
        格子 = 该案例在该维度上「变了几次」（0 = 没变，留白；变得越多颜色越深，并标数字）
    纵向看同一维度上新故障与 Top5 是否「同步变化」，一眼看出它们为什么相似、差异在哪。
    图上方给出 Top5 相似工单表格。

说明：
    - 复用 vcb_case_library_direct 的向量化逻辑；完整案例库从 Qdrant schema v2 加载。
    - 每条案例「某维度变了几次」直接由它的 segment 记录统计得到（rcd[1] 即维度），
      与建库时的向量口径一致：
        * 新故障   -> get_local_case_result(...) 的 main_segment
        * 历史工单 -> Qdrant payload 中的 segment（按 case_id 存）
    - 每次查询会重新读取 01_数据整理 原始数据，所以会有几秒延迟，与 03_query 一样属正常现象。

部署：
    pip install dash plotly
    python app_visualize.py
    浏览器打开 http://<服务器IP>:8050
"""

from __future__ import annotations

import os
import re
import sys
import json
import hashlib
import hmac
from collections import OrderedDict
from html import unescape
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Sequence
from urllib.parse import parse_qs

import numpy as np
import pandas as pd
import joblib

import dash
from dash import dcc, html, dash_table, callback_context, no_update
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate
from flask import jsonify, request as flask_request
import plotly.graph_objects as go
from dotenv import load_dotenv
from scipy.cluster.hierarchy import fcluster, leaves_list, linkage
from scipy.spatial.distance import squareform

from vcb_case_library_direct import (
    DEFAULT_DATA_DIR,
    ANALYZE_BEFORE_MIN,
    ANALYZE_AFTER_MIN,
    get_local_case_result,
    read_local_raw_data,
    vectorize_node_counts,
    normalize_train,
    normalize_car_no,
    normalize_code,
    target_car_normalized_dim,
    get_vcb_target_dims_for_car,
    get_ramping_finish_dims_for_car,
    load_complete_library_from_qdrant,
    _load_qdrant_display_name_map,
    _load_qdrant_gateway_metrics,
    _load_qdrant_structured_records,
    _load_qdrant_work_order_payloads,
)
from gateway_download import (
    DEFAULT_AFTER_MIN as GATEWAY_AFTER_MIN,
    DEFAULT_BEFORE_MIN as GATEWAY_BEFORE_MIN,
    download_fault_window,
)
from raw_data_catalog import register_raw_window
from iceberg_raw_reader import IcebergRawDataError, read_iceberg_raw_window

# ============================================================
# 配置
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

VCB_KNOWLEDGE_GRAPH_PATH = Path(
    os.getenv(
        "VCB_KNOWLEDGE_GRAPH_PATH",
        str(ROOT_DIR / "knowledge_graph" / "vcb_trip_knowledge_graph.json"),
    )
).expanduser()
if not VCB_KNOWLEDGE_GRAPH_PATH.is_absolute():
    VCB_KNOWLEDGE_GRAPH_PATH = (ROOT_DIR / VCB_KNOWLEDGE_GRAPH_PATH).resolve()

_VCB_KNOWLEDGE_GRAPH_LOCK = RLock()
_VCB_KNOWLEDGE_GRAPH_CACHE: Dict[str, Any] | None = None
_VCB_KNOWLEDGE_GRAPH_MTIME_NS = -1

DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
DATA_DIR_PATH = Path(DATA_DIR).expanduser()
if not DATA_DIR_PATH.is_absolute():
    DATA_DIR_PATH = (ROOT_DIR / DATA_DIR_PATH).resolve()
GATEWAY_QUERY_ROOT = Path(
    os.getenv("GATEWAY_QUERY_ROOT", str(ROOT_DIR / "gateway_downloads" / "api_queries"))
).expanduser()
HISTORICAL_RAW_CACHE_DIR = Path(
    os.getenv(
        "HISTORICAL_RAW_CACHE_DIR",
        str(ROOT_DIR / "runtime" / "historical_raw_cache"),
    )
).expanduser()
if not HISTORICAL_RAW_CACHE_DIR.is_absolute():
    HISTORICAL_RAW_CACHE_DIR = (ROOT_DIR / HISTORICAL_RAW_CACHE_DIR).resolve()
RAW_PLOT_BEFORE_MIN = 1.0
RAW_PLOT_AFTER_MIN = 0.5
DEFAULT_TOP_K = 3
MIN_TOP_K = 1
MAX_TOP_K = 10
DEFAULT_SIMILARITY_THRESHOLD = 0.7
CLUSTER_SIM_THRESHOLD = 0.7
HOST = "0.0.0.0"
# Layout experiment entry point; keep it isolated from the current frontend.
PORT = 8051
DASH_REQUESTS_PATHNAME_PREFIX = os.getenv("DASH_REQUESTS_PATHNAME_PREFIX", "/").strip() or "/"
if not DASH_REQUESTS_PATHNAME_PREFIX.startswith("/"):
    DASH_REQUESTS_PATHNAME_PREFIX = "/" + DASH_REQUESTS_PATHNAME_PREFIX
if not DASH_REQUESTS_PATHNAME_PREFIX.endswith("/"):
    DASH_REQUESTS_PATHNAME_PREFIX += "/"
FAULT_EXTRACTOR_DIR = Path(__file__).resolve().parent / "fault_extractor" / "fault_extractor"
HISTORICAL_EVENT_CODE = os.getenv("HISTORICAL_EVENT_CODE", "1757").strip() or "1757"
QDRANT_URL = os.getenv("QDRANT_URL", "http://10.0.1.87:6333").strip()
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "vcb_case_library_v2").strip()
CASE_LIBRARY_BACKEND = os.getenv("CASE_LIBRARY_BACKEND", "qdrant").strip().lower()
CASE_LIBRARY_DIR = Path(
    os.getenv("CASE_LIBRARY_DIR", str(ROOT_DIR / "case_library"))
).expanduser()
if not CASE_LIBRARY_DIR.is_absolute():
    CASE_LIBRARY_DIR = (ROOT_DIR / CASE_LIBRARY_DIR).resolve()
if CASE_LIBRARY_BACKEND not in {"qdrant", "local"}:
    raise ValueError("CASE_LIBRARY_BACKEND must be 'qdrant' or 'local'")

# 与 数据分析_excluded_codes.ipynb 一致：True 时这些 code 不参与相似度计算。
EXCLUDE_SELECTED_CODES_FROM_SIMILARITY = True
EXCLUDED_SIMILARITY_CODES = {
    "1157_3_1_01_sdr",
    "1172_3_1_01_sdr",
    "1194_3_1_01_sdr",
    "904_2_1_02_sd",
    "904_2_1_05_sd",
    "904_2_1_09_sd",
    "936_2_1_02_sd",
    "936_2_1_05_sd",
    "936_2_1_09_sd",
}

EXCLUDED_SIMILARITY_NODE_PREFIXES = (
    "target_vcb_904_",
    "target_vcb_936_",
)

# These are train-level/shared variables. Their gateway equipUnit is 0 and
# the ``01`` inside the code is not a passenger-car number. Never rewrite
# these codes to the queried car (02/05/09).
EQUIP_UNIT_ZERO_CODES = frozenset(
    {
        "132_0_8_01_sdr",
        "133_4_4_01_sdr",
        "145_0_8_01_sdr",
        "160_0_8_01_sdr",
        "161_4_4_01_sdr",
        "236_0_8_01_sdr",
        "237_0_4_01_sdr",
        "237_4_4_01_sdr",
        "252_0_8_01_sdr",
        "253_0_4_01_sdr",
        "253_4_4_01_sdr",
    }
)

NEW_COLOR = "#e6194b"

# 热力图配色：0 次=白色（没变），变化越多越深的暖色
HEAT_COLORSCALE = [
    [0.0, "#f8fafc"],
    [0.001, "#eff6ff"],
    [0.25, "#bfdbfe"],
    [0.5, "#60a5fa"],
    [0.75, "#2563eb"],
    [1.0, "#1e3a8a"],
]

# 防止本地代理影响 localhost 连接（与 03_query 一致）
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")

# 查询结果按浏览器会话隔离，避免多名用户互相覆盖。
_MAX_SESSION_QUERIES = 16
_SESSION_QUERIES: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_SESSION_QUERIES_LOCK = RLock()


def _session_key(value: Any) -> str:
    text = str(value or "").strip()
    return text or "__uninitialized__"


def get_session_query(session_id: Any) -> Dict[str, Any] | None:
    key = _session_key(session_id)
    with _SESSION_QUERIES_LOCK:
        query = _SESSION_QUERIES.get(key)
        if query is not None:
            _SESSION_QUERIES.move_to_end(key)
        return query


def set_session_query(session_id: Any, query: Dict[str, Any] | None) -> None:
    key = _session_key(session_id)
    with _SESSION_QUERIES_LOCK:
        _SESSION_QUERIES.pop(key, None)
        if query is not None:
            _SESSION_QUERIES[key] = query
            while len(_SESSION_QUERIES) > _MAX_SESSION_QUERIES:
                _SESSION_QUERIES.popitem(last=False)


# 缓存案例库与 segments（避免每次查询重复加载）
_CACHE: Dict[str, Any] = {}
_REPORT_CACHE: "OrderedDict[Any, Dict[str, Any]]" = OrderedDict()
_REPORT_CACHE_LOCK = RLock()
_REPORT_CACHE_MAX = 32


# ============================================================
# 案例库 / segments 加载
# ============================================================


def _load_qdrant_bundle() -> Dict[str, Any]:
    if "qdrant_bundle" not in _CACHE:
        if CASE_LIBRARY_BACKEND == "local":
            required = {
                "vectors": CASE_LIBRARY_DIR / "vectors.npy",
                "metadata": CASE_LIBRARY_DIR / "metadata.csv",
                "node_index": CASE_LIBRARY_DIR / "node_index.joblib",
                "segments": CASE_LIBRARY_DIR / "segments.joblib",
            }
            missing = [path.name for path in required.values() if not path.is_file()]
            if missing:
                raise FileNotFoundError(
                    f"Local case library is incomplete at {CASE_LIBRARY_DIR}: {missing}"
                )
            raw_work_orders, source_work_orders = _load_qdrant_work_order_payloads(
                CASE_LIBRARY_DIR
            )
            structured_work_orders = _load_qdrant_structured_records()
            bundle = {
                "vectors": np.load(required["vectors"]),
                "metadata": pd.read_csv(required["metadata"]),
                "node_index": joblib.load(required["node_index"]),
                "segments": joblib.load(required["segments"]),
                "node_counts": {},
                "raw_work_order_records": raw_work_orders,
                "work_order_source_records": source_work_orders,
                "structured_work_order_records": structured_work_orders,
                "manifest": {
                    "library_version": f"local:{CASE_LIBRARY_DIR}",
                    "display_name_map": _load_qdrant_display_name_map(),
                    "gateway_metrics": _load_qdrant_gateway_metrics(),
                },
            }
            if len(bundle["metadata"]) != len(bundle["vectors"]):
                raise ValueError("Local metadata row count does not match vectors")
            if len(bundle["node_index"]) != bundle["vectors"].shape[1]:
                raise ValueError("Local node_index size does not match vector dimension")
        else:
            bundle = load_complete_library_from_qdrant(
                qdrant_url=QDRANT_URL,
                collection_name=QDRANT_COLLECTION,
            )
        vectors = np.asarray(bundle["vectors"], dtype=float).copy()
        metadata = bundle["metadata"].copy()
        node_index = dict(bundle["node_index"])
        if EXCLUDE_SELECTED_CODES_FROM_SIMILARITY:
            excluded_cols = excluded_node_columns(node_index)
            vectors[:, excluded_cols] = 0.0
            vectors = normalize_vector_rows(vectors)
        bundle["vectors"] = vectors
        bundle["metadata"] = metadata
        bundle["node_index"] = node_index
        _CACHE["vector_backend"] = CASE_LIBRARY_BACKEND
        _CACHE["qdrant_manifest"] = dict(bundle.get("manifest") or {})
        _CACHE["qdrant_bundle"] = bundle
    return _CACHE["qdrant_bundle"]


def load_library():
    bundle = _load_qdrant_bundle()
    return bundle["vectors"], bundle["metadata"], bundle["node_index"]


def load_segments() -> Dict[str, Any]:
    return _load_qdrant_bundle()["segments"]


def gateway_metric_codes_for_car(car_no: Any) -> List[str]:
    """Select metrics that can affect the current case-library vector."""
    cache_key = f"gateway_metric_codes:{normalize_car_no(car_no)}"
    if cache_key not in _CACHE:
        _, _, node_index = load_library()
        node_names = tuple(str(node) for node in node_index)
        required_codes = {
            normalize_code(dim)
            for dim in (
                get_vcb_target_dims_for_car(car_no)
                | get_ramping_finish_dims_for_car(car_no)
            )
        }
        selected: List[str] = []
        for metric in gateway_metric_definitions():
            code = str(metric["code"])
            if code in required_codes or any(
                node.startswith(f"dir_{code}_") for node in node_names
            ):
                selected.append(code)
        if not selected:
            raise RuntimeError("案例库中没有可用于网关查询的相关测点")
        _CACHE[cache_key] = selected
    return list(_CACHE[cache_key])


def gateway_metric_definitions() -> List[Dict[str, str]]:
    raw = _load_qdrant_bundle().get("manifest", {}).get("gateway_metrics", [])
    metrics = [
        {
            "code": str(item.get("code", "")).strip(),
            "equipUnit": str(item.get("equipUnit", item.get("coach", ""))).strip(),
        }
        for item in raw
        if isinstance(item, Mapping) and str(item.get("code", "")).strip()
    ] if isinstance(raw, list) else []
    if not metrics:
        raise RuntimeError("Qdrant 案例库 manifest 中没有 gateway_metrics")
    return metrics


# ============================================================
# 维度变化次数统计
# ============================================================


def strip_dim(dim: str) -> str:
    """去掉 dir_ 前缀，显示更干净的变量 code。"""
    s = str(dim)
    return s[4:] if s.startswith("dir_") else s


def is_excluded_dim(dim: Any) -> bool:
    return (
        EXCLUDE_SELECTED_CODES_FROM_SIMILARITY
        and normalize_code(strip_dim(str(dim))) in EXCLUDED_SIMILARITY_CODES
    )


def excluded_node_columns(node_index: Dict[str, int]) -> List[int]:
    if not EXCLUDE_SELECTED_CODES_FROM_SIMILARITY:
        return []
    prefixes = tuple(f"dir_{code}_" for code in EXCLUDED_SIMILARITY_CODES)
    return [
        int(pos)
        for node, pos in node_index.items()
        if str(node).startswith(prefixes) or str(node).startswith(EXCLUDED_SIMILARITY_NODE_PREFIXES)
    ]


def normalize_vector_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


def apply_similarity_exclusions(vector: np.ndarray, node_index: Dict[str, int]) -> np.ndarray:
    out = np.asarray(vector, dtype=float).copy()
    excluded_cols = excluded_node_columns(node_index)
    out[excluded_cols] = 0.0
    norm = float(np.linalg.norm(out))
    return out / norm if norm > 0 else out


def load_name_map() -> Dict[str, str]:
    """Load code display labels from the Qdrant library manifest."""
    raw = _load_qdrant_bundle().get("manifest", {}).get("display_name_map", {})
    return {
        normalize_code(code): str(description)
        for code, description in raw.items()
        if str(code).strip() and str(description).strip()
    } if isinstance(raw, Mapping) else {}


NAME_MAP = load_name_map()

TARGET_DIM_CODES = {
    "target_ramping_finish_911": "911_5_1_02_sd",
    "target_ramping_finish_943": "943_5_1_02_sd",
    "target_vcb_904": "904_2_1_02_sd",
    "target_vcb_936": "936_2_1_02_sd",
}


def vcb_display_dims_for_car(car_no: Any = None) -> List[str]:
    """Return real VCB closed dimensions for display only."""
    return sorted(
        dim for dim in get_vcb_target_dims_for_car(car_no)
        if strip_dim(dim).startswith(("904_2_1_", "936_2_1_"))
    )


def dim_display(dim: str) -> str:
    """原始数据图显示变量原名；无映射时保留 code。"""
    code = TARGET_DIM_CODES.get(str(dim), normalize_code(strip_dim(dim)))
    return NAME_MAP.get(code, code)


def fmt_value(value: Any) -> str:
    """把 node 的前后值压成易读文本。"""
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
    except Exception:
        return str(value)


def wrap_label_html(value: str, width: int = 24) -> str:
    """给 Plotly 纵轴上的长变量名换行。"""
    lines: List[str] = []
    current = ""
    for word in str(value).split():
        candidate = (current + " " + word).strip() if current else word
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > width:
            lines.append(word[:width])
            word = word[width:]
        current = word
    if current:
        lines.append(current)
    return "<br>".join(lines)


TARGET_VCB_DISPLAY_DIMS = {"target_vcb_904", "target_vcb_936"}
RAMPING_DIM_BY_VCB_DIM = {
    "target_vcb_904": "target_ramping_finish_911",
    "target_vcb_936": "target_ramping_finish_943",
}


def parse_node_key(key: Any) -> tuple[str, str, str] | None:
    text = str(key)
    known_dims = sorted(
        list(TARGET_DIM_CODES.keys()) + list(TARGET_VCB_DISPLAY_DIMS),
        key=len,
        reverse=True,
    )
    for dim in known_dims:
        prefix = f"{dim}_"
        if text.startswith(prefix):
            rest = text[len(prefix):]
            if "_" not in rest:
                return None
            prev, cur = rest.rsplit("_", 1)
            return dim, prev, cur
    return None


def is_vcb_display_dim(dim: Any) -> bool:
    return str(dim) in TARGET_VCB_DISPLAY_DIMS


def is_vcb_open_node(key: Any) -> bool:
    parsed = parse_node_key(key)
    if not parsed:
        return False
    dim, prev, cur = parsed
    return is_vcb_display_dim(dim) and fmt_value(prev) == "1" and fmt_value(cur) == "0"


def is_ramping_status_node_for_vcb(key: Any, vcb_dim: Any) -> bool:
    parsed = parse_node_key(key)
    if not parsed:
        return False
    dim, prev, _cur = parsed
    return dim == RAMPING_DIM_BY_VCB_DIM.get(str(vcb_dim)) and prev == "ramping_finish_status"


def segment_counts(seg, granularity: str = "dim", car_no: Any = None, include_vcb_display: bool = False) -> Dict[str, int]:
    """统计一个 segment 里每个 key 出现几次 = 它「变了几次」。

    granularity:
        "dim"  -> key = 维度 rcd[1]                         （把同维度所有前后值合并看）
        "node" -> key = 维度_前值_后值（rcd[1]_rcd[3]_rcd[5]），与打分向量口径完全一致

    只数 len(rcd) >= 6 的有效记录，与 segment_to_node_counts 同源。
    """
    counts: Dict[str, int] = {}
    if not seg:
        return counts
    for rcd in seg:
        if rcd is None or len(rcd) < 6:
            continue
        dim = target_car_normalized_dim(rcd[1], car_no=car_no)
        if dim is None:
            continue
        if is_excluded_dim(rcd[1]) and not (include_vcb_display and is_vcb_display_dim(dim)):
            continue
        node = str(dim) + "_" + str(rcd[3]) + "_" + str(rcd[5])
        if (
            EXCLUDE_SELECTED_CODES_FROM_SIMILARITY
            and node.startswith(EXCLUDED_SIMILARITY_NODE_PREFIXES)
            and not include_vcb_display
        ):
            continue
        if granularity == "node":
            key = node
        else:
            key = str(dim)
        counts[key] = counts.get(key, 0) + 1
    return counts


def segment_nodes(seg, car_no: Any = None, include_vcb_display: bool = False) -> Dict[str, str]:
    """一个 segment 的去重 node -> 维度 映射（node = 维度_前值_后值）。"""
    out: Dict[str, str] = {}
    if not seg:
        return out
    for rcd in seg:
        if rcd is None or len(rcd) < 6:
            continue
        dim = target_car_normalized_dim(rcd[1], car_no=car_no)
        if dim is None:
            continue
        if is_excluded_dim(rcd[1]) and not (include_vcb_display and is_vcb_display_dim(dim)):
            continue
        node = str(dim) + "_" + str(rcd[3]) + "_" + str(rcd[5])
        if (
            EXCLUDE_SELECTED_CODES_FROM_SIMILARITY
            and node.startswith(EXCLUDED_SIMILARITY_NODE_PREFIXES)
            and not include_vcb_display
        ):
            continue
        out[node] = str(dim)
    return out


def row_contributions(seg_r, vec_r, query_vec, node_index, granularity: str, car_no: Any = None) -> Dict[str, float]:
    """该工单各列对它与新故障余弦相似度的贡献：contrib = 新故障权重 × 工单权重。

    向量均已归一化，所以某 node 的贡献 = query_vec[n] * vec_r[n]；
    所有列贡献相加 == 该工单的相似度（node 粒度逐 node；dim 粒度按维度归并后同样相等）。
    """
    out: Dict[str, float] = {}
    if vec_r is None or query_vec is None or not node_index:
        return out
    for node, dim in segment_nodes(seg_r, car_no=car_no).items():
        pos = node_index.get(node)
        if pos is None:
            continue
        c = float(query_vec[pos]) * float(vec_r[pos])
        if c == 0.0:
            continue
        key = node if granularity == "node" else dim
        out[key] = out.get(key, 0.0) + c
    return out


# ============================================================
# 查询：生成新故障向量 + 找 Top5 + 各自维度变化次数
# ============================================================


def compute_query(
    train: str,
    car_no: str,
    fault_time: str,
    time_is_cst: bool,
    top_k: int,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    fault_code: str = "",
    data_dir: str | Path = DATA_DIR,
    download_info: Any = None,
) -> Dict[str, Any]:
    vectors, metadata, node_index = load_library()
    segments = load_segments()

    # 实时下载只包含故障前后 GATEWAY_* 分钟；分析窗口必须与下载窗口
    # 保持一致，否则靠近零点的查询仍会按历史默认的 30 分钟寻找相邻日期文件。
    if download_info is None:
        read_before_min = float(ANALYZE_BEFORE_MIN)
        read_after_min = float(ANALYZE_AFTER_MIN)
    elif isinstance(download_info, Mapping):
        read_before_min = float(download_info.get("before_min", GATEWAY_BEFORE_MIN))
        read_after_min = float(download_info.get("after_min", GATEWAY_AFTER_MIN))
    else:
        read_before_min = float(getattr(download_info, "before_min", GATEWAY_BEFORE_MIN))
        read_after_min = float(getattr(download_info, "after_min", GATEWAY_AFTER_MIN))

    result = get_local_case_result(
        train=train,
        car_no=car_no,
        fault_time=fault_time,
        data_dir=data_dir,
        time_is_cst=time_is_cst,
        read_before_min=read_before_min,
        read_after_min=read_after_min,
        analyze_before_min=read_before_min,
        analyze_after_min=read_after_min,
    )
    status = result.get("status", "unknown")
    if status != "ok":
        return {
            "ok": False,
            "message": (
                f"当前下载区间内没有可用的故障 segment（status={status}）。"
                "请调大“故障前/故障后”时间后重新查询。"
            ),
        }

    node_counts = result["node_counts"]
    query_vec, unknown_nodes = vectorize_node_counts(node_counts, node_index)
    query_vec = apply_similarity_exclusions(query_vec, node_index)
    if np.linalg.norm(query_vec) == 0:
        return {"ok": False, "message": "新故障的节点都不在历史 node_index 中，无法相似度检索。"}

    new_seg = result.get("main_segment")

    # 查询列表也使用与工单热力图一致的最终相似度口径：
    # raw = 原始相似度；detail = 去掉 VCB/RF 后的相似度；display = 最终用于排序和阈值过滤的相似度。
    raw_sims = np.clip(vectors @ query_vec, 0.0, 1.0)
    detail_cols = _core_detail_columns(node_index)
    detail_vectors = vectors.copy()
    if detail_cols:
        detail_vectors[:, detail_cols] = 0.0
    detail_vectors = normalize_vector_rows(detail_vectors)
    query_detail_vec = _zero_and_normalize(query_vec, detail_cols)
    detail_sims = np.clip(detail_vectors @ query_detail_vec, 0.0, 1.0)

    query_rf_label = _segment_rf_label(new_seg, car_no)
    case_rf_labels: List[str] = []
    for _, meta in metadata.iterrows():
        case_rf_labels.append(
            _segment_rf_label(
                segments.get(str(meta.get("case_id", ""))),
                meta.get("car_no", ""),
            )
        )

    query_active_detail = query_detail_vec > 0
    history_active_detail = detail_vectors > 0
    shared_non_core_counts = np.logical_and(history_active_detail, query_active_detail).sum(axis=1).astype(int)
    sims = np.zeros_like(raw_sims, dtype=float)
    for idx, raw_similarity in enumerate(raw_sims):
        detail_similarity = float(detail_sims[idx])
        shared_non_core = int(shared_non_core_counts[idx])
        history_rf_label = case_rf_labels[idx]
        sims[idx] = _final_similarity(
            float(raw_similarity),
            detail_similarity,
            shared_non_core,
            query_rf_label,
            history_rf_label,
        )

    order = np.argsort(-sims)

    # 工单粒度去重：同一 work_order_no 只保留相似度最高的那条 case
    wos = metadata["work_order_no"].astype(str).tolist()
    top_idx: List[int] = []
    seen_wo: set = set()
    for i in order:
        i = int(i)
        if float(sims[i]) < similarity_threshold:
            continue
        wo = wos[i]
        if wo in seen_wo:
            continue
        seen_wo.add(wo)
        top_idx.append(i)
        if len(top_idx) >= top_k:
            break

    # 各案例的 segment（懒到画图时再按所选粒度统计变化次数）
    top_segs = [segments.get(str(metadata.iloc[idx].get("case_id", ""))) for idx in top_idx]

    return {
        "ok": True,
        "metadata": metadata,
        "sims": sims,
        "raw_sims": raw_sims,
        "detail_sims": detail_sims,
        "shared_non_core_counts": shared_non_core_counts,
        "query_rf_label": query_rf_label,
        "case_rf_labels": case_rf_labels,
        "top_idx": top_idx,
        "new_seg": new_seg,
        "top_segs": top_segs,
        "query_vec": query_vec,
        "node_index": node_index,
        "top_vecs": [vectors[i] for i in top_idx],
        "node_count": len(node_counts),
        "excluded_query_node_count": sum(
            1 for node in node_counts
            if str(node).startswith(tuple(f"dir_{code}_" for code in EXCLUDED_SIMILARITY_CODES))
            or str(node).startswith(EXCLUDED_SIMILARITY_NODE_PREFIXES)
        ) if EXCLUDE_SELECTED_CODES_FROM_SIMILARITY else 0,
        "unknown_node_count": len(unknown_nodes),
        "fault_time_utc": str(result.get("fault_time_utc", "")),
        "train": normalize_train(train),
        "car_no": normalize_car_no(car_no),
        "fault_time": fault_time,
        "fault_code": str(fault_code or "").strip(),
        "data_dir": str(data_dir),
        "download_info": download_info,
        "window_before_min": read_before_min,
        "window_after_min": read_after_min,
        "similarity_threshold": float(similarity_threshold),
        "qualified_work_order_count": len(seen_wo),
    }


def make_top_rows(q: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = q["metadata"]
    sims = q["sims"]
    rows: List[Dict[str, Any]] = []
    for rank, idx in enumerate(q["top_idx"]):
        m = metadata.iloc[idx]
        rows.append(
            {
                "排名": rank + 1,
                "工单号": m.get("work_order_no", ""),
                "车号": m.get("train", ""),
                "故障时间": m.get("fault_time_raw", ""),
                "相似度": round(float(sims[idx]), 4),
                "变化维度数": len(segment_counts(q["top_segs"][rank], "dim")),
                "备注": m.get("remark", ""),
            }
        )
    return rows


def build_fault_reference_report(q: Dict[str, Any]) -> Dict[str, Any]:
    """把查询得到的工单级 Top 结果交给 fault_extractor 生成结构化报告。"""
    if not q or not q.get("top_idx"):
        raise ValueError("当前没有达到最低相似度阈值的工单，无法生成报告。")
    similarities: Dict[str, float] = {}
    metadata = q["metadata"]
    sims = q["sims"]
    for idx in q["top_idx"]:
        work_order = str(metadata.iloc[idx].get("work_order_no", "")).strip()
        if work_order:
            similarities[work_order] = max(similarities.get(work_order, 0.0), float(sims[idx]))

    raw_work_order_records = load_work_order_records()
    structured_records = load_structured_work_order_records()
    raw_source_fields = (
        "Description",
        "Long Description",
        "Failure Long Description",
        "Problem Desc",
        "Cause Desc",
        "Remedy Desc",
        "Failure Component",
        "Component Desc",
        "Replaced Component",
        "Rep.Comp Desc",
    )
    raw_source_payload = {
        work_order: {
            field: str(raw_work_order_records.get(work_order, {}).get(field, ""))
            for field in raw_source_fields
        }
        for work_order in similarities
    }
    raw_source_revision = hashlib.sha256(
        json.dumps(
            raw_source_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    reference_revision = str(
        _CACHE.get("qdrant_manifest", {}).get("library_version", "unknown")
    )
    cache_key = (
        reference_revision,
        raw_source_revision,
        tuple(sorted((work_order, round(similarity, 8)) for work_order, similarity in similarities.items())),
    )
    with _REPORT_CACHE_LOCK:
        cached = _REPORT_CACHE.get(cache_key)
        if cached is not None:
            _REPORT_CACHE.move_to_end(cache_key)
            return cached

    # report_syn 使用同目录的 llm_api_tool；将完整 fault_extractor 项目放在导入路径最前。
    extractor_path = str(FAULT_EXTRACTOR_DIR)
    if extractor_path not in sys.path:
        sys.path.insert(0, extractor_path)
    from report_syn import report_synthesis_details

    report = report_synthesis_details(
        similarities,
        raw_work_order_records=raw_work_order_records,
        reference_data=structured_records,
    )
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE[cache_key] = report
        _REPORT_CACHE.move_to_end(cache_key)
        while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
            _REPORT_CACHE.popitem(last=False)
    return report


def report_generation_spinner():
    """Visible processing stages shown while the blocking LLM call runs."""
    stages = [
        "整理相似工单与结构化信息",
        "汇总故障现象和关键证据",
        "归纳可能原因与重点部件",
        "生成排查顺序与维修建议",
        "校验结论与历史工单依据",
    ]
    return html.Div(
        className="report-generation-progress",
        role="status",
        children=[
            html.Div(className="report-generation-orbit", children=[
                html.Span(className="report-generation-core"),
                html.Span(className="report-generation-ring"),
            ]),
            html.Div(className="report-generation-copy", children=[
                html.Div("正在生成故障维修参考报告", className="report-generation-title"),
                html.Div(
                    [html.Span(stage, className="report-generation-stage") for stage in stages],
                    className="report-generation-stages",
                ),
                html.Div(className="report-generation-track", children=[
                    html.Span(className="report-generation-bar")
                ]),
                html.Div(
                    "处理阶段会循环显示，报告完成后自动替换。",
                    className="report-generation-note",
                ),
            ]),
        ],
    )


def _report_fact_items(facts: Mapping[str, Any], key: str, limit: int = 6) -> List[str]:
    items: List[str] = []
    for value in list(facts.get(key, []) or [])[:limit]:
        text = value.get("item") if isinstance(value, Mapping) else value
        text = str(text or "").strip()
        if text and text.lower() not in {"n/a", "na", "none"}:
            items.append(text)
    return items


def _report_priority_components(narrative: Mapping[str, Any], limit: int = 6) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(narrative.get("priority_components", []) or []):
        if isinstance(item, Mapping):
            component = str(item.get("component") or "").strip()
            reason = str(item.get("reason") or "").strip()
            work_orders = [
                str(value).strip()
                for value in list(item.get("source_work_orders", []) or [])
                if str(value).strip()
            ]
        else:
            component = str(item or "").strip()
            reason = ""
            work_orders = []
        if not component or component.lower() in seen:
            continue
        seen.add(component.lower())
        output.append(
            {
                "component": component,
                "reason": reason,
                "source_work_orders": work_orders,
            }
        )
        if len(output) >= limit:
            break
    return output


def _report_list(items: Sequence[Any], class_name: str, ordered: bool = False):
    cleaned = [str(item).strip() for item in items if str(item or "").strip()]
    tag = html.Ol if ordered else html.Ul
    return tag([html.Li(item) for item in cleaned], className=class_name)


def render_fault_reference_report(details: Mapping[str, Any], compact: bool = False):
    """将报告渲染成结论优先的检修决策卡片。"""
    cases = list(details.get("cases", []) or [])
    if not cases:
        return html.Div("参考数据中未找到匹配工单，暂时无法生成报告。", className="report-empty")

    facts = dict(details.get("synthesis_facts", {}) or {})
    narrative = dict(details.get("narrative", {}) or {})
    priority_details = _report_priority_components(narrative, 6)
    priority_names = [item["component"] for item in priority_details]
    physical_cause_focus = _report_fact_items(facts, "top_physical_cause_components", 4)
    treatment_focus = _report_fact_items(facts, "top_treatment_components", 6)
    cause_focus = _report_fact_items(facts, "top_cause_components", 4)
    component_focus = list(dict.fromkeys(
        priority_names
        + treatment_focus
        + _report_fact_items(facts, "top_components", 8)
    ))[:10]
    likely_locations = (
        priority_names
        or physical_cause_focus
        or treatment_focus[:4]
        or cause_focus
        or component_focus[:4]
    )
    primary_location = likely_locations[0] if likely_locations else "VCB 相关控制链路"
    primary_reason = priority_details[0].get("reason", "") if priority_details else ""
    top_case = cases[0]
    top_similarity = float(top_case.get("similarity") or 0.0)

    if top_similarity >= 0.85 and len(cases) >= 2:
        evidence_level, evidence_class = "较强参考", "strong"
    elif top_similarity >= 0.7:
        evidence_level, evidence_class = "中等参考", "medium"
    else:
        evidence_level, evidence_class = "有限参考", "limited"

    inspection_steps = list(narrative.get("recommended_steps", []) or [])[:6]
    historical_actions = _report_fact_items(facts, "top_actions", 5)
    repair_steps = [
        f"检查结果与历史故障模式一致后，再参考执行：{action}"
        for action in historical_actions
    ]
    if not repair_steps:
        repair_steps = ["先完成回路、信号和功能测试；仅在证据明确后进行部件修复或更换。"]
    repair_steps.append("处置后复测相关功能并观察是否复现，确认正常后再闭环工单。")

    conclusion_text = (
        (
            f"{len(cases)} 个匹配历史工单的 Long Description 显示，"
            f"当前最应优先验证的是 {primary_location}：{primary_reason}"
        )
        if primary_reason
        else (
            f"{len(cases)} 个匹配历史工单中，当前最应优先验证的是 {primary_location}。"
        )
    ) + "该判断用于安排排查顺序，不等同于根因已确认。"

    evidence_rows = []
    for case in cases[:6]:
        cause = str(case.get("cause") or "未记录明确原因")
        if case.get("cause_is_inferred"):
            cause += "（历史推断）"
        evidence_rows.append(html.Div([
            html.Div([
                html.Span(f"工单 {case.get('work_order', '')}", className="report-evidence-wo"),
                html.Span(f"{float(case.get('similarity') or 0):.1%}", className="report-evidence-sim"),
            ], className="report-evidence-head"),
            html.Div(str(case.get("fault") or "未记录故障现象"), className="report-evidence-fault"),
            html.Div(f"原因 / 判断：{cause}", className="report-evidence-meta"),
            html.Div(f"历史处置：{case.get('action') or '未记录'}", className="report-evidence-meta"),
            html.Div(
                f"Long Description：{str(case.get('source_long_description') or '未提供')[:700]}",
                className="report-evidence-meta",
            ),
        ], className="report-evidence-row"))

    caution_items = list(narrative.get("cautions", []) or [])
    diagnosis_card = html.Section([
            html.Div([
                html.Div("诊断结论", className="report-kicker"),
                html.Div(f"优先排查：{primary_location}", className="report-conclusion-title"),
                html.P(conclusion_text, className="report-conclusion-text"),
                html.Div([
                    html.Span(f"证据强度 · {evidence_level}", className=f"report-confidence {evidence_class}"),
                    html.Span(f"最高相似度 {top_similarity:.1%}", className="report-stat"),
                    html.Span(f"参考工单 {len(cases)} 个", className="report-stat"),
                ], className="report-stats"),
            ], className="report-conclusion-main"),
            html.Div([
                html.Div("可能故障位置", className="report-focus-label"),
                html.Div([
                    html.Span(location, className="report-focus-chip")
                    for location in (likely_locations or [primary_location])
                ], className="report-focus-chips"),
            ], className="report-focus-box"),
        ], className="report-hero")

    if compact:
        return html.Div([diagnosis_card], className="fault-report fault-report-compact")

    return html.Div([
        diagnosis_card,

        html.Div([
            html.Section([
                html.Div([html.Span("01", className="report-step-no"), html.H4("建议排查顺序")], className="report-card-head"),
                _report_list(inspection_steps, "report-action-list", ordered=True),
            ], className="report-action-card inspect"),
            html.Section([
                html.Div([html.Span("02", className="report-step-no"), html.H4("建议维修与验证")], className="report-card-head"),
                _report_list(repair_steps, "report-action-list repair"),
            ], className="report-action-card repair"),
        ], className="report-action-grid"),

        html.Div([
            html.Div("重点关注部件", className="report-minor-title"),
            html.Div([html.Span(item, className="report-component-chip") for item in component_focus], className="report-component-list"),
        ], className="report-components") if component_focus else None,

        html.Details([
            html.Summary([html.Span("查看参考依据与历史工单详情"), html.Span(f"{len(cases)} 条", className="report-count")]),
            html.Div(evidence_rows, className="report-evidence-list"),
        ], className="report-evidence"),

        html.Div([
            html.Div("使用提示", className="report-note-title"),
            _report_list(caution_items or ["相似度仅用于历史案例排序，最终根因须以现场检查和测试结果为准。"], "report-caution-list"),
        ], className="report-caution"),
    ], className="fault-report fault-report-complete")


# ============================================================
# 维度变化热力图
# ============================================================


def _placeholder(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, font=dict(size=15), xref="paper", yref="paper", x=0.5, y=0.5)
    fig.update_layout(height=420, template="plotly_white")
    return fig


def build_heatmap(q: Dict[str, Any], only_new_dims: bool = True, granularity: str = "node",
                  value_mode: str = "count") -> go.Figure:
    """行=新故障+Top5，列=维度或 node。

    value_mode="count"   格子 = 变了几次（0=没变，留白）
    value_mode="contrib" 格子 = 该列对该工单相似度的贡献分（×100），各列相加 = 该工单相似度
    granularity="dim"/"node" 控制列粒度（node 与打分向量同源）。
    两种 value_mode 下列顺序一致，方便对照"变了几次"与"贡献多少分"。
    """
    metadata = q["metadata"]
    sims = q["sims"]
    top_idx = q["top_idx"]
    top_car_nos = [metadata.iloc[idx].get("car_no", "") for idx in top_idx]
    new_counts = segment_counts(q["new_seg"], granularity, q.get("car_no"), include_vcb_display=True)
    top_counts = [
        segment_counts(s, granularity, top_car_nos[i], include_vcb_display=True)
        for i, s in enumerate(q["top_segs"])
    ]
    top_contribs = [
        row_contributions(q["top_segs"][r], q["top_vecs"][r], q.get("query_vec"),
                          q.get("node_index"), granularity, top_car_nos[r])
        for r in range(len(top_idx))
    ]
    unit = "node（维度+前后值）" if granularity == "node" else "维度"
    is_contrib = (value_mode == "contrib")

    # 行（顺序与 heatmap_rows 一致）+ 每行的计数 / 贡献
    row_labels, _ = heatmap_rows(q)
    row_counts: List[Dict[str, int]] = [new_counts] + list(top_counts)
    row_contribs: List[Dict[str, float]] = [{}] + list(top_contribs)

    # 列 = 各行并集（按计数口径，保证两种 value_mode 列一致）
    all_keys: set = set()
    for c in row_counts:
        all_keys.update(c.keys())
    if only_new_dims:
        all_keys &= set(new_counts.keys())
    if not all_keys:
        return _placeholder("这些案例没有可显示的变化。")

    def sort_key(d):
        active = sum(1 for c in row_counts if c.get(d, 0) > 0)
        return (-active, -new_counts.get(d, 0), -sum(c.get(d, 0) for c in row_counts), str(d))

    sorted_keys = sorted(all_keys, key=sort_key)
    if granularity == "node":
        paired_ramping_keys: set[str] = set()
        for key in sorted_keys:
            parsed = parse_node_key(key)
            if parsed and is_vcb_open_node(key):
                vcb_dim, _prev, _cur = parsed
                paired_ramping_keys.update(
                    str(candidate)
                    for candidate in sorted_keys
                    if is_ramping_status_node_for_vcb(candidate, vcb_dim)
                )

        keys = []
        used_keys: set[str] = set()
        vcb_first_keys = [str(key) for key in sorted_keys if is_vcb_open_node(key)]
        remaining_sorted_keys = vcb_first_keys + [
            str(key) for key in sorted_keys if str(key) not in set(vcb_first_keys)
        ]
        for key in remaining_sorted_keys:
            key = str(key)
            if key in used_keys or key in paired_ramping_keys:
                continue
            keys.append(key)
            used_keys.add(key)
            if is_vcb_open_node(key):
                parsed = parse_node_key(key)
                vcb_dim = parsed[0] if parsed else ""
                ramping_keys = [
                    str(candidate)
                    for candidate in sorted_keys
                    if str(candidate) not in used_keys and is_ramping_status_node_for_vcb(candidate, vcb_dim)
                ]
                ramping_keys = sorted(
                    ramping_keys,
                    key=lambda candidate: (
                        parse_node_key(candidate)[2] if parse_node_key(candidate) else "",
                        str(candidate),
                    ),
                )
                keys.extend(ramping_keys)
                used_keys.update(ramping_keys)
    else:
        keys = sorted_keys
    all_segments = [q["new_seg"]] + list(q["top_segs"])
    all_car_nos = [q.get("car_no", "")] + list(top_car_nos)
    key_to_dim: Dict[str, str] = {}
    key_to_label: Dict[str, str] = {}
    if granularity == "node":
        for seg, car_no in zip(all_segments, all_car_nos):
            for rcd in (seg or []):
                if rcd is None or len(rcd) < 6:
                    continue
                dim = target_car_normalized_dim(rcd[1], car_no=car_no)
                if dim is None:
                    continue
                node = dim + "_" + str(rcd[3]) + "_" + str(rcd[5])
                key_to_dim[node] = dim
                if str(rcd[3]) == "ramping_finish_status":
                    side = "TC1" if str(dim) == "target_ramping_finish_911" else "TC2"
                    key_to_label[node] = f"↳ RF {side}={fmt_value(rcd[5])}"
                else:
                    key_to_label[node] = f"{dim_display(dim)}  {fmt_value(rcd[3])}→{fmt_value(rcd[5])}"
    else:
        for key in keys:
            key_to_dim[str(key)] = str(key)
            key_to_label[str(key)] = dim_display(str(key))
    col_labels = [wrap_label_html(key_to_label.get(str(key), dim_display(str(key))), 18) for key in keys]
    nrows, ncols = len(row_labels), len(keys)

    # 计数矩阵 + 贡献矩阵
    cnt = np.zeros((nrows, ncols))
    con = np.zeros((nrows, ncols))
    for ri in range(nrows):
        for ci, d in enumerate(keys):
            cnt[ri, ci] = row_counts[ri].get(d, 0)
            con[ri, ci] = row_contribs[ri].get(d, 0.0)

    if is_contrib:
        z = con * 100.0          # ×100 便于读数
        z[0, :] = np.nan         # 新故障行不参与贡献
        colorbar_title = "相似度<br>贡献×100"
        floor = 0.01
    else:
        z = cnt.copy()
        colorbar_title = "变化<br>次数"
        floor = 1.0

    posz = z[np.isfinite(z) & (z > 0)]
    zmax = float(np.percentile(posz, 95)) if posz.size else floor
    zmax = max(zmax, floor)

    # 悬停：同时携带说明文字与真实 dim，供浏览器端无刷新高亮原始时序。
    hover_text = np.empty((nrows, ncols), dtype=object)
    cust = np.empty((nrows, ncols), dtype=object)
    for ri in range(nrows):
        for ci in range(ncols):
            contrib_txt = "—（新故障本身）" if ri == 0 else f"+{con[ri, ci] * 100:.2f}（×100）"
            hover_text[ri, ci] = (f"{row_labels[ri]}<br>{unit} {col_labels[ci]}<br>"
                                  f"变化次数 {int(cnt[ri, ci])}<br>相似度贡献 {contrib_txt}")
            cust[ri, ci] = key_to_dim.get(str(keys[ci]), str(keys[ci]))

    fig = go.Figure(
        go.Heatmap(
            z=z, x=col_labels, y=row_labels, zmin=0.0, zmax=zmax, colorscale=HEAT_COLORSCALE,
            xgap=2, ygap=2, text=hover_text, customdata=cust, hovertemplate="%{text}<extra></extra>",
            colorbar=dict(
                title=dict(text=colorbar_title, side="right", font=dict(size=12)),
                thickness=17, len=1.0, y=0.5, x=1.025, xpad=10,
                tickfont=dict(size=11), outlinewidth=0,
            ),
        )
    )

    # 非零格子标注数值；深色格白字、浅色格深字
    annotations = []
    for ri in range(nrows):
        for ci in range(ncols):
            if is_contrib:
                if ri == 0:
                    continue
                val = con[ri, ci] * 100.0
                if val <= 0:
                    continue
                txt = f"{val:.1f}"
            else:
                v = int(cnt[ri, ci])
                if v <= 0:
                    continue
                txt = str(v)
            zz = z[ri, ci]
            tcol = "white" if (np.isfinite(zz) and zz / zmax > 0.55) else "#333333"
            annotations.append(
                dict(x=col_labels[ci], y=row_labels[ri], text=txt,
                     showarrow=False, font=dict(size=12, color=tcol))
            )

    cell_txt = "相似度贡献×100（一行各列相加=该行相似度）" if is_contrib else "变了几次（0=没变，留白）"
    fig.update_layout(
        title=dict(text="<b>变化模式对比</b>",
                   font=dict(size=16), x=0.02, xanchor="left"),
        annotations=annotations,
        xaxis=dict(tickangle=-48, tickfont=dict(size=12), side="bottom", title=None,
                   showgrid=False, zeroline=False),
        yaxis=dict(autorange="reversed", tickfont=dict(size=13)),
        margin=dict(l=250, r=105, t=112, b=145),
        template="plotly_white",
        plot_bgcolor="#f8fafc",
        paper_bgcolor="#ffffff",
        height=max(480, 180 + nrows * 65),
        width=max(880, 240 + ncols * 46),
    )

    # 新故障行（第 1 行）与 Top5 行之间画一条分隔线
    fig.add_shape(
        type="line", xref="paper", yref="y", x0=0.0, x1=1.0, y0=0.5, y1=0.5,
        line=dict(color=NEW_COLOR, width=2, dash="dot"),
    )
    return fig


# ============================================================
# 原始数据追溯：单变量时序 + 全维度时序
# ============================================================

TS_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
    "#e377c2", "#17becf", "#bcbd22", "#393b79", "#637939", "#8c6d31",
    "#843c39", "#7b4173", "#5254a3", "#8ca252", "#bd9e39", "#ad494a",
]


def heatmap_rows(q: Dict[str, Any]):
    """与 build_heatmap 完全一致的行顺序，返回 (labels, segments)。"""
    metadata = q["metadata"]
    sims = q["sims"]
    top_idx = q["top_idx"]
    labels = [f"★ 新故障 {q['train']}"]
    segs = [q["new_seg"]]
    for r, idx in enumerate(top_idx):
        m = metadata.iloc[idx]
        labels.append(f"#{r + 1} {str(m.get('work_order_no', ''))}｜{str(m.get('train', ''))}｜相似度 {float(sims[idx]):.3f}")
        segs.append(q["top_segs"][r])
    return labels, segs


# ---- 原始数据按需读取（点击时读 01_数据整理，按 train+时刻缓存） ----
_RAW_CACHE: Dict[Any, Any] = {}
_RAW_CACHE_LOCK = RLock()


def get_raw_df(train, fault_time_utc, file_partition_time=None, data_dir=DATA_DIR) -> pd.DataFrame:
    train = normalize_train(train)
    futc = pd.Timestamp(fault_time_utc)
    partition_time = pd.Timestamp(file_partition_time) if file_partition_time is not None else futc
    data_dir = str(data_dir)
    key = (data_dir, train, futc.isoformat(), partition_time.isoformat())
    if key not in _RAW_CACHE:
        df, _sel = read_local_raw_data(
            data_dir, train, futc, file_partition_time=partition_time
        )
        if df is not None and len(df) > 0:
            df = df.copy()
            df["code_norm"] = df["code"].astype(str).map(normalize_code)
        _RAW_CACHE[key] = df
    return _RAW_CACHE[key]


def get_historical_raw_df(
    train,
    fault_time_utc,
    file_partition_time,
    codes: Sequence[str],
    before_minutes: float,
    after_minutes: float,
    event_code: Any = HISTORICAL_EVENT_CODE,
) -> pd.DataFrame:
    """Read a historical case directly from Iceberg and cache the small window."""
    train = normalize_train(train)
    futc = pd.Timestamp(fault_time_utc)
    partition_time = pd.Timestamp(file_partition_time)
    code_key = tuple(sorted({str(code) for code in codes if str(code).strip()}))
    key = (
        "iceberg",
        train,
        futc.isoformat(),
        partition_time.isoformat(),
        str(event_code),
        float(before_minutes),
        float(after_minutes),
        code_key,
    )
    with _RAW_CACHE_LOCK:
        cached = _RAW_CACHE.get(key)
    if cached is not None:
        return cached

    # Iceberg 首次查询需要连接 Glue/S3。把小时间窗持久化到 runtime，
    # 服务器或容器重启后再次查看同一工单时无需重复访问远端数据仓库。
    cache_payload = json.dumps(
        {
            "train": train,
            "fault_time_utc": futc.isoformat(),
            "partition_time": partition_time.isoformat(),
            "event_code": str(event_code),
            "before_minutes": float(before_minutes),
            "after_minutes": float(after_minutes),
            "codes": code_key,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_name = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest() + ".parquet"
    cache_path = HISTORICAL_RAW_CACHE_DIR / cache_name
    if cache_path.is_file():
        try:
            cached = pd.read_parquet(cache_path)
            with _RAW_CACHE_LOCK:
                _RAW_CACHE[key] = cached
            return cached
        except Exception as exc:
            print(f"读取历史原始数据磁盘缓存失败，将重新查询 Iceberg：{exc}")

    df = read_iceberg_raw_window(
        train=train,
        fault_time_utc=futc,
        partition_time_cst=partition_time,
        codes=code_key,
        event_code=event_code,
        before_minutes=before_minutes,
        after_minutes=after_minutes,
    )
    if df is None or df.empty:
        raise IcebergRawDataError(
            f"Iceberg 中没有匹配数据（{train}，{partition_time:%Y-%m-%d %H:%M:%S} CST）"
        )
    df = df.copy()
    df["code_norm"] = df["code"].astype(str).map(normalize_code)
    try:
        HISTORICAL_RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_temp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        df.to_parquet(cache_temp, index=False)
        os.replace(cache_temp, cache_path)
    except Exception as exc:
        print(f"写入历史原始数据磁盘缓存失败，本次继续使用内存数据：{exc}")
    with _RAW_CACHE_LOCK:
        _RAW_CACHE[key] = df
    return df


def raw_series_for_dim(df_raw, dim, t0, t1, car_no: Any = None):
    """某维度在 [t0, t1] 内的原始逐采样 (times, values)，按时间排序。"""
    if df_raw is None or len(df_raw) == 0:
        return [], []
    code_norm = raw_code_for_dim(dim, car_no=car_no)
    sub = df_raw[df_raw["code_norm"] == code_norm]
    sub = sub[(sub["_time"] >= t0) & (sub["_time"] <= t1)].sort_values("_time")
    return list(sub["_time"]), list(sub["_value"])


def seg_time_range(seg):
    """返回建库时选中分析 segment 的起止时间。"""
    times = []
    for rcd in (seg or []):
        if rcd is None or len(rcd) < 6:
            continue
        try:
            times.extend([pd.Timestamp(rcd[2]), pd.Timestamp(rcd[4])])
        except Exception:
            continue
    return (min(times), max(times)) if times else (None, None)


def row_train_faultutc(q, ri):
    """返回 train、筛记录用 UTC 时间、选 CSV 用原始故障时间。"""
    if ri == 0:
        return (
            normalize_train(q["train"]),
            pd.Timestamp(q["fault_time_utc"]),
            pd.Timestamp(q["fault_time"]),
        )
    idx = q["top_idx"][ri - 1]
    m = q["metadata"].iloc[idx]
    return (
        normalize_train(m.get("train", "")),
        pd.Timestamp(m.get("fault_time_utc")),
        pd.Timestamp(m.get("fault_time_raw")),
    )


def row_car_no(q: Dict[str, Any], ri: int) -> str:
    if ri == 0:
        return normalize_car_no(q.get("car_no", ""))
    idx = q["top_idx"][ri - 1]
    m = q["metadata"].iloc[idx]
    return normalize_car_no(m.get("car_no", ""))


def row_data_dir(q: Dict[str, Any], ri: int) -> str:
    if ri == 0:
        return str(q.get("data_dir", DATA_DIR))
    return str(DATA_DIR_PATH)


def query_window_minutes(q: Mapping[str, Any]) -> tuple[float, float]:
    return (
        float(q.get("window_before_min", RAW_PLOT_BEFORE_MIN)),
        float(q.get("window_after_min", RAW_PLOT_AFTER_MIN)),
    )


def get_query_row_raw_df(
    q: Mapping[str, Any],
    ri: int,
    seg: Any,
    selected_dim: Any = None,
) -> pd.DataFrame:
    """Use gateway data for the new fault and Iceberg for historical rows."""
    train, futc, partition_time = row_train_faultutc(q, ri)
    if ri == 0:
        return get_raw_df(train, futc, partition_time, row_data_dir(q, ri))

    car_no = row_car_no(q, ri)
    counts = segment_counts(seg, "dim")
    dims = set(counts)
    if selected_dim:
        dims.add(str(selected_dim))
    ordered_dims = sorted(dims, key=lambda dim: (-counts.get(dim, 0), str(dim)))
    ordered_dims = raw_plot_dims_with_vcb(ordered_dims, car_no)
    before_minutes, after_minutes = query_window_minutes(q)
    return get_historical_raw_df(
        train,
        futc,
        partition_time,
        raw_codes_for_dims(ordered_dims, car_no),
        before_minutes,
        after_minutes,
    )


def raw_plot_dims_with_vcb(dims, car_no: Any = None) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for dim in list(dims) + vcb_display_dims_for_car(car_no):
        dim = str(dim)
        if dim in seen:
            continue
        seen.add(dim)
        out.append(dim)
    return out


def raw_code_for_dim(dim: Any, car_no: Any = None) -> str:
    dim = str(dim)
    car = normalize_car_no(car_no)
    original_code = normalize_code(strip_dim(dim))
    if original_code in EQUIP_UNIT_ZERO_CODES:
        return original_code
    if car in {"02", "05", "09"}:
        virtual_prefixes = {
            "target_vcb_904": f"904_2_1_{car}_sd",
            "target_vcb_936": f"936_2_1_{car}_sd",
            "target_ramping_finish_911": f"911_5_1_{car}_sd",
            "target_ramping_finish_943": f"943_5_1_{car}_sd",
        }
        if dim in virtual_prefixes:
            return virtual_prefixes[dim]
    return original_code


def raw_codes_for_dims(dims: Sequence[Any], car_no: Any = None) -> List[str]:
    """Resolve plot dimensions to exact Iceberg codes, preserving order."""
    result: List[str] = []
    seen: set[str] = set()
    for dim in dims:
        code = raw_code_for_dim(dim, car_no=car_no)
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result


def selected_event_time(seg, selected_dim: Any, car_no: Any = None):
    selected_dim = str(selected_dim) if selected_dim else ""
    for rcd in (seg or []):
        if rcd is None or len(rcd) < 6:
            continue
        raw_dim = str(rcd[1])
        normalized_dim = target_car_normalized_dim(raw_dim, car_no=car_no)
        if raw_dim == selected_dim or normalized_dim == selected_dim:
            try:
                return pd.Timestamp(rcd[4])
            except Exception:
                return None
    return None


def _coerce_num(v):
    try:
        return float(v)
    except Exception:
        return None


def _dim_events(seg, dim):
    """该维度在 segment 内的变化事件，按变化时刻排序：[(prev_t, prev_v, cur_t, cur_v), ...]"""
    evs = []
    for rcd in (seg or []):
        if rcd is None or len(rcd) < 6:
            continue
        if str(rcd[1]) != dim:
            continue
        evs.append((rcd[2], rcd[3], rcd[4], rcd[5]))
    evs.sort(key=lambda e: pd.Timestamp(e[2]))
    return evs


def _fmt_t(t) -> str:
    try:
        return pd.Timestamp(t).strftime("%H:%M:%S.%f")[:-3]
    except Exception:
        return str(t)


def _col_label_to_dim(q: Dict[str, Any], granularity: str, col_label: str):
    """把点击到的列标签（去掉 dir_ 后的显示名）映射回真实维度 dim。"""
    segs = [q["new_seg"]] + list(q["top_segs"])
    if granularity == "node":
        for seg in segs:
            for node, dim in segment_nodes(seg).items():
                if strip_dim(node) == col_label:
                    return dim
        return None
    for seg in segs:
        for rcd in (seg or []):
            if rcd is None or len(rcd) < 6:
                continue
            d = str(rcd[1])
            if strip_dim(d) == col_label:
                return d
    return None


def build_single_ts(
    df_raw,
    dim,
    fault_utc,
    car_no: Any = None,
    window_before_min: float = RAW_PLOT_BEFORE_MIN,
    window_after_min: float = RAW_PLOT_AFTER_MIN,
) -> go.Figure:
    """单变量原始逐采样时序：使用本次查询设置的窗口。"""
    label = dim_display(dim)
    t0 = pd.Timestamp(fault_utc) - pd.Timedelta(minutes=window_before_min)
    t1 = pd.Timestamp(fault_utc) + pd.Timedelta(minutes=window_after_min)
    xs, ys = raw_series_for_dim(df_raw, dim, t0, t1, car_no=car_no)
    if not xs:
        return _placeholder(f"维度 {label}：该参数此窗口内无原始采样")

    ex = [t0] + list(xs) + [t1]          # 首尾延伸，保证画满整段时间
    ey = [ys[0]] + list(ys) + [ys[-1]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line_shape="hv",
                             line=dict(color="#1f77b4", width=1.6),
                             hovertemplate="%{x|%H:%M:%S.%L}<br>值 %{y}<extra></extra>", name=label))
    fig.add_trace(go.Scatter(x=list(xs), y=list(ys), mode="markers",
                             marker=dict(size=4, color="#1f77b4"), hoverinfo="skip", showlegend=False))
    fig.add_vline(x=pd.Timestamp(fault_utc), line=dict(color=NEW_COLOR, width=1.5, dash="dash"))
    fig.update_layout(
        title=dict(text=f"维度 {label}　原始数据（{len(xs)} 个采样，红线=故障时刻，UTC）",
                   font=dict(size=13), x=0.02, xanchor="left"),
        height=520, margin=dict(l=55, r=20, t=46, b=42), template="plotly_white",
        xaxis_title="时间(UTC)", yaxis_title="值", showlegend=False,
    )
    return fig


def build_combined_ts(
    df_raw,
    dims,
    fault_utc,
    highlight_dim=None,
    seg=None,
    car_no: Any = None,
    window_before_min: float = RAW_PLOT_BEFORE_MIN,
    window_after_min: float = RAW_PLOT_AFTER_MIN,
) -> go.Figure:
    """全维度原始时序；标出建库选中的 segment，并用变量原名显示。"""
    dims = list(dims)
    if not dims:
        return _placeholder("此窗口内无可显示的维度")
    t0 = pd.Timestamp(fault_utc) - pd.Timedelta(minutes=window_before_min)
    t1 = pd.Timestamp(fault_utc) + pd.Timedelta(minutes=window_after_min)
    highlight_dim = str(highlight_dim) if highlight_dim else None

    fig = go.Figure()
    tickvals, ticktext = [], []
    for i, dim in enumerate(dims):
        if i % 2 == 0:
            fig.add_hrect(y0=i, y1=i + 1, fillcolor="#f8fafc", opacity=1.0,
                          line_width=0, layer="below")
        tickvals.append(i + 0.5)
        display_name = dim_display(dim)
        ticktext.append(wrap_label_html(display_name, 14))
        xs, ys = raw_series_for_dim(df_raw, dim, t0, t1, car_no=car_no)
        if not xs:
            continue
        nums = [_coerce_num(v) for v in ys]
        valid = [n for n in nums if n is not None]
        if valid:
            lo, hi = min(valid), max(valid)
            rng = (hi - lo) or 1.0
            ny = [((n - lo) / rng if n is not None else 0.0) for n in nums]
        else:
            ny = [0.5] * len(ys)
        Y = [i + 0.12 + 0.76 * v for v in ny]
        ex = [t0] + list(xs) + [t1]
        ey = [Y[0]] + Y + [Y[-1]]
        cd = [ys[0]] + list(ys) + [ys[-1]]
        selected = highlight_dim is not None and str(dim) == highlight_dim
        # 所有未选维度统一为灰蓝色；红色只表达“当前选中”，避免彩虹色造成误读。
        base_color = "#64748b"
        color = NEW_COLOR if selected else base_color
        width = 4.0 if selected else 1.45
        opacity = 1.0 if selected else 0.72
        fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line_shape="hv",
                                 line=dict(color=color, width=width), opacity=opacity, showlegend=False,
                                 meta={"kind": "dimension", "dim": str(dim), "base_color": base_color},
                                 text=[display_name] * len(ex), customdata=cd,
                                 hovertemplate="%{text}<br>%{x|%H:%M:%S.%L}<br>值 %{customdata}<extra></extra>"))

    seg_start, seg_end = seg_time_range(seg)
    if seg_start is not None:
        if seg_start == seg_end:
            seg_end = seg_end + pd.Timedelta(seconds=1)
        fig.add_vrect(
            x0=seg_start, x1=seg_end, fillcolor="#fbbf24", opacity=0.18,
            layer="below", line_width=0,
            annotation_text="分析 segment", annotation_position="top left",
            annotation_font=dict(color="#8a6200", size=11),
        )
    fig.add_vline(x=pd.Timestamp(fault_utc), line=dict(color=NEW_COLOR, width=1.2, dash="dash"))
    seg_text = ""
    if seg_start is not None:
        fault_ts = pd.Timestamp(fault_utc)
        seg_center = seg_start + (seg_end - seg_start) / 2
        offset_sec = int(round((seg_center - fault_ts).total_seconds()))
        offset_abs = abs(offset_sec)
        offset_text = (
            f"故障前 {offset_abs} 秒"
            if offset_sec < 0
            else f"故障后 {offset_abs} 秒"
            if offset_sec > 0
            else "与故障时刻重合"
        )
        seg_text = f"分析 segment：{seg_start:%H:%M:%S}–{seg_end:%H:%M:%S} · 距离故障时刻：{offset_text}"
    fig.update_layout(
        title=dict(text=("<b>原始数据可视化</b><br><br><span style='font-size:13px;color:#64748b'>红线=故障时刻"
                          + (f"　｜　{seg_text}" if seg_text else "")
                          + (f" · 当前选中变量：{dim_display(highlight_dim)}" if highlight_dim else "") + "</span>"),
                   font=dict(size=16), x=0.02, xanchor="left"),
        height=max(484, 102 + len(dims) * 38), margin=dict(l=195, r=65, t=112, b=42), template="plotly_white",
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff", hovermode="closest",
        xaxis=dict(title="时间（UTC）", range=[t0, t1], showgrid=True, gridcolor="#eef2f7", zeroline=False),
        yaxis=dict(tickvals=tickvals, ticktext=ticktext, tickfont=dict(size=12), range=[0, len(dims)],
                   showgrid=False, zeroline=False, automargin=False),
        font=dict(family="Microsoft YaHei, PingFang SC, Arial", size=13, color="#334155"),
        meta={"selected_dim": highlight_dim},
    )
    return fig


def row_header(q: Dict[str, Any], ri: int) -> str:
    """两个原始数据子图共用的标题：工单号 ｜ 车号 ｜ 相似度。"""
    if ri == 0:
        return f"★ 新故障　｜　车号 {normalize_train(q['train'])}　｜　（被检索的新故障本身）"
    idx = q["top_idx"][ri - 1]
    m = q["metadata"].iloc[idx]
    wo = str(m.get("work_order_no", ""))
    tr = str(m.get("train", ""))
    sim = float(q["sims"][idx])
    return f"工单 {wo}　｜　车号 {tr}　｜　相似度 {sim:.3f}"


def build_detail_rows(
    df_raw,
    dim,
    fault_utc,
    car_no: Any = None,
    window_before_min: float = RAW_PLOT_BEFORE_MIN,
    window_after_min: float = RAW_PLOT_AFTER_MIN,
):
    """所点参数在分析窗口内的原始逐采样明细行（给数据明细表）。"""
    label = dim_display(dim)
    t0 = pd.Timestamp(fault_utc) - pd.Timedelta(minutes=window_before_min)
    t1 = pd.Timestamp(fault_utc) + pd.Timedelta(minutes=window_after_min)
    xs, ys = raw_series_for_dim(df_raw, dim, t0, t1, car_no=car_no)
    rows = []
    prev = None
    for i, (t, v) in enumerate(zip(xs, ys)):
        changed = "" if (prev is None or v == prev) else f"{prev} → {v}"
        rows.append({
            "序号": i + 1,
            "时间(UTC)": pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "维度": label,
            "值": v,
            "较上一采样变化": changed,
        })
        prev = v
    # 保持原始时间与序号顺序；前端仅把视口滚动到首个变化点，不重排数据。
    return rows


# ============================================================
# 工单级相似度聚类热力图
# ============================================================



RF_CLUSTER_THRESHOLD = 0.7


def _is_one_to_zero(prev: Any, cur: Any) -> bool:
    return fmt_value(prev) == "1" and fmt_value(cur) == "0"


def _core_detail_columns(node_index: Dict[str, int]) -> List[int]:
    cols = set(excluded_node_columns(node_index))
    for node, pos in node_index.items():
        text = str(node)
        if text.startswith("target_vcb_") or text.startswith("target_ramping_finish_"):
            cols.add(int(pos))
    return sorted(cols)


def _zero_and_normalize(vec: np.ndarray, cols: List[int]) -> np.ndarray:
    out = np.asarray(vec, dtype=float).copy()
    if cols:
        out[cols] = 0.0
    norm = float(np.linalg.norm(out))
    return out / norm if norm > 0 else out


def _final_similarity(
    raw_similarity: float,
    detail_similarity: float,
    shared_non_core: int,
    rf_a: str,
    rf_b: str,
    *,
    same_work_order: bool = False,
) -> float:
    """Apply the single final-similarity rule used by search and heatmaps."""
    if same_work_order:
        return 1.0
    if rf_a != rf_b:
        return min(float(raw_similarity), 0.30)
    if int(shared_non_core) == 0:
        return min(float(raw_similarity), 0.45)
    return min(1.0, 0.35 + 0.65 * float(detail_similarity))


def _ramping_dim_for_vcb_dim(dim: str) -> str:
    text = str(dim)
    if text in RAMPING_DIM_BY_VCB_DIM:
        return RAMPING_DIM_BY_VCB_DIM[text]
    if text.startswith("dir_904_2_1_"):
        return text.replace("dir_904_2_1_", "dir_911_5_1_", 1)
    if text.startswith("dir_936_2_1_"):
        return text.replace("dir_936_2_1_", "dir_943_5_1_", 1)
    return ""


def _segment_rf_label(seg: Any, car_no: Any = None) -> str:
    if not seg:
        return "RF=-"
    allowed_vcb = set(get_vcb_target_dims_for_car(car_no))
    allowed_ramping = {_ramping_dim_for_vcb_dim(dim) for dim in allowed_vcb}
    ramp_rows = []
    for rcd in seg:
        if rcd is None or len(rcd) < 6:
            continue
        dim = str(rcd[1])
        if dim in allowed_ramping and str(rcd[3]) == "ramping_finish_status":
            ramp_rows.append((dim, pd.Timestamp(rcd[2]), pd.Timestamp(rcd[4]), fmt_value(rcd[5])))

    values: List[str] = []
    for rcd in seg:
        if rcd is None or len(rcd) < 6:
            continue
        dim = str(rcd[1])
        if dim not in allowed_vcb:
            continue
        if not _is_one_to_zero(rcd[3], rcd[5]):
            continue
        event_time = pd.Timestamp(rcd[4])
        ramp_dim = _ramping_dim_for_vcb_dim(dim)
        for rr_dim, rr_prev_t, rr_cur_t, rr_value in ramp_rows:
            if rr_dim == ramp_dim and rr_prev_t == event_time and rr_cur_t == event_time:
                if rr_value in {"0", "1"}:
                    values.append(rr_value)
                break

    has_0 = "0" in values
    has_1 = "1" in values
    if has_0 and has_1:
        return "RF=0/1"
    if has_1:
        return "RF=1"
    if has_0:
        return "RF=0"
    return "RF=-"


def _library_rf_lookup(metadata: pd.DataFrame) -> Dict[str, str]:
    segments = load_segments()
    rows: Dict[str, List[str]] = {}
    for _, meta in metadata.iterrows():
        wo = str(meta.get("work_order_no", ""))
        case_id = str(meta.get("case_id", ""))
        label = _segment_rf_label(segments.get(case_id), meta.get("car_no", ""))
        if label != "RF=-":
            rows.setdefault(wo, []).append(label)

    out: Dict[str, str] = {}
    for wo, labels in rows.items():
        has_0 = "RF=0" in labels or "RF=0/1" in labels
        has_1 = "RF=1" in labels or "RF=0/1" in labels
        if has_0 and has_1:
            out[wo] = "RF=0/1"
        elif has_1:
            out[wo] = "RF=1"
        elif has_0:
            out[wo] = "RF=0"
        else:
            out[wo] = "RF=-"
    return out


def _rf_order(label: str) -> int:
    return {"RF=1": 0, "RF=0": 1, "RF=0/1": 2, "RF=-": 3}.get(str(label), 9)


def _cluster_order_complete(group_indices: List[int], similarities: np.ndarray) -> tuple[List[int], Dict[int, int]]:
    if len(group_indices) <= 1:
        return group_indices, {group_indices[0]: 1} if group_indices else {}

    sub = similarities[np.ix_(group_indices, group_indices)]
    distances = np.clip(1.0 - sub, 0.0, 1.0)
    distances = (distances + distances.T) / 2.0
    np.fill_diagonal(distances, 0.0)
    hierarchy = linkage(squareform(distances, checks=False), method="complete")
    leaves = leaves_list(hierarchy).astype(int).tolist()
    ordered = [group_indices[i] for i in leaves]
    raw_clusters = fcluster(
        hierarchy, t=1.0 - RF_CLUSTER_THRESHOLD, criterion="distance"
    ).astype(int)

    renumber: Dict[int, int] = {}
    cluster_by_global: Dict[int, int] = {}
    for global_idx in ordered:
        local_idx = group_indices.index(global_idx)
        raw = int(raw_clusters[local_idx])
        if raw not in renumber:
            renumber[raw] = len(renumber) + 1
        cluster_by_global[global_idx] = renumber[raw]
    return ordered, cluster_by_global


def _evidence_label(rf_label: str, wo: str, max_shared: int, max_detail: float) -> str:
    if rf_label == "RF=1":
        return "ramping finish"
    if rf_label == "RF=0/1":
        return "mixed/uncertain"
    if rf_label == "RF=0":
        if wo == "5001505634":
            return "C/I-main circuit"
        if wo == "5001453818":
            return "low-info trigger"
        if max_shared <= 0:
            return "low-info trigger"
        if max_detail >= 0.85:
            return "strong-detail"
        return "detail evidence"
    return "unknown"


def build_work_order_similarity_heatmap(query: Dict[str, Any] | None = None) -> go.Figure:
    """工单级热力图；页面只展示最终相似度。"""
    vectors, metadata, node_index = load_library()
    metadata = metadata.copy()
    metadata["work_order_no"] = metadata["work_order_no"].astype(str)
    work_orders = sorted(metadata["work_order_no"].unique().tolist())
    indices_by_wo = {
        wo: metadata.index[metadata["work_order_no"] == wo].astype(int).tolist()
        for wo in work_orders
    }

    labels = list(work_orders)
    rf_lookup = _library_rf_lookup(metadata)
    train_lookup = metadata.groupby("work_order_no")["train"].first().astype(str).to_dict()
    car_lookup = metadata.groupby("work_order_no")["car_no"].first().astype(str).to_dict()
    segments = load_segments()
    case_rf_labels = [
        _segment_rf_label(
            segments.get(str(meta.get("case_id", ""))),
            meta.get("car_no", ""),
        )
        for _, meta in metadata.iterrows()
    ]

    raw_vectors = np.asarray(vectors, dtype=float)
    detail_cols = _core_detail_columns(node_index)
    detail_vectors = raw_vectors.copy()
    if detail_cols:
        detail_vectors[:, detail_cols] = 0.0
    detail_vectors = normalize_vector_rows(detail_vectors)
    active_detail = detail_vectors > 0

    if query is not None and query.get("ok"):
        new_label = "☑ 新故障"
        labels.append(new_label)
        query_vec = np.asarray(query.get("query_vec"), dtype=float)
        raw_vectors = np.vstack([raw_vectors, query_vec])
        query_detail_vec = _zero_and_normalize(query_vec, detail_cols)
        detail_vectors = np.vstack([detail_vectors, query_detail_vec])
        active_detail = np.vstack([active_detail, query_detail_vec > 0])
        indices_by_wo[new_label] = [len(raw_vectors) - 1]
        query_rf_label = _segment_rf_label(query.get("new_seg"), query.get("car_no"))
        case_rf_labels.append(query_rf_label)
        rf_lookup[new_label] = query_rf_label
        train_lookup[new_label] = str(query.get("train", ""))
        car_lookup[new_label] = str(query.get("car_no", ""))

    n = len(labels)
    raw_case_sim = np.clip(raw_vectors @ raw_vectors.T, 0.0, 1.0)
    detail_case_sim = np.clip(detail_vectors @ detail_vectors.T, 0.0, 1.0)

    raw_similarities = np.eye(n, dtype=float)
    detail_similarities = np.eye(n, dtype=float)
    display_similarities = np.eye(n, dtype=float)
    shared_counts = np.zeros((n, n), dtype=int)
    best_case_pairs = np.full((n, n), "—", dtype=object)

    for i, wo_a in enumerate(labels):
        for j, wo_b in enumerate(labels):
            if j < i:
                raw_similarities[i, j] = raw_similarities[j, i]
                detail_similarities[i, j] = detail_similarities[j, i]
                display_similarities[i, j] = display_similarities[j, i]
                shared_counts[i, j] = shared_counts[j, i]
                best_case_pairs[i, j] = best_case_pairs[j, i]
                continue

            best_score = -1.0
            best_payload: Dict[str, Any] = {}
            for case_i in indices_by_wo[wo_a]:
                for case_j in indices_by_wo[wo_b]:
                    raw_similarity = float(raw_case_sim[case_i, case_j])
                    detail_similarity = float(detail_case_sim[case_i, case_j])
                    shared_non_core = int(np.logical_and(active_detail[case_i], active_detail[case_j]).sum())
                    rf_a = case_rf_labels[case_i]
                    rf_b = case_rf_labels[case_j]
                    display_similarity = _final_similarity(
                        raw_similarity,
                        detail_similarity,
                        shared_non_core,
                        rf_a,
                        rf_b,
                        same_work_order=wo_a == wo_b,
                    )

                    if display_similarity > best_score:
                        if wo_a == "☑ 新故障":
                            case_a = "新故障"
                        else:
                            case_a = str(metadata.iloc[case_i].get("case_id", ""))
                        if wo_b == "☑ 新故障":
                            case_b = "新故障"
                        else:
                            case_b = str(metadata.iloc[case_j].get("case_id", ""))
                        best_score = display_similarity
                        best_payload = {
                            "raw": raw_similarity,
                            "detail": detail_similarity,
                            "display": display_similarity,
                            "shared": shared_non_core,
                            "pair": f"{case_a} → {case_b}",
                        }

            raw_similarities[i, j] = raw_similarities[j, i] = float(best_payload.get("raw", 0.0))
            detail_similarities[i, j] = detail_similarities[j, i] = float(best_payload.get("detail", 0.0))
            display_similarities[i, j] = display_similarities[j, i] = float(best_payload.get("display", 0.0))
            shared_counts[i, j] = shared_counts[j, i] = int(best_payload.get("shared", 0))
            best_case_pairs[i, j] = best_case_pairs[j, i] = str(best_payload.get("pair", "—"))

    ordered_indices: List[int] = []
    cluster_by_index: Dict[int, str] = {}
    for rf_label in ["RF=1", "RF=0", "RF=0/1", "RF=-"]:
        group = [idx for idx, label in enumerate(labels) if rf_lookup.get(label, "RF=-") == rf_label]
        group = sorted(group, key=lambda idx: labels[idx])
        group_order, group_clusters = _cluster_order_complete(group, display_similarities)
        ordered_indices.extend(group_order)
        for idx in group_order:
            cluster_by_index[idx] = f"{rf_label}:C{group_clusters.get(idx, 1)}"

    for idx in range(n):
        if idx not in ordered_indices:
            ordered_indices.append(idx)
            cluster_by_index[idx] = f"{rf_lookup.get(labels[idx], 'RF=-')}:C1"

    ordered = display_similarities[np.ix_(ordered_indices, ordered_indices)]
    ordered_pairs = best_case_pairs[np.ix_(ordered_indices, ordered_indices)]

    display_labels: List[str] = []
    cluster_number_map: Dict[str, int] = {}
    for idx in ordered_indices:
        wo = labels[idx]
        cluster_key = cluster_by_index.get(idx, "C?")
        if cluster_key not in cluster_number_map:
            cluster_number_map[cluster_key] = len(cluster_number_map) + 1
        display_labels.append(
            f"C{cluster_number_map[cluster_key]}｜{wo}<br>{train_lookup.get(wo, '')}/{car_lookup.get(wo, '')}"
        )

    customdata = np.empty((n, n, 4), dtype=object)
    for row_pos, row_idx in enumerate(ordered_indices):
        for col_pos, col_idx in enumerate(ordered_indices):
            customdata[row_pos, col_pos] = [
                labels[row_idx],
                labels[col_idx],
                ordered_pairs[row_pos, col_pos],
                f"{ordered[row_pos, col_pos]:.4f}",
            ]

    text = np.empty((n, n), dtype=object)
    for row in range(n):
        for col in range(n):
            text[row, col] = f"{ordered[row, col]:.2f}"

    fig = go.Figure(go.Heatmap(
        z=ordered,
        x=display_labels,
        y=display_labels,
        zmin=0,
        zmax=1,
        colorscale=[
            [0.0, "#f7fbff"], [0.2, "#deebf7"], [0.4, "#9ecae1"],
            [0.6, "#6baed6"], [0.8, "#756bb1"], [1.0, "#54278f"],
        ],
        text=text,
        texttemplate="%{text}",
        textfont=dict(size=11),
        customdata=customdata,
        hovertemplate=(
            "<b>%{customdata[0]} → %{customdata[1]}</b>"
            "<br>最终相似度：%{customdata[3]}"
            "<br>最佳 case 对：%{customdata[2]}<extra></extra>"
        ),
        colorbar=dict(title="最终<br>相似度", thickness=14),
    ))

    rf_style = {
        "RF=1": {"line": "#2563eb", "fill": "rgba(37,99,235,0.08)", "text": "RF=1"},
        "RF=0": {"line": "#f97316", "fill": "rgba(249,115,22,0.09)", "text": "RF=0"},
        "RF=0/1": {"line": "#7c3aed", "fill": "rgba(124,58,237,0.08)", "text": "RF=0/1 mixed"},
        "RF=-": {"line": "#64748b", "fill": "rgba(100,116,139,0.06)", "text": "RF=-"},
    }
    rf_labels_ordered = [rf_lookup.get(labels[idx], "RF=-") for idx in ordered_indices]
    cluster_labels_ordered = [cluster_by_index.get(idx, "C?") for idx in ordered_indices]
    start = 0
    while start < n:
        end = start
        while end + 1 < n and rf_labels_ordered[end + 1] == rf_labels_ordered[start]:
            end += 1
        style = rf_style.get(rf_labels_ordered[start], rf_style["RF=-"])
        fig.add_shape(
            type="rect", x0=start - 0.5, x1=end + 0.5, y0=start - 0.5, y1=end + 0.5,
            line=dict(color=style["line"], width=5), fillcolor=style["fill"],
        )
        fig.add_annotation(
            x=(start + end) / 2, y=start - 0.82, text=style["text"], showarrow=False,
            font=dict(size=13, color="white"), bgcolor=style["line"], bordercolor=style["line"],
            borderpad=4, xanchor="center", yanchor="bottom",
        )
        fig.add_annotation(
            x=start - 0.95, y=(start + end) / 2, text=style["text"], showarrow=False,
            font=dict(size=12, color=style["line"]), textangle=-90,
            xanchor="right", yanchor="middle",
        )
        start = end + 1

    for pos in range(1, n):
        if cluster_labels_ordered[pos] != cluster_labels_ordered[pos - 1]:
            boundary = pos - 0.5
            fig.add_shape(type="line", x0=boundary, x1=boundary, y0=-0.5, y1=n - 0.5,
                          line=dict(color="white", width=4))
            fig.add_shape(type="line", x0=-0.5, x1=n - 0.5, y0=boundary, y1=boundary,
                          line=dict(color="white", width=4))

    if "☑ 新故障" in labels:
        new_original_index = labels.index("☑ 新故障")
        new_pos = ordered_indices.index(new_original_index)
        fig.add_shape(type="rect", x0=-0.5, x1=n - 0.5, y0=new_pos - 0.5, y1=new_pos + 0.5,
                      line=dict(color="#e6194b", width=3), fillcolor="rgba(0,0,0,0)")
        fig.add_shape(type="rect", x0=new_pos - 0.5, x1=new_pos + 0.5, y0=-0.5, y1=n - 0.5,
                      line=dict(color="#e6194b", width=3), fillcolor="rgba(0,0,0,0)")

    fig.update_layout(
        title=None,
        height=max(780, 130 + n * 64),
        template="plotly_white",
        hoverlabel=dict(align="left"),
        margin=dict(l=230, r=40, t=60, b=210),
        xaxis=dict(title="先按 RF 分区；区内按最终相似度完全链路分簇（阈值 0.7）", tickangle=-55, side="bottom", automargin=True),
        yaxis=dict(
            title="红框=RF区域；白线=0.7簇边界；格子=最终相似度",
            autorange="reversed",
            automargin=True,
        ),
        annotations=[
            *list(fig.layout.annotations or []),
            dict(
                xref="paper", yref="paper", x=0, y=1.08, showarrow=False, align="left",
                text="格子数字和颜色均为最终相似度；红框为 RF 区域，白线为 0.7 簇边界。",
                font=dict(size=12, color="#52616b"),
            ),
        ],
    )
    return fig

def clean_work_order_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return ""
    text = unescape(re.sub(r"<[^>]+>", " ", text)).replace("//", "\n\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def load_work_order_records() -> Dict[str, Dict[str, str]]:
    """Load normalized work-order source records from Qdrant payloads."""
    if "work_order_records" in _CACHE:
        return _CACHE["work_order_records"]
    records = {
        str(work_order): dict(record)
        for work_order, record in _load_qdrant_bundle()["raw_work_order_records"].items()
    }
    _CACHE["work_order_records"] = records
    return records


def load_structured_work_order_records() -> Dict[str, Dict[str, Any]]:
    """Load structured work-order records from Qdrant payloads."""
    if "structured_work_order_records" in _CACHE:
        return _CACHE["structured_work_order_records"]
    records = {
        str(work_order): dict(record)
        for work_order, record in _load_qdrant_bundle()["structured_work_order_records"].items()
    }
    _CACHE["structured_work_order_records"] = records
    return records


_KG_EVENT_GROUPS = {
    "reset": "复位 / 检查 / 功能测试",
    "checked": "复位 / 检查 / 功能测试",
    "function tested": "复位 / 检查 / 功能测试",
    "tested": "复位 / 检查 / 功能测试",
    "monitored": "复位 / 检查 / 功能测试",
    "replaced": "更换",
    "swapped": "互换测试",
    "isolated": "隔离",
    "repaired": "修复",
}

_KG_COMPONENT_GROUPS = (
    ("vcb_apc_atr", "VCB / APC / ATR", {"vcb", "apc", "atr"}),
    (
        "apc_controller_receiver",
        "APC controller / APC receiver",
        {"apc controller", "apc receiver"},
    ),
    (
        "atr_controller_receiver",
        "ATR controller / ATR receiver",
        {"atr controller", "atr receiver"},
    ),
    (
        "apccr_ramfr_vcbcr",
        "APCCR / RAMFR / VCBCR",
        {"apccr", "ramfr", "vcbcr"},
    ),
    (
        "vcb_control_relays",
        "VCBSTR / VCBCTR / VCBHR / VDOS",
        {"vcbstr", "vcbctr", "vcbhr", "vdos"},
    ),
    ("epdr_pdr", "EPDR / PDR", {"epdr", "pdr"}),
    (
        "ci_converter_inverter_gcu",
        "C/I converter / C/I inverter / C/I GCU",
        {
            "c/i converter",
            "c/i inverter",
            "c/i gcu",
            "c/i converter & inverter power unit",
            "c/i inverter power unit",
        },
    ),
    (
        "ovt_igbt",
        "OVT unit / converter IGBT module",
        {"ovt unit", "converter igbt module"},
    ),
    (
        "main_transformer_connections",
        "main transformer / cable / cable lug / terminal wire / HTJB jumper",
        {
            "main transformer",
            "main transformer cable",
            "main transformer cable lug",
            "main transformer terminal wire",
            "htjb jumper",
        },
    ),
    ("aps_converter", "APS / APS Converter unit", {"aps", "aps converter unit"}),
)


def _kg_clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(re.sub(r"<[^>]+>", " ", str(value))).strip()
    text = re.sub(r"\s+", " ", text).strip(" ,.;:")
    return "" if text.casefold() in {"", "nan", "none", "null", "n/a", "na"} else text


_KG_MAINTENANCE_ACTION_LABELS = {
    "reset": "复位",
    "checked": "检查",
    "function tested": "功能测试",
    "tested": "测试",
    "monitored": "持续监测",
    "replaced": "更换",
    "swapped": "互换测试",
    "isolated": "隔离",
    "repaired": "修复",
}


def _kg_maintenance_action_keys(node: Mapping[str, Any]) -> List[str]:
    action_order = tuple(_KG_MAINTENANCE_ACTION_LABELS)
    actions = {
        _kg_clean_text(action).casefold()
        for action in node.get("member_actions", [])
        if _kg_clean_text(action)
    }
    return [action for action in action_order if action in actions]


def _kg_maintenance_display_label(node: Mapping[str, Any]) -> str:
    """Return action-only text; affected parts are rendered in the component column."""
    labels = [
        _KG_MAINTENANCE_ACTION_LABELS[action]
        for action in _kg_maintenance_action_keys(node)
    ]
    return " / ".join(labels) or "维修处置"


def _kg_as_list(value: Any) -> List[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    output: List[str] = []
    for item in values:
        text = _kg_clean_text(item)
        if text:
            output.append(text)
    return output


def _kg_normalize_component(value: Any) -> str:
    text = re.sub(r"^(?:P|M|R)\d{3}\s+", "", _kg_clean_text(value), flags=re.I)
    aliases = {
        "c/i gcu": "C/I GCU",
        "c/i converter": "C/I converter",
        "c/i inverter": "C/I inverter",
        "apc": "APC",
        "atr": "ATR",
        "vcb": "VCB",
        "apccr": "APCCR",
        "ramfr": "RAMFR",
        "vcbcr": "VCBCR",
        "vcbhr": "VCBHR",
        "vcbstr": "VCBSTR",
        "vcbctr": "VCBCTR",
        "vdos": "VDOS",
    }
    return aliases.get(text.casefold(), text)


def _kg_component_group(value: Any) -> tuple[str, str]:
    component = _kg_normalize_component(value) or "工单未明确相关部件"
    folded = component.casefold()
    for key, label, members in _KG_COMPONENT_GROUPS:
        if folded in members:
            return key, label
    digest = hashlib.sha1(folded.encode("utf-8")).hexdigest()[:12]
    return f"single_{digest}", component


def _kg_method_label(value: Any) -> str:
    event = _kg_clean_text(value).casefold()
    if event in _KG_EVENT_GROUPS:
        return _KG_EVENT_GROUPS[event]
    if any(word in event for word in ("replac", "renew")):
        return "更换"
    if any(word in event for word in ("swap", "interchange")):
        return "互换测试"
    if any(word in event for word in ("isolat", "cut off")):
        return "隔离"
    if any(word in event for word in ("repair", "rectif")):
        return "修复"
    if any(word in event for word in ("reset", "check", "test", "monitor", "inspect")):
        return "复位 / 检查 / 功能测试"
    return _kg_clean_text(value) or "工单未记录维修手段"


def _kg_treatment_pairs(record: Mapping[str, Any]) -> List[tuple[str, str, str]]:
    components = _kg_as_list(record.get("treatment_components"))
    events = _kg_as_list(record.get("treatment_events"))
    pairs: List[tuple[str, str, str]] = []
    for index in range(max(len(components), len(events))):
        component = components[index] if index < len(components) else "工单未明确相关部件"
        event = events[index] if index < len(events) else record.get("action", "")
        pairs.append((component, _kg_method_label(event), _kg_clean_text(event)))

    if pairs:
        return pairs

    fallback_components: List[str] = []
    for value in _kg_as_list(record.get("component")):
        fallback_components.extend(
            _kg_normalize_component(item)
            for item in re.split(r"[,;]", value)
            if _kg_normalize_component(item)
        )
    raw_action = _kg_clean_text(record.get("action"))
    method = _kg_method_label(raw_action)
    return [
        (component, method, raw_action)
        for component in (fallback_components or ["工单未明确相关部件"])
    ]


def _kg_display_label(value: str) -> str:
    return _kg_clean_text(value).replace(" / ", " /<br>")


def _top3_knowledge_graph_placeholder(message: str) -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        text=message,
        showarrow=False,
        align="center",
        font=dict(size=15, color="#8ddfff"),
    )
    figure.update_layout(
        height=900,
        template="plotly_dark",
        margin=dict(l=16, r=16, t=18, b=18),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        paper_bgcolor="#06101f",
        plot_bgcolor="#06101f",
    )
    return figure


def build_top3_work_order_knowledge_graph(q: Mapping[str, Any] | None) -> go.Figure:
    """Build the query-scoped graph: work order -> maintenance -> component."""
    if not q or not q.get("top_idx"):
        return _top3_knowledge_graph_placeholder("查询后，这里显示所选相似工单的维修知识图谱")

    metadata = q["metadata"]
    similarities = q["sims"]
    structured_records = load_structured_work_order_records()
    work_orders: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    methods: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    components: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    work_order_method_edges: Dict[tuple[str, str], Dict[str, Any]] = {}
    method_component_edges: Dict[tuple[str, str], Dict[str, Any]] = {}

    for rank, index in enumerate(list(q.get("top_idx") or [])[:MAX_TOP_K], start=1):
        row = metadata.iloc[int(index)]
        work_order = _kg_clean_text(row.get("work_order_no")).removesuffix(".0")
        if not work_order:
            continue
        record = structured_records.get(work_order, {})
        work_order_id = f"work_order:{work_order}"
        work_orders[work_order_id] = {
            "label": work_order,
            "rank": rank,
            "similarity": float(similarities[int(index)]),
            "train": _kg_clean_text(row.get("train")),
            "fault_time": _kg_clean_text(row.get("fault_time_raw")),
            "action": _kg_clean_text(record.get("action")),
        }

        for original_component, method_label, original_event in _kg_treatment_pairs(record):
            component_key, component_label = _kg_component_group(original_component)
            method_id = f"method:{method_label}"
            component_id = f"component:{component_key}"
            method_node = methods.setdefault(
                method_id,
                {"label": method_label, "work_orders": set(), "original_events": set()},
            )
            method_node["work_orders"].add(work_order)
            if original_event:
                method_node["original_events"].add(original_event)

            component_node = components.setdefault(
                component_id,
                {
                    "label": component_label,
                    "work_orders": set(),
                    "original_components": set(),
                },
            )
            component_node["work_orders"].add(work_order)
            component_node["original_components"].add(_kg_normalize_component(original_component))

            wo_edge = work_order_method_edges.setdefault(
                (work_order_id, method_id), {"work_orders": set(), "components": set()}
            )
            wo_edge["work_orders"].add(work_order)
            wo_edge["components"].add(component_label)
            component_edge = method_component_edges.setdefault(
                (method_id, component_id), {"work_orders": set(), "components": set()}
            )
            component_edge["work_orders"].add(work_order)
            component_edge["components"].add(_kg_normalize_component(original_component))

    if not work_orders:
        return _top3_knowledge_graph_placeholder("当前查询没有可展示的相似工单")

    method_priority = {
        "复位 / 检查 / 功能测试": 0,
        "更换": 1,
        "互换测试": 2,
        "隔离": 3,
        "修复": 4,
    }
    method_ids = sorted(
        methods,
        key=lambda node_id: (method_priority.get(methods[node_id]["label"], 99), methods[node_id]["label"]),
    )
    component_ids = list(components)
    column_nodes = [list(work_orders), method_ids, component_ids]

    def vertical_positions(count: int) -> List[float]:
        if count <= 1:
            return [0.5]
        return np.linspace(0.92, 0.08, count).astype(float).tolist()

    positions: Dict[str, tuple[float, float]] = {}
    for x, node_ids in zip((0.0, 1.0, 2.0), column_nodes):
        for node_id, y in zip(node_ids, vertical_positions(len(node_ids))):
            positions[node_id] = (x, y)

    figure = go.Figure()
    rank_colors = {1: "#ff4d8d", 2: "#26d8ff", 3: "#9d6cff"}
    for x, border, fill in (
        (0.0, "rgba(255,77,141,0.30)", "rgba(255,77,141,0.035)"),
        (1.0, "rgba(247,209,84,0.28)", "rgba(247,209,84,0.030)"),
        (2.0, "rgba(38,255,174,0.28)", "rgba(38,255,174,0.030)"),
    ):
        figure.add_shape(
            type="rect",
            x0=x - 0.27,
            x1=x + 0.27,
            y0=0.01,
            y1=1.0,
            line=dict(color=border, width=1),
            fillcolor=fill,
            layer="below",
        )
    for y in np.linspace(0.08, 0.92, 8):
        figure.add_shape(
            type="line",
            x0=-0.27,
            x1=2.27,
            y0=float(y),
            y1=float(y),
            line=dict(color="rgba(69,153,205,0.075)", width=1, dash="dot"),
            layer="below",
        )

    def add_edge(source: str, target: str, color: str, hover: str) -> None:
        x0, y0 = positions[source]
        x1, y1 = positions[target]
        figure.add_trace(go.Scatter(
            x=[x0, x1],
            y=[y0, y1],
            mode="lines",
            line=dict(color="rgba(48,210,255,0.13)", width=9),
            hoverinfo="skip",
            showlegend=False,
        ))
        figure.add_trace(go.Scatter(
            x=[x0, x1],
            y=[y0, y1],
            mode="lines",
            line=dict(color=color, width=2.4),
            hoverinfo="skip",
            showlegend=False,
        ))
        figure.add_trace(go.Scatter(
            x=[(x0 + x1) / 2],
            y=[(y0 + y1) / 2],
            mode="markers",
            marker=dict(size=18, color="rgba(0,0,0,0)"),
            hovertext=hover,
            hovertemplate="%{hovertext}<extra></extra>",
            showlegend=False,
        ))

    for (source, target), evidence in work_order_method_edges.items():
        rank = int(work_orders[source]["rank"])
        hover = (
            f"工单 {work_orders[source]['label']} → {methods[target]['label']}"
            f"<br>涉及部件：{'、'.join(sorted(evidence['components']))}"
        )
        add_edge(source, target, rank_colors.get(rank, "#64748b"), hover)
    for (source, target), evidence in method_component_edges.items():
        hover = (
            f"{methods[source]['label']} → {components[target]['label']}"
            f"<br>来源工单：{'、'.join(sorted(evidence['work_orders']))}"
            f"<br>原始部件：{'、'.join(sorted(value for value in evidence['components'] if value))}"
        )
        add_edge(source, target, "rgba(38,255,174,0.82)", hover)

    node_x: List[float] = []
    node_y: List[float] = []
    node_hover: List[str] = []
    for node_id, data in work_orders.items():
        x, y = positions[node_id]
        node_x.append(x)
        node_y.append(y)
        node_hover.append(
            f"<b>Top {data['rank']} · 工单 {data['label']}</b>"
            f"<br>相似度：{data['similarity']:.1%}"
            f"<br>车号：{data['train'] or '未记录'}"
            f"<br>故障时间：{data['fault_time'] or '未记录'}"
            f"<br>工单处置原文：{(data['action'] or '未记录')[:700]}"
        )
        figure.add_annotation(
            x=x,
            y=y,
            text=f"Top {data['rank']}<br><b>{data['label']}</b><br>{data['similarity']:.1%}",
            showarrow=False,
            width=132,
            align="center",
            bordercolor=rank_colors.get(int(data["rank"]), "#26d8ff"),
            borderwidth=2,
            borderpad=8,
            bgcolor="#0a1930",
            font=dict(size=12, color="#e8f8ff"),
        )

    for node_id in method_ids:
        data = methods[node_id]
        x, y = positions[node_id]
        node_x.append(x)
        node_y.append(y)
        node_hover.append(
            f"<b>维修手段：{data['label']}</b>"
            f"<br>来源工单：{'、'.join(sorted(data['work_orders']))}"
            f"<br>原始维修事件：{'、'.join(sorted(data['original_events'])) or '未记录'}"
        )
        figure.add_annotation(
            x=x,
            y=y,
            text=f"<b>{_kg_display_label(data['label'])}</b>",
            showarrow=False,
            width=158,
            align="center",
            bordercolor="#f7d154",
            borderwidth=2,
            borderpad=9,
            bgcolor="#18213a",
            font=dict(size=12, color="#ffe98f"),
        )

    for node_id in component_ids:
        data = components[node_id]
        x, y = positions[node_id]
        node_x.append(x)
        node_y.append(y)
        original_components = sorted(value for value in data["original_components"] if value)
        node_hover.append(
            f"<b>相关部件：{data['label']}</b>"
            f"<br>来源工单：{'、'.join(sorted(data['work_orders']))}"
            f"<br>工单中实际出现：{'、'.join(original_components) or '未记录'}"
        )
        figure.add_annotation(
            x=x,
            y=y,
            text=f"<b>{_kg_display_label(data['label'])}</b>",
            showarrow=False,
            width=190,
            align="center",
            bordercolor="#26ffae",
            borderwidth=2,
            borderpad=8,
            bgcolor="#092a2b",
            font=dict(size=11, color="#9dffda"),
        )

    figure.add_trace(go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers",
        marker=dict(size=54, color="rgba(0,0,0,0)"),
        hovertext=node_hover,
        hovertemplate="%{hovertext}<extra></extra>",
        showlegend=False,
    ))
    for x, title, color in (
        (0.0, "工单号", "#ff6ca4"),
        (1.0, "维修手段", "#ffe47a"),
        (2.0, "相关部件", "#5dffc2"),
    ):
        figure.add_annotation(
            x=x,
            y=1.08,
            text=f"<b>{title}</b>",
            showarrow=False,
            font=dict(size=16, color=color),
        )

    largest_column = max(len(column) for column in column_nodes)
    figure.update_layout(
        height=max(900, min(1160, 230 + largest_column * 80)),
        template="plotly_dark",
        margin=dict(l=86, r=112, t=62, b=40),
        hoverlabel=dict(
            align="left",
            bgcolor="#07182d",
            bordercolor="#26d8ff",
            font=dict(size=12, color="#eaf8ff"),
        ),
        xaxis=dict(range=[-0.28, 2.28], visible=False, fixedrange=True),
        yaxis=dict(range=[-0.04, 1.14], visible=False, fixedrange=True),
        paper_bgcolor="#06101f",
        plot_bgcolor="#06101f",
        showlegend=False,
    )
    return figure


def _top3_graph_placeholder_document(message: str) -> str:
    safe_message = json.dumps(str(message), ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}html,body{{width:100%;height:100%;margin:0;overflow:hidden}}
body{{position:relative;display:grid;place-items:center;background:transparent;color:#8ca3b0;font:13px/1.6 "HarmonyOS Sans SC","PingFang SC","Microsoft YaHei",sans-serif}}
body::before,body::after{{display:none}}
.empty{{position:relative;z-index:1;min-width:300px;padding:20px 28px;border:1px dashed rgba(145,198,225,.20);border-radius:9px;background:rgba(37,61,78,.22);box-shadow:inset 0 1px 0 rgba(255,255,255,.03);text-align:center;letter-spacing:.2px}}
.empty::before{{display:block;width:7px;height:7px;margin:0 auto 11px;border-radius:50%;background:rgba(99,201,244,.58);content:"";animation:pulse 3s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.55}}}}
@media(prefers-reduced-motion:reduce){{.empty::before{{animation:none}}}}
</style></head><body><div class="empty" id="message"></div><script>
document.getElementById("message").textContent={safe_message};
</script></body></html>"""


def load_vcb_trip_knowledge_graph() -> Dict[str, Any]:
    """Load the canonical full VCB Trip graph and refresh it when the JSON changes."""
    global _VCB_KNOWLEDGE_GRAPH_CACHE, _VCB_KNOWLEDGE_GRAPH_MTIME_NS

    if not VCB_KNOWLEDGE_GRAPH_PATH.exists():
        raise FileNotFoundError(f"完整知识图谱数据不存在：{VCB_KNOWLEDGE_GRAPH_PATH}")
    graph_mtime_ns = VCB_KNOWLEDGE_GRAPH_PATH.stat().st_mtime_ns
    with _VCB_KNOWLEDGE_GRAPH_LOCK:
        if (
            _VCB_KNOWLEDGE_GRAPH_CACHE is not None
            and _VCB_KNOWLEDGE_GRAPH_MTIME_NS == graph_mtime_ns
        ):
            return _VCB_KNOWLEDGE_GRAPH_CACHE
        with VCB_KNOWLEDGE_GRAPH_PATH.open("r", encoding="utf-8") as graph_file:
            graph_data = json.load(graph_file)
        if not isinstance(graph_data, dict):
            raise ValueError("完整知识图谱 JSON 顶层必须是对象")
        if not isinstance(graph_data.get("nodes"), list) or not isinstance(graph_data.get("edges"), list):
            raise ValueError("完整知识图谱 JSON 缺少 nodes 或 edges")
        node_types = {str(node.get("type") or "") for node in graph_data["nodes"] if isinstance(node, Mapping)}
        required_types = {"work_order", "maintenance", "equipment"}
        if not required_types.issubset(node_types):
            missing_types = "、".join(sorted(required_types - node_types))
            raise ValueError(f"完整知识图谱缺少标准节点类型：{missing_types}")
        _VCB_KNOWLEDGE_GRAPH_CACHE = graph_data
        _VCB_KNOWLEDGE_GRAPH_MTIME_NS = graph_mtime_ns
        return graph_data


def _knowledge_graph_signal_catalog(graph_data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the canonical signal vocabulary exactly as defined by the full graph."""
    catalog: List[Dict[str, Any]] = []
    for node in graph_data.get("nodes", []):
        if not isinstance(node, Mapping) or str(node.get("type") or "") != "signal":
            continue
        description = _kg_clean_text(
            node.get("normalized_dimension")
            or node.get("signal_description")
            or node.get("label")
        )
        normalized_key = _kg_clean_text(node.get("normalized_signal_key")) or str(node.get("id") or "")
        if not normalized_key:
            continue
        catalog.append({
            "id": str(node.get("id") or ""),
            "normalized_signal_key": normalized_key,
            "normalized_dimension": description,
            "previous_value": _kg_clean_text(node.get("previous_value")),
            "current_value": _kg_clean_text(node.get("current_value")),
            "source_normalized_nodes": [
                _kg_clean_text(value)
                for value in list(node.get("source_normalized_nodes") or [])
                if _kg_clean_text(value)
            ],
            "work_orders": [
                _kg_clean_text(value).removesuffix(".0")
                for value in list(node.get("work_orders") or [])
                if _kg_clean_text(value)
            ],
        })
    return sorted(
        catalog,
        key=lambda item: (
            item["normalized_dimension"].casefold(),
            item["previous_value"],
            item["current_value"],
            item["normalized_signal_key"],
        ),
    )


def _new_fault_signal_changes(q: Mapping[str, Any] | None) -> List[Dict[str, Any]]:
    """Keep raw normalized changes so the latest graph can remap them at click time."""
    if not q:
        return []
    car_no = q.get("car_no", "")
    changes: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    for record in q.get("new_seg") or []:
        if record is None or len(record) < 6:
            continue
        dimension = target_car_normalized_dim(record[1], car_no=car_no)
        if dimension is None:
            continue
        previous_value = _kg_clean_text(record[3])
        current_value = _kg_clean_text(record[5])
        raw_node = f"{dimension}_{previous_value}_{current_value}"
        change = changes.setdefault(
            raw_node,
            {
                "raw_node": raw_node,
                "dimension": str(dimension),
                "description": dim_display(str(dimension)),
                "previous_value": previous_value,
                "current_value": current_value,
                "count": 0,
            },
        )
        change["count"] += 1
    return list(changes.values())


def _knowledge_graph_signal_snapshot(work_order: str = "") -> Dict[str, Any]:
    """Read a fresh, mtime-aware signal snapshot for browser comparisons."""
    graph_data = load_vcb_trip_knowledge_graph()
    catalog = _knowledge_graph_signal_catalog(graph_data)
    normalized_work_order = _kg_clean_text(work_order).removesuffix(".0")
    selected_keys = [
        item["normalized_signal_key"]
        for item in catalog
        if normalized_work_order and normalized_work_order in item["work_orders"]
    ]
    graph_meta = dict(graph_data.get("meta") or {})
    return {
        "schema_version": graph_meta.get("schema_version"),
        "generated_at": graph_meta.get("generated_at"),
        "source_file": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
        "excluded_signals": list(graph_meta.get("excluded_signals") or []),
        "catalog": catalog,
        "work_order": normalized_work_order,
        "work_order_signal_keys": selected_keys,
    }


def build_top3_work_order_knowledge_graph_payload(q: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Extract the selected work orders' union subgraph from the canonical full graph."""
    if not q or not q.get("top_idx"):
        return {"nodes": [], "edges": [], "meta": {"source": "canonical_vcb_trip_graph"}}

    full_graph = load_vcb_trip_knowledge_graph()
    raw_work_order_records = load_work_order_records()
    structured_records = load_structured_work_order_records()
    signal_snapshot = _knowledge_graph_signal_snapshot()
    signal_keys_by_work_order: Dict[str, List[str]] = {}
    for signal in signal_snapshot["catalog"]:
        for signal_work_order in signal["work_orders"]:
            signal_keys_by_work_order.setdefault(signal_work_order, []).append(
                signal["normalized_signal_key"]
            )
    canonical_nodes = {
        str(node.get("id")): node
        for node in full_graph.get("nodes", [])
        if isinstance(node, Mapping) and node.get("id")
    }
    maintenance_edges_by_work_order: Dict[str, List[Mapping[str, Any]]] = {}
    equipment_edges_by_maintenance: Dict[str, List[Mapping[str, Any]]] = {}
    for edge in full_graph.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        edge_type = str(edge.get("type") or "")
        if edge_type == "maintenance_to_work_order":
            maintenance_edges_by_work_order.setdefault(str(edge.get("target") or ""), []).append(edge)
        elif edge_type == "equipment_to_maintenance":
            equipment_edges_by_maintenance.setdefault(str(edge.get("target") or ""), []).append(edge)

    metadata = q["metadata"]
    similarities = q["sims"]
    graph_nodes: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    graph_edges: OrderedDict[tuple[str, str], Dict[str, Any]] = OrderedDict()
    rank_colors = ["#ff4d8d", "#26d8ff", "#9d6cff"]
    missing_work_orders: List[str] = []

    def append_unique(target: List[str], value: Any) -> None:
        text = _kg_clean_text(value)
        if text and text not in target:
            target.append(text)

    def append_many(target: List[str], values: Any) -> None:
        if isinstance(values, (list, tuple, set)):
            for value in values:
                append_unique(target, value)
        else:
            append_unique(target, values)

    def connect(source: str, target: str, work_order: str, canonical_edge_id: Any) -> None:
        edge = graph_edges.setdefault(
            (source, target),
            {
                "id": f"view:{source}->{target}",
                "source": source,
                "target": target,
                "work_orders": [],
                "canonical_edge_ids": [],
            },
        )
        append_unique(edge["work_orders"], work_order)
        append_unique(edge["canonical_edge_ids"], canonical_edge_id)

    for rank, index in enumerate(list(q.get("top_idx") or [])[:MAX_TOP_K], start=1):
        row = metadata.iloc[int(index)]
        work_order = _kg_clean_text(row.get("work_order_no")).removesuffix(".0")
        if not work_order:
            continue
        work_order_id = f"work_order:{work_order}"
        canonical_work_order = canonical_nodes.get(work_order_id)
        similarity = float(similarities[int(index)])
        raw_work_order = dict(raw_work_order_records.get(work_order) or {})
        structured_record = dict(structured_records.get(work_order) or {})
        if canonical_work_order is None:
            missing_work_orders.append(work_order)
            graph_nodes[work_order_id] = {
                "id": work_order_id,
                "type": "work_order",
                "canonical_type": "work_order",
                "label": f"工单 {work_order}（未入图）",
                "work_order": work_order,
                "work_orders": [work_order],
                "rank": rank,
                "color": rank_colors[(rank - 1) % len(rank_colors)],
                "similarity": similarity,
                "train": _kg_clean_text(row.get("train")),
                "car_no": _kg_clean_text(row.get("car_no")),
                "fault_time": _kg_clean_text(row.get("fault_time_raw")),
                "raw_work_order": raw_work_order,
                "structured_record": structured_record,
                "signal_keys": list(signal_keys_by_work_order.get(work_order, [])),
                "missing": True,
                "canonical_source": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
            }
            continue

        fault_times = canonical_work_order.get("fault_times") or []
        if not raw_work_order:
            raw_work_order = dict(canonical_work_order.get("raw_work_order") or {})
        graph_nodes[work_order_id] = {
            "id": work_order_id,
            "type": "work_order",
            "canonical_type": "work_order",
            "label": _kg_clean_text(canonical_work_order.get("label")) or f"工单 {work_order}",
            "work_order": work_order,
            "work_orders": [work_order],
            "rank": rank,
            "color": rank_colors[(rank - 1) % len(rank_colors)],
            "similarity": similarity,
            "train": _kg_clean_text(canonical_work_order.get("train")),
            "car_no": _kg_clean_text(canonical_work_order.get("car_no") or row.get("car_no")),
            "fault_time": _kg_clean_text(fault_times[0] if fault_times else row.get("fault_time_raw")),
            "fault_times": [_kg_clean_text(value) for value in fault_times if _kg_clean_text(value)],
            "fault": _kg_clean_text(canonical_work_order.get("fault")),
            "cause": _kg_clean_text(canonical_work_order.get("cause")),
            "action": _kg_clean_text(canonical_work_order.get("maintenance_action")),
            "result": _kg_clean_text(canonical_work_order.get("result")),
            "replace_in_part": list(canonical_work_order.get("replace_in_part") or []),
            "replace_out_part": list(canonical_work_order.get("replace_out_part") or []),
            "raw_work_order": raw_work_order,
            "structured_record": structured_record,
            "signal_keys": list(signal_keys_by_work_order.get(work_order, [])),
            "missing": False,
            "canonical_source": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
        }

        for work_order_edge in maintenance_edges_by_work_order.get(work_order_id, []):
            canonical_method_id = str(work_order_edge.get("source") or "")
            canonical_method = canonical_nodes.get(canonical_method_id)
            if not canonical_method or canonical_method.get("type") != "maintenance":
                continue
            method_actions = _kg_maintenance_action_keys(canonical_method)
            method_id = "method_category:" + ("|".join(method_actions) or "other")
            method_node = graph_nodes.setdefault(
                method_id,
                {
                    "id": method_id,
                    "type": "method",
                    "canonical_type": "maintenance",
                    "label": _kg_maintenance_display_label(canonical_method),
                    "work_orders": [],
                    "canonical_node_ids": [],
                    "canonical_work_orders": [],
                    "original_events": [],
                    "member_methods": [],
                    "affected_equipment": [],
                    "group_basis": _kg_clean_text(canonical_method.get("group_basis")),
                    "canonical_source": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
                },
            )
            append_unique(method_node["work_orders"], work_order)
            append_unique(method_node["canonical_node_ids"], canonical_method_id)
            append_many(method_node["canonical_work_orders"], canonical_method.get("work_orders") or [])
            append_many(method_node["original_events"], canonical_method.get("member_actions") or [])
            append_many(method_node["member_methods"], canonical_method.get("member_methods") or [])
            append_many(method_node["affected_equipment"], canonical_method.get("affected_equipment") or [])
            connect(work_order_id, method_id, work_order, work_order_edge.get("id"))

            for equipment_edge in equipment_edges_by_maintenance.get(canonical_method_id, []):
                edge_work_orders = {_kg_clean_text(value) for value in equipment_edge.get("work_orders", [])}
                if work_order not in edge_work_orders:
                    continue
                component_id = str(equipment_edge.get("source") or "")
                canonical_component = canonical_nodes.get(component_id)
                if not canonical_component or canonical_component.get("type") != "equipment":
                    continue
                component_node = graph_nodes.setdefault(
                    component_id,
                    {
                        "id": component_id,
                        "type": "component",
                        "canonical_type": "equipment",
                        "label": _kg_clean_text(canonical_component.get("label")),
                        "work_orders": [],
                        "canonical_work_orders": list(canonical_component.get("work_orders") or []),
                        "original_components": list(canonical_component.get("member_equipment") or []),
                        "original_events": list(canonical_component.get("roles") or []),
                        "group_basis": _kg_clean_text(canonical_component.get("group_basis")),
                        "canonical_source": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
                    },
                )
                append_unique(component_node["work_orders"], work_order)
                connect(method_id, component_id, work_order, equipment_edge.get("id"))

    work_order_nodes = [
        node for node in graph_nodes.values()
        if node.get("type") == "work_order"
    ]
    if work_order_nodes:
        root_id = "root:vcb_tripped"
        graph_nodes[root_id] = {
            "id": root_id,
            "type": "root",
            "canonical_type": "root",
            "label": "VCB tripped",
            "work_orders": [node["work_order"] for node in work_order_nodes],
            "canonical_source": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
        }
        for work_order_node in work_order_nodes:
            connect(
                root_id,
                work_order_node["id"],
                work_order_node["work_order"],
                f"root_to_work_order:{work_order_node['work_order']}",
            )

    graph_meta = dict(full_graph.get("meta") or {})
    return {
        "nodes": list(graph_nodes.values()),
        "edges": list(graph_edges.values()),
        "meta": {
            "source": "canonical_vcb_trip_graph",
            "source_file": str(VCB_KNOWLEDGE_GRAPH_PATH.name),
            "schema_version": graph_meta.get("schema_version"),
            "generated_at": graph_meta.get("generated_at"),
            "missing_work_orders": missing_work_orders,
            "signal_api_url": (
                DASH_REQUESTS_PATHNAME_PREFIX.rstrip("/")
                + "/api/v1/knowledge-graph/signals"
            ),
            "new_fault": {
                "train": _kg_clean_text(q.get("train")),
                "car_no": _kg_clean_text(q.get("car_no")),
                "fault_time": _kg_clean_text(q.get("fault_time")),
            },
            "new_signal_changes": _new_fault_signal_changes(q),
            "signal_catalog": signal_snapshot["catalog"],
            "excluded_signals": signal_snapshot["excluded_signals"],
        },
    }


def build_top3_work_order_knowledge_graph_document(q: Mapping[str, Any] | None) -> str:
    """Render root -> work order -> action -> component as an SVG graph."""
    if not q or not q.get("top_idx"):
        return _top3_graph_placeholder_document("查询后，这里显示所选相似工单的维修知识图谱")

    graph_payload = build_top3_work_order_knowledge_graph_payload(q)
    if not graph_payload.get("nodes"):
        return _top3_graph_placeholder_document("当前查询没有可展示的相似工单")

    payload = json.dumps(
        graph_payload,
        ensure_ascii=False,
    ).replace("</", "<\\/")
    document = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
:root{--text:#edf5fa;--muted:#91a7b4;--cyan:#6bcdf5;--panel:rgba(52,76,95,.68)}
*{box-sizing:border-box}
html,body{width:100%;height:100%;margin:0;overflow:hidden;background:transparent!important}
body{background:transparent;color:var(--text);font:13px/1.5 "HarmonyOS Sans SC","PingFang SC","Microsoft YaHei",sans-serif}
.shell{display:block;height:100%;min-height:0;background:transparent!important}
.toolbar{position:relative;display:grid;min-height:60px;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:16px;padding:8px 14px;border-bottom:1px solid rgba(49,231,255,.30);background:linear-gradient(90deg,rgba(3,17,38,.97),rgba(7,37,68,.93) 50%,rgba(3,17,38,.97));color:#9ac5d9;font-size:11px;box-shadow:0 5px 22px rgba(0,0,0,.26),inset 0 -1px 0 rgba(49,231,255,.10)}
.toolbar::before{position:absolute;bottom:-1px;left:50%;width:240px;height:2px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);box-shadow:0 0 10px rgba(49,231,255,.48);transform:translateX(-50%);content:""}
.toolbar::after{position:absolute;right:14px;bottom:6px;width:74px;height:1px;background:linear-gradient(90deg,transparent,rgba(49,231,255,.46));content:""}
.legend-group{display:flex;min-width:0;align-items:center;gap:13px}.legend-label{color:#6e99af;font-weight:700;white-space:nowrap}.legend{display:flex;align-items:center;gap:6px;white-space:nowrap}.dot{width:8px;height:8px;border:1px solid #e8fdff;border-radius:50%;box-shadow:0 0 10px currentColor}
.graph-title{text-align:center;white-space:nowrap}.graph-title b{display:block;color:#e8faff;font-size:16px;letter-spacing:.4px;line-height:1.2}.graph-title small{display:block;margin-top:3px;color:#5d96b4;font-size:8px;letter-spacing:1.5px}
.selection{justify-self:end;max-width:330px;overflow:hidden;color:#668da7;text-align:right;text-overflow:ellipsis;white-space:nowrap}.selection b{color:#8ce9ff;font-weight:700}
.viewport{position:relative;width:100%;height:100%;min-height:0;overflow:auto;scrollbar-width:thin;scrollbar-color:rgba(117,170,197,.38) transparent;background:transparent!important}
.viewport::after{display:none}
.viewport::-webkit-scrollbar{width:6px;height:6px}
.viewport::-webkit-scrollbar-track{background:transparent}
.viewport::-webkit-scrollbar-thumb{border-radius:999px;background:rgba(117,170,197,.35)}
.viewport::-webkit-scrollbar-thumb:hover{background:rgba(117,170,197,.52)}
.viewport::-webkit-scrollbar-corner{background:transparent}
svg{position:relative;z-index:1;display:block;width:100%;min-width:0;height:auto}
.column-title{font-size:15px;font-weight:800;letter-spacing:2px}.column-sub{font-size:9px;fill:#5f8eab;letter-spacing:1.5px}
.column-panel{display:none}.edge-glow{display:none}.edge{fill:none;stroke:rgba(194,224,239,.22);stroke-width:.85}.edge.highlight{stroke:rgba(120,215,255,.90);stroke-width:1.7;filter:none}.edge-glow.highlight{display:none}
.node{cursor:pointer}.node .frame{fill:rgba(255,255,255,.10);stroke:rgba(245,252,255,.45);stroke-width:1.05;filter:drop-shadow(0 8px 20px rgba(0,0,0,.24)) drop-shadow(0 0 14px rgba(128,222,255,.12));transition:stroke .18s ease,fill .18s ease,filter .18s ease}.node .frame-sheen{fill:url(#nodeSheen);opacity:1;pointer-events:none}.node .frame-noise{fill:url(#nodeNoise);opacity:.22;pointer-events:none;mix-blend-mode:screen}.node .frame-inner{fill:none;stroke:rgba(255,255,255,.17);stroke-width:.65;pointer-events:none}.node .rim{fill:none;stroke:rgba(226,248,255,.30);stroke-width:.8;pointer-events:none}.node .frame-highlight{fill:none;stroke:rgba(255,255,255,.46);stroke-width:.9;stroke-linecap:round;pointer-events:none;filter:drop-shadow(0 0 3px rgba(202,245,255,.32))}.node .port{fill:rgba(225,247,255,.86);stroke:rgba(16,61,85,.36);stroke-width:.4}.node:hover .frame{fill:rgba(255,255,255,.145);stroke:rgba(245,253,255,.64);filter:drop-shadow(0 9px 20px rgba(0,0,0,.25)) drop-shadow(0 0 14px rgba(128,222,255,.20))}
.node text{pointer-events:none}.node-title{fill:rgba(250,253,255,.99);stroke:rgba(2,18,31,.44);stroke-width:.38px;paint-order:stroke fill;font-size:14.5px;font-weight:600;letter-spacing:.05px;filter:drop-shadow(0 1px 1px rgba(0,0,0,.25))}.node-sub{fill:rgba(220,237,245,.88);stroke:rgba(2,18,31,.34);stroke-width:.28px;paint-order:stroke fill;font-size:11.2px;font-weight:500;letter-spacing:.08px}
.node-icon-disc{fill:rgba(24,55,73,.60);stroke:rgba(211,241,253,.25);stroke-width:1}.node-icon-line{fill:none;stroke:#ecf9fe;stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round;pointer-events:none;filter:drop-shadow(0 1px 1px rgba(0,0,0,.24))}.rank-badge{fill:rgba(16,44,62,.68);stroke:rgba(213,240,252,.24);stroke-width:1}.rank-number{fill:#f8fdff;stroke:rgba(2,18,31,.42);stroke-width:.3px;paint-order:stroke fill;font-size:13px;font-weight:600;text-anchor:middle;dominant-baseline:middle}
.node.root .frame{fill:rgba(169,235,255,.13);stroke:rgba(155,234,255,.62);stroke-width:1.2}.node.root .node-title{fill:#fbfeff}.node.root .node-sub{fill:rgba(170,238,255,.96);letter-spacing:.7px}.node.selected .frame{fill:rgba(255,255,255,.17);stroke:rgba(180,240,255,.92);stroke-width:1.55;filter:drop-shadow(0 8px 20px rgba(0,0,0,.24)) drop-shadow(0 0 14px rgba(112,220,255,.28))}.node.selected .rim{stroke:rgba(255,255,255,.40)}.node.selected .frame-inner{stroke:rgba(255,255,255,.24)}.node.selected .node-title{fill:#fff}.node.missing .frame{stroke:rgba(255,194,133,.72);stroke-dasharray:5 4;fill:rgba(255,208,167,.10)}.node.faded,.edge.faded,.edge-glow.faded{opacity:.14}
.detail-button{cursor:pointer}.detail-button-circle{fill:rgba(7,35,61,.94);stroke:rgba(114,232,255,.88);stroke-width:1;transition:fill .14s ease,stroke .14s ease,filter .14s ease}.detail-button-line{fill:none;stroke:#72e8ff;stroke-width:1.35;stroke-linecap:round;pointer-events:none}.detail-button-dot{fill:#72e8ff;filter:drop-shadow(0 0 2px #21d9ff);pointer-events:none}.detail-button:hover .detail-button-circle,.detail-button:focus .detail-button-circle{fill:rgba(18,82,113,.98);stroke:#effdff;filter:drop-shadow(0 0 5px rgba(49,231,255,.68))}.detail-button:hover .detail-button-line{stroke:#fff}.detail-button:focus{outline:none}
.signal-button{cursor:pointer}.signal-button-circle{fill:rgba(25,15,54,.96);stroke:rgba(182,116,255,.92);stroke-width:1;transition:fill .14s ease,stroke .14s ease,filter .14s ease}.signal-button-wave{fill:none;stroke:#c49aff;stroke-width:1.25;stroke-linecap:round;stroke-linejoin:round;pointer-events:none;filter:drop-shadow(0 0 2px rgba(181,111,255,.9))}.signal-button:hover .signal-button-circle,.signal-button:focus .signal-button-circle{fill:rgba(76,37,113,.98);stroke:#f5eaff;filter:drop-shadow(0 0 6px rgba(181,111,255,.78))}.signal-button:hover .signal-button-wave{stroke:#fff}.signal-button:focus{outline:none}
.work-order-modal{position:fixed;z-index:50;inset:0;display:none;place-items:center;padding:24px}.work-order-modal.open{display:grid}.detail-backdrop{position:absolute;inset:0;background:rgba(0,5,15,.78);backdrop-filter:blur(3px)}
.node.work_order .signal-button,.node.work_order .detail-button{display:none}
.detail-dialog{position:relative;display:flex;width:min(920px,calc(100vw - 48px));max-height:calc(100vh - 48px);flex-direction:column;overflow:hidden;border:1px solid rgba(205,230,242,.14);border-radius:12px;background:linear-gradient(135deg,rgba(255,255,255,.055),transparent 32%),rgba(8,28,46,.58);box-shadow:0 22px 70px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.065);backdrop-filter:blur(16px) saturate(125%)}
.detail-head{display:flex;min-height:58px;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid rgba(49,231,255,.24);background:linear-gradient(90deg,rgba(11,55,86,.78),rgba(4,23,44,.88))}.detail-head-mark{width:9px;height:9px;flex:0 0 9px;border:1px solid #effcff;background:var(--pink);box-shadow:0 0 9px var(--pink);transform:rotate(45deg)}.detail-title-wrap{min-width:0;flex:1}.detail-kicker{color:#6da4bd;font-size:9px;letter-spacing:2px}.detail-title{margin:2px 0 0;color:#f2fbff;font-size:18px;line-height:1.2}.detail-close{display:grid;width:30px;height:30px;place-items:center;border:1px solid rgba(49,231,255,.32);background:rgba(4,24,44,.8);color:#9bdcea;cursor:pointer;font-size:20px;line-height:1}.detail-close:hover{border-color:var(--cyan);color:#fff;background:rgba(16,78,108,.8)}
.detail-content{min-height:0;overflow:auto;padding:14px 16px 18px;scrollbar-width:thin;scrollbar-color:rgba(49,231,255,.56) rgba(2,12,27,.72)}.detail-section+ .detail-section,.detail-disclosure+ .detail-section,.detail-section+ .detail-disclosure,.detail-disclosure+ .detail-disclosure{margin-top:12px}.detail-section-title{display:flex;align-items:center;gap:8px;margin:0 0 8px;color:#9eeafa;font-size:12px;font-weight:700;letter-spacing:.5px}.detail-section-title::before{width:3px;height:12px;background:var(--cyan);box-shadow:0 0 7px rgba(49,231,255,.65);content:""}.detail-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.detail-grid.overview{grid-template-columns:repeat(3,minmax(0,1fr))}.detail-field{min-width:0;padding:8px 10px;border:1px solid rgba(69,157,204,.18);background:rgba(5,30,53,.60)}.detail-field.wide{grid-column:1/-1}.detail-label{margin-bottom:3px;color:#5f91aa;font-size:9px;letter-spacing:.45px}.detail-value{overflow-wrap:anywhere;color:#d8eef6;font-family:var(--font-data);font-size:10.8px;line-height:1.5;white-space:pre-wrap}.detail-field.long-description{max-height:210px;overflow:auto;border-color:rgba(245,200,76,.25);background:rgba(45,36,13,.25)}.detail-field.long-description .detail-label{color:#ddbd64}.detail-disclosure{overflow:hidden;border:1px solid rgba(48,141,190,.22);background:rgba(3,20,38,.62)}.detail-disclosure>summary{display:flex;min-height:38px;align-items:center;gap:9px;padding:8px 11px;color:#8fcfe0;cursor:pointer;font-size:11px;font-weight:700;list-style:none;user-select:none}.detail-disclosure>summary::-webkit-details-marker{display:none}.detail-disclosure>summary::before{width:6px;height:6px;border-right:1px solid #68daf1;border-bottom:1px solid #68daf1;transform:rotate(-45deg);transition:transform .15s ease;content:""}.detail-disclosure[open]>summary::before{transform:rotate(45deg) translateY(-2px)}.detail-disclosure>summary::after{margin-left:auto;color:#4f849d;font-family:var(--font-data);font-size:8px;font-weight:500;letter-spacing:1px;content:"EXPAND"}.detail-disclosure[open]>summary::after{content:"COLLAPSE"}.detail-disclosure-body{padding:0 10px 10px;border-top:1px solid rgba(49,231,255,.10)}.detail-disclosure .detail-grid{padding-top:9px}.detail-empty{padding:16px;border:1px dashed rgba(86,145,177,.25);color:#668ea4;text-align:center}
.signal-compare-modal{position:fixed;z-index:60;inset:0;display:none;place-items:center;padding:22px}.signal-compare-modal.open{display:grid}.signal-compare-backdrop{position:absolute;inset:0;background:rgba(0,8,14,.76);backdrop-filter:blur(5px)}.signal-compare-dialog{position:relative;display:flex;width:min(1040px,calc(100vw - 44px));max-height:calc(100vh - 44px);flex-direction:column;overflow:hidden;border:1px solid rgba(205,230,242,.14);border-radius:12px;background:linear-gradient(135deg,rgba(255,255,255,.055),transparent 32%),rgba(8,28,46,.58);box-shadow:0 24px 78px rgba(0,0,0,.50),inset 0 1px 0 rgba(255,255,255,.065);backdrop-filter:blur(16px) saturate(125%)}
.signal-compare-content{min-height:0;overflow:auto;padding:14px 16px 18px;scrollbar-width:thin;scrollbar-color:rgba(157,103,255,.64) rgba(2,12,27,.72)}.signal-summary{display:grid;grid-template-columns:1.35fr repeat(3,minmax(92px,.65fr));gap:8px;margin-bottom:12px}.signal-summary-main,.signal-kpi{min-width:0;border:1px solid rgba(80,173,221,.20);background:linear-gradient(145deg,rgba(8,43,72,.72),rgba(5,25,47,.70));padding:9px 11px}.signal-summary-main{border-color:rgba(157,103,255,.34)}.signal-summary-label,.signal-kpi-label{color:#668fa8;font-size:9px;letter-spacing:.7px}.signal-summary-value{margin-top:2px;color:#d9f9ff;font-size:12px}.signal-kpi-value{margin-top:1px;color:#d9f9ff;font:700 16px/1.25 ui-monospace,SFMono-Regular,Consolas,monospace}.signal-kpi.accent .signal-kpi-value{color:#55f3ca;text-shadow:0 0 9px rgba(54,255,194,.32)}
.signal-columns,.signal-row{display:grid;grid-template-columns:minmax(0,1fr) 46px minmax(0,1fr);gap:8px}.signal-columns{position:sticky;z-index:2;top:-14px;margin:0 0 7px;padding:10px 0 7px;background:linear-gradient(180deg,rgba(4,15,32,.99) 75%,transparent)}.signal-column-title{color:#93dff0;font-size:11px;font-weight:700;letter-spacing:.7px}.signal-column-title:last-child{text-align:right}.signal-row{align-items:stretch;margin-bottom:7px}.signal-card{position:relative;min-width:0;min-height:54px;padding:8px 10px;border:1px solid rgba(205,230,242,.09);background:linear-gradient(135deg,rgba(255,255,255,.04),transparent 40%),rgba(93,120,138,.20);transition:background .16s ease,border .16s ease,box-shadow .16s ease}.signal-card.right{text-align:right}.signal-card.match{border-color:rgba(54,255,194,.38);background:linear-gradient(120deg,rgba(12,78,72,.42),rgba(93,120,138,.24));box-shadow:inset 0 0 16px rgba(54,255,194,.06)}.signal-card.variant{border-color:rgba(255,211,78,.20);background:rgba(93,120,138,.14)}.signal-card.empty{display:grid;place-items:center;border-style:dashed;background:rgba(93,120,138,.08);color:#3e667c;font-size:10px}.signal-name{overflow-wrap:anywhere;color:#d5edf5;font-size:10.7px;font-weight:700;line-height:1.35}.signal-values{margin-top:4px;color:#73a4bc;font:9.5px/1.25 ui-monospace,SFMono-Regular,Consolas,monospace}.signal-card.match .signal-name{color:#afffe8}.signal-card.match .signal-values{color:#65e7c5}.signal-relation{display:grid;place-items:center;color:#456e86;font-size:15px}.signal-relation.match{color:#48ffc1;text-shadow:0 0 8px rgba(54,255,194,.7)}.signal-relation.variant{color:#f1c958}.signal-loading{display:grid;min-height:280px;place-items:center;color:#85c9dc;text-align:center}.signal-loading::before{width:28px;height:28px;margin:0 auto 12px;border:2px solid rgba(157,103,255,.28);border-top-color:#ae82ff;border-radius:50%;animation:signalSpin .8s linear infinite;content:""}.signal-note{margin-top:10px;color:#567f96;font-size:9px;text-align:right}.signal-error{padding:30px;border:1px dashed rgba(255,91,142,.34);color:#e68aa9;text-align:center}
.metrics{display:grid;min-height:0;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;padding:8px 10px 10px;border-top:1px solid rgba(49,231,255,.22);background:linear-gradient(180deg,rgba(4,22,43,.95),rgba(2,12,27,.98));box-shadow:0 -8px 24px rgba(0,0,0,.18)}.metric{position:relative;display:grid;min-width:0;grid-template-columns:48px minmax(0,1fr);grid-template-rows:auto 1fr;align-items:center;padding:9px 14px;border:1px solid rgba(54,139,190,.20);background:linear-gradient(145deg,rgba(7,34,59,.72),rgba(3,19,37,.78))}.metric+.metric{border-left-color:rgba(54,139,190,.12)}.metric-icon{display:grid;width:38px;height:38px;grid-row:1/3;place-items:center;border:1px solid currentColor;border-radius:50%;color:var(--metric-color,#31e7ff);font:700 16px/1 ui-monospace,SFMono-Regular,Consolas,monospace;box-shadow:inset 0 0 12px color-mix(in srgb,var(--metric-color,#31e7ff) 12%,transparent),0 0 10px color-mix(in srgb,var(--metric-color,#31e7ff) 10%,transparent)}.metric-label{overflow:hidden;color:#8fb9ca;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.metric-value{align-self:end;color:var(--metric-color,#5ee8ff);font:800 24px/1.05 ui-monospace,SFMono-Regular,Consolas,monospace;text-shadow:0 0 10px color-mix(in srgb,var(--metric-color,#31e7ff) 24%,transparent)}.metric-note{grid-column:2;color:#527b90;font-size:8.5px;white-space:nowrap}
@keyframes flow{to{stroke-dashoffset:-110}}
@keyframes portPulse{50%{opacity:.55;r:2.5}}
@keyframes scanline{0%,100%{transform:translateY(0);opacity:0}14%{opacity:.24}50%{transform:translateY(calc(100vh - 52px));opacity:.08}51%{opacity:0}}
@keyframes signalSpin{to{transform:rotate(360deg)}}
@media(max-width:1100px){.selection{display:none}.toolbar{gap:10px}.detail-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:680px){.work-order-modal,.signal-compare-modal{padding:8px}.detail-dialog,.signal-compare-dialog{width:calc(100vw - 16px);max-height:calc(100vh - 16px)}.detail-grid{grid-template-columns:1fr}.signal-summary{grid-template-columns:repeat(3,1fr)}.signal-summary-main{grid-column:1/-1}.signal-columns,.signal-row{grid-template-columns:minmax(0,1fr) 28px minmax(0,1fr)}}
@media(prefers-reduced-motion:reduce){.edge,.node .port,.viewport::after{animation:none}}
</style>
</head>
<body>
<div class="shell">
  <span class="selection" id="selection" hidden></span>
  <div class="viewport"><svg id="graph" viewBox="0 0 1080 900" preserveAspectRatio="xMidYMin meet"></svg></div>
  <div id="metrics" hidden></div>
</div>
<div class="work-order-modal" id="workOrderModal" aria-hidden="true">
  <div class="detail-backdrop" data-detail-close></div>
  <section class="detail-dialog" role="dialog" aria-modal="true" aria-labelledby="workOrderDetailTitle">
    <header class="detail-head">
      <i class="detail-head-mark"></i>
      <div class="detail-title-wrap"><div class="detail-kicker">WORK ORDER DETAIL</div><h2 class="detail-title" id="workOrderDetailTitle">工单详情</h2></div>
      <button class="detail-close" id="workOrderDetailClose" type="button" aria-label="关闭">×</button>
    </header>
    <div class="detail-content" id="workOrderDetailContent"></div>
  </section>
</div>
<div class="signal-compare-modal" id="signalCompareModal" aria-hidden="true">
  <div class="signal-compare-backdrop" data-signal-close></div>
  <section class="signal-compare-dialog" role="dialog" aria-modal="true" aria-labelledby="signalCompareTitle">
    <header class="detail-head">
      <i class="detail-head-mark" style="background:#b77aff;box-shadow:0 0 9px #b77aff"></i>
      <div class="detail-title-wrap"><div class="detail-kicker">SIGNAL CHANGE COMPARISON</div><h2 class="detail-title" id="signalCompareTitle">信号变化对比</h2></div>
      <button class="detail-close" id="signalCompareClose" type="button" aria-label="关闭">×</button>
    </header>
    <div class="signal-compare-content" id="signalCompareContent"></div>
  </section>
</div>
<script>
const DATA=__DATA__;
const NS="http://www.w3.org/2000/svg";
const svg=document.getElementById("graph"),selection=document.getElementById("selection"),metrics=document.getElementById("metrics"),detailModal=document.getElementById("workOrderModal"),detailTitle=document.getElementById("workOrderDetailTitle"),detailContent=document.getElementById("workOrderDetailContent"),signalModal=document.getElementById("signalCompareModal"),signalTitle=document.getElementById("signalCompareTitle"),signalContent=document.getElementById("signalCompareContent");
const WIDTH=1080,NODE_H=86,X={root:16,work_order:190,method:490,component:800},W={root:142,work_order:195,method:225,component:245};
const COLORS={root:"#79d5fa",work_order:"#8ebed5",method:"#8ebed5",component:"#8ebed5"};
const nodeById=new Map(DATA.nodes.map(node=>[node.id,node])),edgeById=new Map(DATA.edges.map(edge=>[edge.id,edge]));
const selectedWorkOrders=new Set();let selectedNode="",activeSummaryWorkOrder="";
function el(name,attrs={},text=""){const node=document.createElementNS(NS,name);Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,v));if(text)node.textContent=text;return node}
  function distribute(count,top,height,maxGap=86){if(count<=0)return[];if(count===1)return[top+(height-NODE_H)/2];const gap=Math.min(maxGap,(height-NODE_H)/(count-1));const used=(count-1)*gap;const start=top+(height-NODE_H-used)/2;return Array.from({length:count},(_,i)=>start+i*gap)}
function lines(value,max=27,limit=3){const raw=String(value||"");const chunks=raw.split(" / ");const out=[];let line="";for(const chunk of chunks){const next=line?line+" / "+chunk:chunk;if(next.length<=max)line=next;else{if(line)out.push(line);line=chunk}}if(line)out.push(line);if(out.length===1&&out[0].length>max){const value=out[0];out.length=0;for(let i=0;i<value.length;i+=max)out.push(value.slice(i,i+max))}if(out.length>limit){out.length=limit;out[limit-1]=out[limit-1].slice(0,Math.max(1,max-1))+"…"}return out}
function addText(parent,x,y,value,cls,max=27,limit=3){const text=el("text",{x,y,class:cls});lines(value,max,limit).forEach((line,i)=>text.appendChild(el("tspan",{x,dy:i?14:0},line)));parent.appendChild(text)}
  function nodeSub(node){if(node.type==="work_order")return `车号 ${node.train||"-"}  ·  车厢 ${node.car_no||"-"}`;return ""}
function valueText(value){if(value===null||value===undefined)return"";if(Array.isArray(value))return value.map(valueText).filter(Boolean).join(" / ");if(typeof value==="object")return Object.entries(value).map(([key,item])=>`${key}：${valueText(item)}`).filter(line=>!line.endsWith("：")).join("\n");const text=String(value).trim();return /^(n\/?a|none|null|nan)$/i.test(text)?"":text}
function addField(grid,label,value,options={}){const text=valueText(value);if(!text)return false;const field=document.createElement("div");field.className=`detail-field${options.wide?" wide":""}${options.long?" long-description":""}`;const name=document.createElement("div");name.className="detail-label";name.textContent=label;const content=document.createElement("div");content.className="detail-value";content.textContent=text;field.append(name,content);grid.appendChild(field);return true}
function addSection(title,fields,options={}){const section=document.createElement("section");section.className="detail-section";const heading=document.createElement("h3");heading.className="detail-section-title";heading.textContent=title;const grid=document.createElement("div");grid.className=`detail-grid${options.gridClass?` ${options.gridClass}`:""}`;let count=0;fields.forEach(field=>{if(addField(grid,field[0],field[1],field[2]||{}))count++});if(!count)return;section.append(heading,grid);detailContent.appendChild(section)}
function addDisclosure(title,fields,options={}){const disclosure=document.createElement("details");disclosure.className="detail-disclosure";disclosure.open=Boolean(options.open);const summary=document.createElement("summary");summary.textContent=title;const body=document.createElement("div");body.className="detail-disclosure-body";const grid=document.createElement("div");grid.className="detail-grid";let count=0;fields.forEach(field=>{if(addField(grid,field[0],field[1],field[2]||{}))count++});if(!count)return;body.appendChild(grid);disclosure.append(summary,body);detailContent.appendChild(disclosure)}
function combinedValues(...values){const result=[];const append=value=>{if(Array.isArray(value)){value.forEach(append);return}const text=valueText(value);if(text&&!result.includes(text))result.push(text)};values.forEach(append);return result}
function distinctParts(...values){const result=[],seen=new Set();const append=value=>{if(Array.isArray(value)){value.forEach(append);return}valueText(value).split(/\s*(?:\/|,|、|;)\s*/).filter(Boolean).forEach(part=>{const key=part.toLowerCase();if(!seen.has(key)){seen.add(key);result.push(part)}})};values.forEach(append);return result}
function codeDescription(code,description){return combinedValues(code,description).join(" · ")}
const STRUCTURED_LABELS={work_order_no:"工单号",description:"工单描述",fault:"故障现象",cause:"故障原因",action:"维修措施",result:"维修结果",component:"涉及部件",components:"涉及部件",equipment:"涉及设备",maintenance_action:"维修措施",long_description:"Long Description",failure_long_description:"Failure Long Description",replace_in_part:"换入件",replace_out_part:"换出件",problem:"问题",remedy:"处理方法",fault_time:"故障时间",train:"车号",car_no:"车厢号"};
function openWorkOrderDetail(node){const raw=node.raw_work_order||{},structured=node.structured_record||{};detailTitle.textContent=`工单 ${node.work_order||node.label||""}`;detailContent.innerHTML="";const similarity=`Top ${node.rank} · ${(Number(node.similarity||0)*100).toFixed(1)}%`;const vehicle=codeDescription(node.train||raw.Trainset,(node.car_no||raw["CarNO."])?`${node.car_no||raw["CarNO."]} 车厢`:"");const location=codeDescription(raw.Location,raw["LOCATION.DESCRIPTION"]);addSection("工单概览",[["工单号",node.work_order],["匹配结果",similarity],["车辆位置",vehicle],["故障时间",node.fault_times?.length?node.fault_times:node.fault_time],["状态 / 类型",codeDescription(raw.Status,raw["Work Type"])],["地点",location]],{gridClass:"overview"});addSection("故障与处置",[["工单描述",raw.Description||structured.description,{wide:true}],["故障现象",node.fault||structured.fault,{wide:true}],["故障原因",node.cause||structured.cause,{wide:true}],["维修措施",node.action||structured.action||structured.maintenance_action,{wide:true}],["维修结果",node.result||structured.result,{wide:true}]]);const relatedComponents=distinctParts(structured.components,structured.component,structured.phenomenon_component,structured.cause_components,structured.treatment_components);addSection("部件与换件",[["涉及部件",relatedComponents,{wide:true}],["换入件",node.replace_in_part],["换出件",node.replace_out_part],["故障编码",raw["Failure Code"]]]);addDisclosure("原始工单记录（Long Description）",[["Long Description",raw["Long Description"]||structured.long_description,{wide:true,long:true}],["Failure Long Description",raw["Failure Long Description"]||structured.failure_long_description,{wide:true,long:true}],["Remark Description",raw["Remark Description"],{wide:true}]]);const structuredUsed=new Set(["work_order_no","work_order","description","long_description","failure_long_description","fault","cause","action","maintenance_action","result","train","car_no","fault_time","components","component","phenomenon_component","cause_components","treatment_components","replace_in_part","replace_out_part"]);const analysisFields=[["问题编码",codeDescription(raw["Problem Code"],raw["Problem Desc"])],["原因编码",codeDescription(raw["Cause Code"],raw["Cause Desc"])],["处理编码",codeDescription(raw["Remedy Code"],raw["Remedy Desc"]),{wide:true}],...Object.entries(structured).filter(([key])=>!structuredUsed.has(key)).map(([key,value])=>[STRUCTURED_LABELS[key]||key.replaceAll("_"," "),value,{wide:typeof value==="object"||String(value||"").length>90}])];addDisclosure("编码与结构化依据",analysisFields);const rawUsed=new Set(["Description","Long Description","Failure Long Description","Remark Description","Work Order NO.","Trainset","CarNO.","Status","Work Type","Location","LOCATION.DESCRIPTION","Failure Code","Problem Code","Problem Desc","Cause Code","Cause Desc","Remedy Code","Remedy Desc"]);addDisclosure("更多工单字段",Object.entries(raw).filter(([key])=>!rawUsed.has(key)).map(([key,value])=>[key,value,{wide:String(value||"").length>90}]));if(!detailContent.children.length){const empty=document.createElement("div");empty.className="detail-empty";empty.textContent="该工单暂未记录详细信息";detailContent.appendChild(empty)}detailModal.classList.add("open");detailModal.setAttribute("aria-hidden","false");document.getElementById("workOrderDetailClose").focus()}
function closeWorkOrderDetail(){detailModal.classList.remove("open");detailModal.setAttribute("aria-hidden","true")}
document.getElementById("workOrderDetailClose").addEventListener("click",closeWorkOrderDetail);detailModal.querySelector("[data-detail-close]").addEventListener("click",closeWorkOrderDetail);document.addEventListener("keydown",event=>{if(event.key==="Escape"&&detailModal.classList.contains("open"))closeWorkOrderDetail()});
function closeSignalComparison(){signalModal.classList.remove("open");signalModal.setAttribute("aria-hidden","true")}
document.getElementById("signalCompareClose").addEventListener("click",closeSignalComparison);signalModal.querySelector("[data-signal-close]").addEventListener("click",closeSignalComparison);
function htmlNode(tag,className="",text=""){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined&&text!==null)node.textContent=String(text);return node}
function signalKey(value){return String(value||"").trim()}
function isExcludedSignal(signal,excluded){const haystack=[signal.raw_node,signal.dimension,signal.description,signal.normalized_dimension,signal.normalized_signal_key].map(value=>signalKey(value).toLowerCase()).join(" ");return (excluded||[]).some(value=>{const term=signalKey(value).toLowerCase();return term&&haystack.includes(term)})}
function mapNewSignalChanges(catalog,excluded){const bySource=new Map();(catalog||[]).forEach(signal=>(signal.source_normalized_nodes||[]).forEach(source=>bySource.set(signalKey(source),signal)));const merged=new Map();(DATA.meta.new_signal_changes||[]).forEach(change=>{const canonical=bySource.get(signalKey(change.raw_node));const mapped=canonical?{...canonical,count:Number(change.count||1),raw_nodes:[change.raw_node]}:{normalized_signal_key:`raw:${signalKey(change.raw_node)}`,normalized_dimension:change.description||change.dimension||change.raw_node,previous_value:change.previous_value,current_value:change.current_value,count:Number(change.count||1),raw_nodes:[change.raw_node]};if(isExcludedSignal(mapped,excluded))return;const key=signalKey(mapped.normalized_signal_key);if(merged.has(key)){const current=merged.get(key);current.count+=mapped.count;current.raw_nodes.push(...mapped.raw_nodes)}else merged.set(key,mapped)});return [...merged.values()]}
function comparisonRows(newSignals,historySignals){const historyByKey=new Map(historySignals.map(signal=>[signalKey(signal.normalized_signal_key),signal]));const usedHistory=new Set(),rows=[];newSignals.forEach(signal=>{const key=signalKey(signal.normalized_signal_key),history=historyByKey.get(key);if(history){usedHistory.add(key);rows.push({left:signal,right:history,state:"match"})}});const leftRest=newSignals.filter(signal=>!historyByKey.has(signalKey(signal.normalized_signal_key)));const rightRest=historySignals.filter(signal=>!usedHistory.has(signalKey(signal.normalized_signal_key)));const usedRight=new Set();leftRest.forEach(left=>{const dimension=signalKey(left.normalized_dimension).toLowerCase();const index=rightRest.findIndex((right,i)=>!usedRight.has(i)&&dimension&&signalKey(right.normalized_dimension).toLowerCase()===dimension);if(index>=0){usedRight.add(index);rows.push({left,right:rightRest[index],state:"variant"})}else rows.push({left,right:null,state:"different"})});rightRest.forEach((right,index)=>{if(!usedRight.has(index))rows.push({left:null,right,state:"different"})});return rows}
function signalCard(signal,side,state){if(!signal)return htmlNode("div",`signal-card ${side} empty`,"无对应变化");const card=htmlNode("div",`signal-card ${side} ${state}`);const name=htmlNode("div","signal-name",signal.normalized_dimension||signal.description||signal.raw_node||"未命名信号");const values=htmlNode("div","signal-values",`${signalKey(signal.previous_value)||"∅"}  →  ${signalKey(signal.current_value)||"∅"}`);card.append(name,values);return card}
function addSignalMetric(parent,label,value,className=""){const card=htmlNode("div",`signal-kpi ${className}`);card.append(htmlNode("div","signal-kpi-label",label),htmlNode("div","signal-kpi-value",value));parent.appendChild(card)}
function renderSignalComparison(node,snapshot,usedFallback=false){const catalog=Array.isArray(snapshot.catalog)?snapshot.catalog:[],excluded=Array.isArray(snapshot.excluded_signals)?snapshot.excluded_signals:(DATA.meta.excluded_signals||[]),newSignals=mapNewSignalChanges(catalog,excluded);const selectedKeys=new Set((snapshot.work_order_signal_keys||node.signal_keys||[]).map(signalKey));const historySignals=catalog.filter(signal=>selectedKeys.has(signalKey(signal.normalized_signal_key))&&!isExcludedSignal(signal,excluded));const rows=comparisonRows(newSignals,historySignals);const matched=rows.filter(row=>row.state==="match").length;const union=new Set([...newSignals,...historySignals].map(signal=>signalKey(signal.normalized_signal_key))).size;const similarity=union?matched/union:0;signalContent.innerHTML="";const summary=htmlNode("div","signal-summary"),main=htmlNode("div","signal-summary-main");main.append(htmlNode("div","signal-summary-label","对比对象"),htmlNode("div","signal-summary-value",`${DATA.meta.new_fault?.train||"新故障"} / ${DATA.meta.new_fault?.car_no||"-"}  ↔  工单 ${node.work_order}`));summary.appendChild(main);addSignalMetric(summary,"信号相似度",`${(similarity*100).toFixed(1)}%`,"accent");addSignalMetric(summary,"完全相同",`${matched} / ${union}`);addSignalMetric(summary,"信号数量",`${newSignals.length} / ${historySignals.length}`);signalContent.appendChild(summary);const headers=htmlNode("div","signal-columns");headers.append(htmlNode("div","signal-column-title","新故障信号变化"),htmlNode("div",""),htmlNode("div","signal-column-title",`历史工单 ${node.work_order}`));signalContent.appendChild(headers);if(!rows.length){signalContent.appendChild(htmlNode("div","signal-error","当前新故障或该历史工单没有可用于对比的标准信号变化。"))}else rows.forEach(row=>{const line=htmlNode("div","signal-row"),relation=htmlNode("div",`signal-relation ${row.state}`,row.state==="match"?"≡":row.state==="variant"?"≈":"·");line.append(signalCard(row.left,"left",row.state),relation,signalCard(row.right,"right",row.state));signalContent.appendChild(line)});const sourceStatus=usedFallback?"接口暂不可用，当前显示查询时保存的知识图谱快照。":"已在点击时重新读取完整知识图谱。";signalContent.appendChild(htmlNode("div","signal-note",`${sourceStatus} 版本：${snapshot.schema_version||"-"} · 生成时间：${snapshot.generated_at||"-"}`))}
async function openSignalComparison(node){closeWorkOrderDetail();signalTitle.textContent=`信号变化对比 · 工单 ${node.work_order||node.label||""}`;signalContent.innerHTML="";signalContent.appendChild(htmlNode("div","signal-loading","正在读取最新版完整知识图谱并匹配信号…"));signalModal.classList.add("open");signalModal.setAttribute("aria-hidden","false");let snapshot=null,usedFallback=false;try{const url=new URL(DATA.meta.signal_api_url,document.baseURI);url.searchParams.set("work_order",node.work_order);url.searchParams.set("_",Date.now());const response=await fetch(url,{cache:"no-store",headers:{Accept:"application/json"}});if(!response.ok)throw new Error(`HTTP ${response.status}`);snapshot=await response.json();if(!snapshot.ok)throw new Error(snapshot.error||"知识图谱读取失败")}catch(error){usedFallback=true;snapshot={catalog:DATA.meta.signal_catalog||[],excluded_signals:DATA.meta.excluded_signals||[],work_order_signal_keys:node.signal_keys||[],schema_version:"查询快照",generated_at:"-"}}if(signalModal.classList.contains("open"))renderSignalComparison(node,snapshot,usedFallback)}
document.addEventListener("keydown",event=>{if(event.key!=="Escape")return;if(signalModal.classList.contains("open"))closeSignalComparison();else if(detailModal.classList.contains("open"))closeWorkOrderDetail()});
function bindNode(g,node){g.addEventListener("click",()=>{if(node.type==="work_order"){selectedNode="";activeSummaryWorkOrder=node.work_order;selectedWorkOrders.has(node.work_order)?selectedWorkOrders.delete(node.work_order):selectedWorkOrders.add(node.work_order);publishDashboardSummary(node)}else{selectedWorkOrders.clear();selectedNode=selectedNode===node.id?"":node.id}applyHighlight()})}
  function gearPath(cx,cy,teeth,outerRadius,rootRadius){
    const points=[];
    for(let index=0;index<teeth*4;index+=1){
      const phase=index%4,radius=(phase===1||phase===2)?outerRadius:rootRadius;
      const angle=-Math.PI/2+(Math.PI*2*index)/(teeth*4);
      points.push(`${(cx+Math.cos(angle)*radius).toFixed(2)} ${(cy+Math.sin(angle)*radius).toFixed(2)}`);
    }
    return `M${points.join("L")}Z`;
  }
  function createNode(node,pos){
    const kind=node.type,color=node.color||COLORS[kind],width=W[kind];
    const g=el("g",{class:`node ${kind}${node.missing?" missing":""}`,transform:`translate(${pos.x},${pos.y})`,"data-id":node.id,style:`color:${color}`});
    const frame=el("rect",{class:"frame",x:0,y:0,width,height:NODE_H,rx:8});
    const sheen=el("rect",{class:"frame-sheen",x:1,y:1,width:width-2,height:42,rx:7});
    const noise=el("rect",{class:"frame-noise",x:1,y:1,width:width-2,height:NODE_H-2,rx:7});
    const inner=el("rect",{class:"frame-inner",x:1.25,y:1.25,width:width-2.5,height:NODE_H-2.5,rx:7});
    const rim=el("rect",{class:"rim",x:.8,y:.8,width:width-1.6,height:NODE_H-1.6,rx:8});
    const highlight=el("path",{class:"frame-highlight",d:`M10 1.5 H${width-10}`});
    const rightPort=el("circle",{class:"port",cx:width-2,cy:NODE_H/2,r:2.5,fill:color});
    g.append(frame,sheen,noise,inner,rim,highlight,rightPort);
    if(kind==="method"||kind==="component")g.appendChild(el("circle",{class:"port",cx:2,cy:NODE_H/2,r:2.5,fill:color}));
    if(kind==="work_order"){
      const badge=el("rect",{class:"rank-badge",x:width-43,y:48,width:31,height:25,rx:5});
      const rank=el("text",{x:width-27.5,y:61,class:"rank-number"},String(Math.round(Number(node.similarity||0)*100)));
      g.append(badge,rank);
      g.appendChild(el("path",{class:"node-icon-line",d:"M15 17H29V35H15ZM18 21H26M18 25H26M18 29H23"}));
      addText(g,38,28,node.label,"node-title",16,1);
      g.appendChild(el("text",{x:15,y:61,class:"node-sub"},nodeSub(node)));
    }else if(kind==="root"){
      g.appendChild(el("circle",{class:"node-icon-disc",cx:25,cy:35,r:13}));
      g.appendChild(el("path",{class:"node-icon-line",d:"M17 35H21L24 28L28 42L31 35H34"}));
      addText(g,45,36,node.label,"node-title",13,1);
      g.appendChild(el("text",{x:45,y:57,class:"node-sub"},"ROOT FAULT"));
    }else{
      if(kind==="method"){
        g.appendChild(el("path",{class:"node-icon-line",transform:"translate(7.4,18.2) scale(.8)",d:"M32 24a6.5 6.5 0 0 1-8.2 8l-7.5 7.5a2.8 2.8 0 0 1-4-4l7.5-7.5a6.5 6.5 0 0 1 8-8.2L24 23.6l4.2 4.2L32 24z"}));
      }else{
        g.append(
          el("path",{class:"node-icon-line",d:gearPath(20.5,37.5,8,4.3,3.45)}),
          el("circle",{class:"node-icon-line",cx:20.5,cy:37.5,r:1.65}),
          el("path",{class:"node-icon-line",d:gearPath(28,46.5,9,6.2,4.95)}),
          el("circle",{class:"node-icon-line",cx:28,cy:46.5,r:2.55})
        );
      }
      const titleWidth=kind==="component"?21:22,titleLineCount=lines(node.label,titleWidth,2).length;
      addText(g,47,titleLineCount>1?34:45,node.label,"node-title",titleWidth,2);
    }
    if(kind==="work_order"){
      const signalButton=el("g",{class:"signal-button",transform:`translate(${width-47},5)`,tabindex:"0",role:"button","aria-label":`对比工单 ${node.work_order} 的信号变化`});
      signalButton.append(el("circle",{class:"signal-button-circle",cx:9,cy:9,r:7.8}),el("path",{class:"signal-button-wave",transform:"translate(-1.5,-1.5) scale(.86)",d:"M3.5 10.5H6.2L8.4 6.2L11.1 15L13.5 8.4L15.7 10.5H18"}));
      const showSignal=event=>{event.preventDefault();event.stopPropagation();openSignalComparison(node)};
      signalButton.addEventListener("click",showSignal);signalButton.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" ")showSignal(event)});
      const detailButton=el("g",{class:"detail-button",transform:`translate(${width-24},5)`,tabindex:"0",role:"button","aria-label":`查看工单 ${node.work_order} 详情`});
      detailButton.append(el("circle",{class:"detail-button-circle",cx:9,cy:9,r:7.8}),el("circle",{class:"detail-button-dot",cx:9,cy:6.6,r:.75}),el("path",{class:"detail-button-line",d:"M9 8.8V12.2"}));
      const showDetail=event=>{event.preventDefault();event.stopPropagation();openWorkOrderDetail(node)};
      detailButton.addEventListener("click",showDetail);detailButton.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" ")showDetail(event)});
      g.append(signalButton,detailButton);
    }
    bindNode(g,node);svg.appendChild(g)
  }
function applyHighlight(){let activeNodes=new Set(),activeEdges=new Set();if(selectedWorkOrders.size){DATA.edges.forEach(edge=>{if((edge.work_orders||[]).some(wo=>selectedWorkOrders.has(wo))){activeEdges.add(edge.id);activeNodes.add(edge.source);activeNodes.add(edge.target)}});DATA.nodes.filter(node=>node.type==="work_order"&&selectedWorkOrders.has(node.work_order)).forEach(node=>activeNodes.add(node.id))}else if(selectedNode){activeNodes.add(selectedNode);DATA.edges.forEach(edge=>{if(edge.source===selectedNode||edge.target===selectedNode){activeEdges.add(edge.id);activeNodes.add(edge.source);activeNodes.add(edge.target)}})}const filtering=selectedWorkOrders.size||selectedNode;svg.querySelectorAll(".node").forEach(g=>{const node=nodeById.get(g.dataset.id);g.classList.toggle("selected",node?.type==="work_order"&&selectedWorkOrders.has(node.work_order)||g.dataset.id===selectedNode);g.classList.toggle("faded",Boolean(filtering)&&!activeNodes.has(g.dataset.id))});svg.querySelectorAll("[data-edge]").forEach(path=>{const active=activeEdges.has(path.dataset.edge);path.classList.toggle("highlight",active);path.classList.toggle("faded",Boolean(filtering)&&!active)});const selected=[...selectedWorkOrders];selection.innerHTML=selected.length?`已点亮工单（${selected.length}）：<b>${selected.join("、")}</b>`:"点击工单可多选，路径高亮取<b>并集</b>"}
function renderMetrics(columns){const topSimilarity=Math.max(0,...columns.work_order.map(node=>Number(node.similarity||0)));const items=[
  {icon:"◎",label:"最高相似度",value:`${(topSimilarity*100).toFixed(1)}%`,note:"与当前故障的相似度",color:"#27cfff"},
  {icon:"◇",label:"参考工单数量",value:`${columns.work_order.length} 个`,note:"本次返回工单总数",color:"#52bfff"},
  {icon:"⌁",label:"关联维修手段",value:`${columns.method.length} 项`,note:"图谱维修手段节点",color:"#52e57d"},
  {icon:"⚙",label:"关联相关部件",value:`${columns.component.length} 项`,note:"图谱相关部件节点",color:"#20d9ff"},
  {icon:"↗",label:"图谱关系数量",value:`${DATA.edges.length} 条`,note:"当前知识图谱关系",color:"#ff5f86"}
];metrics.innerHTML="";items.forEach(item=>{const card=htmlNode("div","metric");card.style.setProperty("--metric-color",item.color);card.append(htmlNode("div","metric-icon",item.icon),htmlNode("div","metric-label",item.label),htmlNode("div","metric-value",item.value),htmlNode("div","metric-note",item.note));metrics.appendChild(card)})}
  function publishDashboardSummary(top){
    if(!top)return;
    const structured=top.structured_record||{};
    const catalog=Array.isArray(DATA.meta.signal_catalog)?DATA.meta.signal_catalog:[];
    const excluded=Array.isArray(DATA.meta.excluded_signals)?DATA.meta.excluded_signals:[];
    const newSignals=mapNewSignalChanges(catalog,excluded);
    const selectedKeys=new Set((top.signal_keys||[]).map(signalKey));
    const historySignals=catalog.filter(signal=>selectedKeys.has(signalKey(signal.normalized_signal_key))&&!isExcludedSignal(signal,excluded));
    const rows=comparisonRows(newSignals,historySignals);
    const matched=rows.filter(row=>row.state==="match").length;
    const compactSignal=signal=>signal?{
      description:signal.normalized_dimension||signal.description||signal.dimension||"未命名信号",
      previous:signal.previous_value,current:signal.current_value,count:Number(signal.count||1)
    }:null;
    window.parent.postMessage({type:"1757-kg-summary",payload:{
      workOrder:top.work_order||"",train:top.train||"",carNo:top.car_no||"",similarity:Number(top.similarity||0),
      fault:structured.fault||top.fault||"",component:structured.component||"",cause:structured.cause||top.cause||"",
      action:structured.action||top.action||"",result:structured.result||top.result||"",
      newSignalCount:newSignals.length,historySignalCount:historySignals.length,matchedSignalCount:matched,
      signalRows:rows.map(row=>({state:row.state,left:compactSignal(row.left),right:compactSignal(row.right)}))
    }},"*")
  }
  function render(){
    svg.innerHTML="";
    const defs=el("defs");
    const sheenGradient=el("linearGradient",{id:"nodeSheen",x1:"0%",y1:"0%",x2:"0%",y2:"100%"});
    sheenGradient.append(el("stop",{offset:"0%","stop-color":"#ffffff","stop-opacity":".24"}),el("stop",{offset:"52%","stop-color":"#e9f9ff","stop-opacity":".055"}),el("stop",{offset:"100%","stop-color":"#d6f3ff","stop-opacity":"0"}));
    const topLine=el("linearGradient",{id:"nodeTopLine",x1:"0%",y1:"0%",x2:"100%",y2:"0%"});
    topLine.append(el("stop",{offset:"0%","stop-color":"#dff7ff","stop-opacity":"0"}),el("stop",{offset:"50%","stop-color":"#dff7ff","stop-opacity":".58"}),el("stop",{offset:"100%","stop-color":"#dff7ff","stop-opacity":"0"}));
    const noiseFilter=el("filter",{id:"nodeNoiseFilter",x:"0",y:"0",width:"100%",height:"100%"});
    noiseFilter.append(el("feTurbulence",{type:"fractalNoise",baseFrequency:".72",numOctaves:"2",seed:"14",result:"noise"}),el("feColorMatrix",{in:"noise",type:"matrix",values:"0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 .18 0"}));
    const noisePattern=el("pattern",{id:"nodeNoise",width:"72",height:"72",patternUnits:"userSpaceOnUse"});
    noisePattern.appendChild(el("rect",{width:"72",height:"72",filter:"url(#nodeNoiseFilter)"}));
    defs.append(sheenGradient,topLine,noiseFilter,noisePattern);svg.appendChild(defs);
    const columns={root:[],work_order:[],method:[],component:[]};
    DATA.nodes.forEach(node=>{if(columns[node.type])columns[node.type].push(node)});
    columns.work_order.sort((a,b)=>a.rank-b.rank);
    ["method","component"].forEach(type=>columns[type].sort((a,b)=>(b.work_orders||[]).length-(a.work_orders||[]).length||a.label.localeCompare(b.label,"zh-CN")));
    const maxCount=Math.max(1,...Object.values(columns).map(items=>items.length));
    const viewport=document.querySelector(".viewport"),svgWidth=Math.max(980,viewport.clientWidth||WIDTH);
    const viewportUnits=Math.max(510,(viewport.clientHeight||510)*WIDTH/svgWidth);
    const total=Math.max(viewportUnits,maxCount*100+30),drawableHeight=total-24;
    const clusterHeight=Math.min(drawableHeight,Math.max(NODE_H,(maxCount-1)*100+NODE_H));
    const clusterTop=12+Math.max(0,(drawableHeight-clusterHeight)/2);
    svg.setAttribute("viewBox",`0 0 ${WIDTH} ${total}`);
    const positions=new Map();
    const rootYs=distribute(columns.root.length,clusterTop,clusterHeight,100);
    const methodYs=distribute(columns.method.length,clusterTop,clusterHeight,100);
    const componentYs=distribute(columns.component.length,clusterTop,clusterHeight,100);
    let workYs=distribute(columns.work_order.length,clusterTop,clusterHeight,116);
    if(columns.work_order.length>1&&methodYs.length>columns.work_order.length){
      const lastAnchor=Math.max(0,methodYs.length-2);
      workYs=columns.work_order.map((_,index)=>methodYs[Math.round(index*lastAnchor/(columns.work_order.length-1))]+11);
    }
    columns.root.forEach((node,index)=>positions.set(node.id,{x:X.root,y:rootYs[index]}));
    columns.work_order.forEach((node,index)=>positions.set(node.id,{x:X.work_order,y:workYs[index]}));
    columns.method.forEach((node,index)=>positions.set(node.id,{x:X.method,y:methodYs[index]}));
    columns.component.forEach((node,index)=>positions.set(node.id,{x:X.component,y:componentYs[index]}));
    DATA.edges.forEach(edge=>{const source=positions.get(edge.source),target=positions.get(edge.target);if(!source||!target)return;const sourceNode=nodeById.get(edge.source);const x1=source.x+W[sourceNode.type],y1=source.y+NODE_H/2,x2=target.x,y2=target.y+NODE_H/2,mid=(x1+x2)/2,d=`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;svg.appendChild(el("path",{d,class:"edge",stroke:"rgba(184,220,239,.30)","data-edge":edge.id}))});
    ["root","work_order","method","component"].forEach(type=>columns[type].forEach(node=>createNode(node,positions.get(node.id))));
    const summaryNode=columns.work_order.find(node=>node.work_order===activeSummaryWorkOrder)||columns.work_order[0];
    applyHighlight();publishDashboardSummary(summaryNode)
  }
render();
window.addEventListener("message",event=>{if(event.data?.type!=="1757-open-top-signal")return;const workOrders=DATA.nodes.filter(node=>node.type==="work_order").sort((a,b)=>Number(a.rank||99)-Number(b.rank||99));const target=workOrders.find(node=>node.work_order===activeSummaryWorkOrder)||workOrders[0];if(target)openSignalComparison(target)});
let resizeTimer=0;window.addEventListener("resize",()=>{window.clearTimeout(resizeTimer);resizeTimer=window.setTimeout(render,120)});
</script>
</body>
</html>"""
    return document.replace("__DATA__", payload)


def _detail_grid(record: Dict[str, str], fields: List[tuple[str, str]]) -> html.Div:
    cells = []
    for column, label in fields:
        value = record.get(column, "")
        if not value:
            continue
        cells.append(html.Div([
            html.Div(label, style={"fontSize": "12px", "color": "#6b7280", "marginBottom": "3px"}),
            html.Div(value, style={"fontSize": "14px", "color": "#1f2937", "whiteSpace": "pre-wrap"}),
        ], style={"padding": "10px 12px", "background": "#f8fafc", "borderRadius": "6px",
                  "border": "1px solid #e5e7eb", "minWidth": "180px"}))
    return html.Div(cells, style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit,minmax(220px,1fr))",
                                  "gap": "8px"})


def _structured_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip() and str(item).strip().lower() not in {"n/a", "na", "none"}]
        return "、".join(values)
    text = str(value).strip()
    return "" if text.lower() in {"", "n/a", "na", "none", "null"} else text


def _structured_card(title: str, value: Any, accent: str = "#2563eb") -> html.Div:
    text = _structured_value(value) or "未记录"
    return html.Div([
        html.Div(title, style={"fontSize": "12px", "fontWeight": "700", "color": accent, "marginBottom": "6px"}),
        html.Div(text, style={"fontSize": "14px", "lineHeight": "1.65", "color": "#1f2937"}),
    ], style={"padding": "12px 14px", "background": "#fff", "border": "1px solid #e5e7eb",
              "borderLeft": f"4px solid {accent}", "borderRadius": "7px"})


def _event_pairs(record: Dict[str, Any], component_key: str, event_key: str) -> List[html.Div]:
    components = record.get(component_key) if isinstance(record.get(component_key), list) else []
    events = record.get(event_key) if isinstance(record.get(event_key), list) else []
    rows = []
    for i in range(max(len(components), len(events))):
        component = _structured_value(components[i] if i < len(components) else "") or "相关部件"
        event = _structured_value(events[i] if i < len(events) else "") or "未记录"
        rows.append(html.Div([
            html.Span(component, style={"fontWeight": "700", "color": "#334155"}),
            html.Span("  ·  ", style={"color": "#94a3b8"}),
            html.Span(event, style={"color": "#475569"}),
        ], style={"padding": "7px 0", "borderBottom": "1px solid #f1f5f9", "lineHeight": "1.5"}))
    return rows or [html.Div("未记录", style={"color": "#94a3b8", "padding": "7px 0"})]


def render_work_order_detail(work_order_no: str, session_id: Any = None):
    raw_work_order = str(work_order_no or "").strip()
    match = re.search(r"\d{10}", raw_work_order)
    wo = match.group(0) if match else raw_work_order.removeprefix("★").strip()
    if wo == "新故障" or "新故障" in raw_work_order:
        q = get_session_query(session_id)
        if not q:
            return html.Div("当前没有新故障查询结果。", style={"color": "#666"})
        top_rows = make_top_rows(q)
        return html.Div([
            html.H3("★ 本次查询的新故障", style={"margin": "0 0 12px", "color": "#b4233b"}),
            _detail_grid({
                "train": str(q.get("train", "")),
                "fault_raw": str(q.get("fault_time", "")),
                "fault_utc": str(q.get("fault_time_utc", "")),
                "nodes": str(q.get("node_count", "")),
            }, [
                ("train", "车号"), ("fault_raw", "输入故障时间"),
                ("fault_utc", "UTC 时间"), ("nodes", "节点数"),
            ]),
            html.H4("最相似历史工单", style={"margin": "16px 0 8px"}),
            dash_table.DataTable(
                columns=[{"name": c, "id": c} for c in ["排名", "工单号", "车号", "故障时间", "相似度", "备注"]],
                data=top_rows, style_cell={"textAlign": "left", "padding": "6px", "whiteSpace": "normal"},
                style_header={"fontWeight": "bold", "backgroundColor": "#f5f5f5"},
            ),
        ])

    record = load_structured_work_order_records().get(wo)
    raw_record = load_work_order_records().get(wo)
    if not record and not raw_record:
        return html.Div(f"工单数据中没有找到工单 {wo}。", style={"color": "#b4233b"})
    if not record:
        return html.Div([
            html.Div(f"工单 {wo} 尚未完成结构化处理。", style={"fontWeight": "800", "color": "#b4233b"}),
            html.Div("请运行 refresh_from_work_order.ps1；覆盖校验通过后再刷新页面。",
                     style={"marginTop": "6px", "color": "#64748b"}),
        ])

    identity = {
        "work_order": _structured_value(record.get("work_order")) or wo,
        "equip_instance": _structured_value(record.get("equip_instance")),
        "equip_model": _structured_value(record.get("equip_model")),
        "equip_unit": _structured_value(record.get("equip_unit")),
    }
    identity_grid = _detail_grid(identity, [
        ("work_order", "工单号"), ("equip_instance", "车组"),
        ("equip_model", "车型"), ("equip_unit", "设备单元"),
    ])

    flow_columns = [
        ("现象", "phenomenon_component", "phenomenon_events", "#dc2626", "#fef2f2"),
        ("原因", "cause_components", "cause_events", "#d97706", "#fffbeb"),
        ("处置", "treatment_components", "treatment_events", "#059669", "#ecfdf5"),
    ]
    flow = html.Div([
        html.Div([
            html.Div(title, style={"fontWeight": "800", "color": color, "marginBottom": "6px"}),
            *_event_pairs(record, component_key, event_key),
        ], style={"padding": "12px 14px", "background": bg, "border": f"1px solid {color}33",
                  "borderRadius": "8px", "minWidth": "220px"})
        for title, component_key, event_key, color, bg in flow_columns
    ], style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit,minmax(240px,1fr))", "gap": "10px"})

    structured_children = [
        html.Div([
            html.H3(f"工单 {wo} · 结构化信息", style={"margin": 0, "color": "#1f2937"}),
            html.Span("FAULT EXTRACTOR", style={"fontSize": "11px", "fontWeight": "800", "color": "#2563eb",
                                                   "background": "#eff6ff", "padding": "5px 8px", "borderRadius": "999px"}),
        ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "12px"}),
        identity_grid,
        html.Div([
            _structured_card("故障现象", record.get("fault"), "#dc2626"),
            _structured_card("涉及部件", record.get("component"), "#7c3aed"),
            _structured_card("原因判断", record.get("cause"), "#d97706"),
            _structured_card("处理结果", record.get("result"), "#059669"),
        ], style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit,minmax(260px,1fr))",
                  "gap": "10px", "marginTop": "12px"}),
        html.H4("诊断与处置链路", style={"margin": "18px 0 8px", "color": "#334155"}),
        flow,
        html.Div([
            _structured_card("支撑证据", record.get("evidence"), "#2563eb"),
            _structured_card("采取动作", record.get("action"), "#059669"),
        ], style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit,minmax(300px,1fr))",
                  "gap": "10px", "marginTop": "12px"}),
        html.Div([
            _structured_card("拆入件", record.get("replace_in_part"), "#64748b"),
            _structured_card("拆出件", record.get("replace_out_part"), "#64748b"),
        ], style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit,minmax(220px,1fr))",
                   "gap": "10px", "marginTop": "10px"}),
    ]
    return html.Div(structured_children)


def _extract_work_order_no_from_any(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d{10}", text)
    if match:
        return match.group(0)
    if "新故障" in text:
        return "新故障"
    return text


# ============================================================
# Dash 应用
# ============================================================


class _OptionalPrefixStripMiddleware:
    """Accept both preserved-prefix and proxy-stripped requests."""

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = str(environ.get("PATH_INFO", ""))
        if path == self.prefix:
            environ["PATH_INFO"] = "/"
        elif path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wsgi_app(environ, start_response)


app = dash.Dash(__name__, requests_pathname_prefix=DASH_REQUESTS_PATHNAME_PREFIX)
app.title = "VCB 相似工单 · Excluded Codes"
server = app.server  # 供 gunicorn app_visualize:server 部署
if DASH_REQUESTS_PATHNAME_PREFIX != "/":
    server.wsgi_app = _OptionalPrefixStripMiddleware(
        server.wsgi_app,
        DASH_REQUESTS_PATHNAME_PREFIX,
    )


@server.route("/api/v1/fault-analysis", methods=["GET"])
def open_fault_analysis_page():
    """Validate public event parameters and open the auto-running Dash page."""
    public_fields = ("equipInstance", "equipUnit", "eventTime", "eventCode")
    payload = {
        name: str(flask_request.args.get(name, "")).strip()
        for name in public_fields
    }
    missing = [name for name, value in payload.items() if not value]
    if missing:
        return jsonify({
            "ok": False,
            "error": "missing required query parameters",
            "missing": missing,
        }), 422

    try:
        from api_service import FaultAnalysisRequest
        from pydantic import ValidationError

        validated = FaultAnalysisRequest.model_validate({
            "train": payload["equipInstance"],
            "car_no": payload["equipUnit"],
            "fault_time": payload["eventTime"],
            "fault_code": payload["eventCode"],
        })
    except ValidationError as exc:
        return jsonify({
            "ok": False,
            "error": "invalid request parameters",
            "details": exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        }), 422

    # Serve the Dash document from this endpoint without redirecting. The
    # browser keeps the integration query string, so the existing dcc.Location
    # callback can populate the form and start the analysis automatically.
    response = server.make_response(app.index())
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    return response


@server.route("/api/v1/fault-analysis", methods=["POST"])
def public_fault_analysis_api():
    """JSON integration endpoint exposed below the Dash public prefix."""
    expected_api_key = os.getenv("API_KEY", "").strip()
    provided_api_key = flask_request.headers.get("X-API-Key", "").strip()
    if expected_api_key and (
        not provided_api_key
        or not hmac.compare_digest(provided_api_key, expected_api_key)
    ):
        return jsonify({
            "ok": False,
            "error": "missing or invalid X-API-Key",
        }), 401

    payload = flask_request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "ok": False,
            "error": "request body must be a JSON object",
        }), 400

    # The public field names follow the upstream event-system contract. Keep
    # accepting the original API field names as aliases for existing callers.
    aliases = {
        "equipInstance": "train",
        "equipUnit": "car_no",
        "eventTime": "fault_time",
        "eventCode": "fault_code",
    }
    normalized = dict(payload)
    for public_name, internal_name in aliases.items():
        if internal_name not in normalized and public_name in normalized:
            normalized[internal_name] = normalized[public_name]

    try:
        from api_service import FaultAnalysisRequest, execute_fault_analysis
        from pydantic import ValidationError

        api_request = FaultAnalysisRequest.model_validate(normalized)
        result = execute_fault_analysis(api_request)
    except ValidationError as exc:
        return jsonify({
            "ok": False,
            "error": "invalid request parameters",
            "details": exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        }), 422
    except Exception as exc:
        status_code = int(getattr(exc, "status_code", 500))
        detail = getattr(exc, "detail", None) or str(exc)
        return jsonify({
            "ok": False,
            "error": str(detail),
        }), status_code

    return jsonify(result)


@server.route("/api/v1/knowledge-graph/signals", methods=["GET"])
def knowledge_graph_signal_api():
    """Return the latest canonical signal nodes for one knowledge-graph work order."""
    work_order = _kg_clean_text(flask_request.args.get("work_order", "")).removesuffix(".0")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", work_order):
        return jsonify({
            "ok": False,
            "error": "invalid work_order",
        }), 400

    try:
        snapshot = _knowledge_graph_signal_snapshot(work_order)
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500

    response = jsonify({"ok": True, **snapshot})
    response.headers["Cache-Control"] = "no-store"
    return response


HINT_FIG = _placeholder("⬆ 输入车号、故障时间、车厢号和故障码，点【查询】后这里显示维度变化热力图")
LIBRARY_SIMILARITY_FIG = _placeholder("该页面已从当前版本移除")
_DETAIL_METADATA = load_library()[1]
DETAIL_WORK_ORDERS = sorted(_DETAIL_METADATA["work_order_no"].astype(str).dropna().unique().tolist())


def build_library_work_order_rows(metadata: pd.DataFrame) -> List[Dict[str, Any]]:
    """Build one searchable summary row for every work order in the case library."""
    frame = metadata.copy()
    frame["work_order_no"] = frame["work_order_no"].astype(str)
    frame["_time_order_num"] = pd.to_numeric(frame.get("time_order"), errors="coerce").fillna(0)
    frame = frame.sort_values(["work_order_no", "_time_order_num", "fault_time_utc"])
    rows: List[Dict[str, Any]] = []
    for work_order, group in frame.groupby("work_order_no", sort=True):
        first = group.iloc[0]
        rows.append({
            "id": str(work_order),
            "工单号": str(work_order),
            "车号": clean_work_order_value(first.get("train")),
            "车厢号": clean_work_order_value(first.get("car_no")),
            "故障时间": clean_work_order_value(first.get("fault_time_raw")),
            "案例数": int(len(group)),
            "备注": clean_work_order_value(first.get("remark")),
        })
    return rows


LIBRARY_WORK_ORDER_ROWS = build_library_work_order_rows(_DETAIL_METADATA)

app.layout = html.Div(
    className="app-shell",
    children=[
        dcc.Location(id="page_url", refresh=False),
        dcc.Store(id="session_id", storage_type="session", data=None),
        dcc.Store(id="ts_selection", data=None),
        dcc.Store(id="query_revision", data=None),
        dcc.Store(id="ts_hover_sink", data=None),
        dcc.Store(id="detail_scroll_sink", data=None),
        dcc.Store(id="work_order_detail_selection", data=None),
        dcc.Store(id="current_work_order_detail", data=None),
        dcc.Store(id="download_result_store", data=None),
        html.Header(className="command-header", children=[
            html.Div(className="header-live-info header-live-info-left", children=[
                html.Details(className="query-drawer", open=False, children=[
                    html.Summary([
                        html.Span("⌁", className="query-drawer-icon"),
                        html.Span("查询条件"),
                    ]),
                ]),
                html.Div(className="header-query-values", children=[
                    html.Div([
                        html.Span("当前查询工单：", className="header-info-label"),
                        html.Strong("—", className="header-current-work-order"),
                    ]),
                    html.Div([
                        html.Span("车号：", className="header-info-label"),
                        html.Strong("—", className="header-current-train"),
                        html.Span("查询时间：", className="header-info-label header-info-spacer"),
                        html.Strong("—", className="header-current-time"),
                    ]),
                ]),
            ]),
            html.Div(className="command-header-center", children=[
                html.H2("故障案例智能分析平台", className="app-title"),
                html.Div("FAULT CASE INTELLIGENT ANALYSIS PLATFORM", className="app-subtitle"),
            ]),
            html.Div(className="header-live-info header-live-info-right", children=[
                html.Div(className="system-state", children=[
                    html.Span("系统状态：", className="header-info-label"),
                    html.I(className="system-state-dot"),
                    html.Strong("正常运行"),
                ]),
                html.Div([
                    html.Span("数据更新时间：", className="header-info-label"),
                    html.Strong("—", className="header-data-time"),
                ]),
            ]),
        ]),
        html.Div(
            className="control-panel",
            children=[
                html.Div(className="query-panel-heading", children=[
                    html.Span("故障案例检索", className="query-panel-title"),
                    html.Span("FAULT CASE RETRIEVAL", className="query-panel-subtitle"),
                ]),
                html.Div([html.Label("车号（如 T9）"), dcc.Input(id="train", type="text", value="", style={"width": "100%"})], className="query-field query-field-train"),
                html.Div([html.Label("故障时间（YYYY-MM-DD HH:MM:SS）"), dcc.Input(id="fault_time", type="text", value="", style={"width": "100%"})], className="query-field query-field-time"),
                html.Div([html.Label("车厢号（如 02/05/09）"), dcc.Input(id="car_no", type="text", value="", style={"width": "100%"})], className="query-field query-field-car"),
                html.Div([html.Label("故障码（如 1757）"), dcc.Input(id="fault_code", type="text", value="1757", style={"width": "100%"})], className="query-field query-field-code"),
                html.Div([html.Label("Top 工单数（1–10）"), dcc.Input(
                    id="top_k", type="number", value=DEFAULT_TOP_K,
                    min=MIN_TOP_K, max=MAX_TOP_K, step=1,
                    style={"width": "100%"},
                )], className="query-field query-field-top-k"),
                html.Div([html.Label("最低相似度"), dcc.Input(
                    id="similarity_threshold", type="number", value=DEFAULT_SIMILARITY_THRESHOLD,
                    min=0, max=1, step=0.05, style={"width": "100%"},
                )], className="query-field query-field-similarity"),
                dcc.Checklist(id="time_is_cst", className="query-time-toggle", options=[{"label": " 时间是北京时间(CST)", "value": "cst"}], value=["cst"]),
                html.Button("查询相似工单", id="run", n_clicks=0, className="primary-button"),
                html.Details(
                    className="window-settings",
                    open=False,
                    children=[
                        html.Summary("下载区间（默认：故障前 60 秒 / 后 30 秒）"),
                        html.Div(className="window-settings-body", children=[
                            html.Div([html.Label("故障前（秒）"), dcc.Input(
                                id="before_seconds", type="number",
                                value=int(round(float(GATEWAY_BEFORE_MIN) * 60)),
                                min=1, max=1800, step=1,
                            )]),
                            html.Div([html.Label("故障后（秒）"), dcc.Input(
                                id="after_seconds", type="number",
                                value=int(round(float(GATEWAY_AFTER_MIN) * 60)),
                                min=1, max=1800, step=1,
                            )]),
                            html.Span(
                                "仅在当前窗口没有可用数据时调整，然后重新查询。",
                                className="window-settings-note",
                            ),
                        ]),
                    ],
                ),
            ],
        ),
        html.Div(className="query-progress-area", children=[
            html.Div(
                id="download_progress",
                className="query-progress-line",
                style={"display": "none"},
                children=[html.Span(className="mini-spinner"), html.Span("数据正在下载"), html.Span(className="animated-dots")],
            ),
            html.Div(
                id="analysis_progress",
                className="query-progress-line",
                style={"display": "none"},
                children=[html.Span(className="mini-spinner"), html.Span("数据正在分析"), html.Span(className="animated-dots")],
            ),
        ]),
        html.Div(id="status", className="status-line"),
        html.Div(
            id="main_page",
            children=[
                html.Div(className="main-results-shell", children=[
                    html.Div(className="dashboard-main top-results-layout", children=[
                        html.Section(className="glass-panel work-order-panel", children=[
                            html.H3([html.Span("工单信息"), html.Small("WORK ORDER")], className="section-title"),
                            html.Div(className="work-order-summary", children=[
                                html.Div(className="work-order-identity", children=[
                                    html.Div("—", className="dashboard-top-work-order"),
                                    html.Div([
                                        html.Span("▱", className="summary-icon"),
                                        html.Span("车号 —", className="dashboard-top-train"),
                                        html.I(),
                                        html.Span("车厢 —", className="dashboard-top-car"),
                                    ], className="work-order-train"),
                                ]),
                                html.Div(className="similarity-summary", children=[
                                    html.Div("TOP1 相似度", className="summary-label"),
                                    html.Div("—", className="dashboard-top-similarity"),
                                    html.Div([html.I() for _ in range(12)], className="similarity-bars"),
                                ]),
                            ]),
                            html.Div(className="structured-info-block", children=[
                                html.H4([html.Span("结构化信息"), html.Small("STRUCTURED INFORMATION")]),
                                html.Div(className="structured-info-grid", children=[
                                    html.Div([html.Span("故障现象"), html.Strong("—", className="structured-fault")], className="structured-field structured-field-wide"),
                                    html.Div([html.Span("涉及部件"), html.Strong("—", className="structured-component")], className="structured-field"),
                                    html.Div([html.Span("原因判断"), html.Strong("—", className="structured-cause")], className="structured-field"),
                                    html.Div([html.Span("维修措施"), html.Strong("—", className="structured-action")], className="structured-field structured-field-wide"),
                                    html.Div([html.Span("处理结果"), html.Strong("—", className="structured-result")], className="structured-field structured-field-wide"),
                                ]),
                            ]),
                            html.Div(className="signal-summary-block", children=[
                                html.Div(className="signal-summary-heading", children=[
                                    html.H4([html.Span("信号变化对比"), html.Small("SIGNAL CHANGE COMPARISON")]),
                                    html.Button("查看完整对比", type="button", className="open-signal-comparison"),
                                ]),
                                html.Div(className="signal-summary-stats", children=[
                                    html.Div([html.Strong("--", className="signal-new-count"), html.Span("新故障变化")]),
                                    html.Div([html.Strong("--", className="signal-history-count"), html.Span("历史信号")]),
                                    html.Div([html.Strong("--", className="signal-match-count"), html.Span("同类变化")]),
                                ]),
                                html.Div(className="signal-comparison-columns", children=[
                                    html.Span("查询故障的信号变化"),
                                    html.Span("TOP1 工单的信号变化"),
                                ]),
                                html.Div("查询后显示 Top1 工单与查询故障的全部信号变化。", className="signal-change-list"),
                            ]),
                        ]),
                        html.Section(className="glass-panel graph-panel top-results-graph", children=[
                            html.Div(className="process-navigation", children=[
                                html.Div([html.B(html.Img(src="/assets/process-similar.svg", alt="")), html.Span(["相似工单", html.Small("SIMILAR WORK ORDERS")])], className="process-step is-active"),
                                html.I(className="process-arrow", **{"aria-hidden": "true"}),
                                html.Div([html.B(html.Img(src="/assets/process-maintenance.svg", alt="")), html.Span(["维修措施", html.Small("MAINTENANCE ACTIONS")])], className="process-step"),
                                html.I(className="process-arrow", **{"aria-hidden": "true"}),
                                html.Div([html.B(html.Img(src="/assets/process-component.svg", alt="")), html.Span(["相关部件", html.Small("RELATED COMPONENTS")])], className="process-step"),
                            ]),
                            html.Div(className="knowledge-graph-direct-shell", children=html.Iframe(
                                id="top3_knowledge_graph",
                                srcDoc=_top3_graph_placeholder_document("查询后，这里显示所选相似工单的维修知识图谱"),
                                className="top3-knowledge-graph",
                                style={"width": "100%", "height": "100%", "minHeight": "0", "border": "0", "background": "transparent"},
                            )),
                        ]),
                        html.Div(className="dashboard-right-stack", children=[
                            html.Section(className="glass-panel replacement-dashboard-card replacement-info-card", children=[
                                html.H3([
                                    html.Span("换件信息"),
                                    html.Small("REPLACEMENT INFORMATION"),
                                    html.I("▱", className="replacement-title-icon"),
                                ], className="section-title replacement-heading"),
                                html.Div(
                                    id="replacement_info_panel",
                                    className="replacement-info-panel",
                                    children=html.Div("完成相似工单查询后显示真实换件记录。", className="replacement-empty-state"),
                                ),
                            ]),
                            html.Section(className="glass-panel diagnosis-panel top-results-report", children=[
                                html.H3([html.Span("诊断结论"), html.Small("DIAGNOSIS CONCLUSION")], className="section-title diagnosis-heading"),
                                dcc.Loading(
                                    id="fault_report_loading", type="default", show_initially=False,
                                    delay_show=200, delay_hide=250, custom_spinner=report_generation_spinner(),
                                    parent_className="report-loading-shell",
                                    overlay_style={"visibility": "visible", "opacity": 0.16, "filter": "blur(2px)"},
                                    children=html.Div(
                                        id="fault_report_panel",
                                        children=html.Div("完成相似工单查询后显示诊断结论。", className="report-empty-state"),
                                        className="fault-report-panel fault-report-summary-trigger",
                                        n_clicks=0, role="button", tabIndex=0, title="点击查看完整诊断报告",
                                    ),
                                ),
                            ]),
                        ]),
                    ]),
                    html.Footer(className="dashboard-footer", children=[
                        html.Span("智能分析引擎  v2.6.1"),
                        html.Span("注：分析结果基于历史工单数据，实际诊断请结合现场情况综合判断"),
                        html.Span("数据来源：运维管理系统 / 设备监测系统 / 知识库"),
                    ]),
                    html.Div(className="callback-data-components", style={"display": "none"}, children=[
                        dash_table.DataTable(
                            id="top_table", data=[], columns=[{"name": c, "id": c} for c in ["排名", "工单号", "车号", "相似度"]],
                            style_header={"backgroundColor": "rgba(37,62,82,.9)", "color": "#dcecf4"},
                            style_cell={"backgroundColor": "rgba(23,45,63,.74)", "color": "#dcecf4", "border": "1px solid rgba(139,194,219,.16)"},
                        ),
                    ]),
                    html.Div(
                        id="fault_report_modal",
                        className="fault-report-modal",
                        children=[
                            html.Div(
                                id="fault_report_modal_backdrop",
                                className="fault-report-modal-backdrop",
                                n_clicks=0,
                            ),
                            html.Div(
                                className="fault-report-dialog",
                                role="dialog",
                                children=[
                                    html.Div(className="fault-report-dialog-head", children=[
                                        html.Div([
                                            html.Div("完整诊断报告", className="fault-report-dialog-title"),
                                            html.Div("基于本次选中的相似历史工单", className="fault-report-dialog-subtitle"),
                                        ]),
                                        html.Button(
                                            "×",
                                            id="close_fault_report",
                                            n_clicks=0,
                                            className="fault-report-close",
                                            title="关闭完整报告",
                                        ),
                                    ]),
                                    html.Div(
                                        id="fault_report_modal_content",
                                        className="fault-report-dialog-body",
                                        children=html.Div("请先查询相似工单。", className="report-empty-state"),
                                    ),
                                ],
                            ),
                        ],
                    ),
                ]),
                html.Div(style={"display": "none"}, children=[
                    dcc.Loading(type="default", children=[
                        html.Div(className="section-card graph-card", style={"overflowX": "auto", "marginTop": "12px"}, children=[
                            dcc.Checklist(
                                id="only_new",
                                options=[{"label": " 只显示新故障变化过的列", "value": "only"}],
                                value=["only"],
                                style={"margin": "4px 4px 10px", "fontSize": "13px", "color": "#374151"},
                            ),
                            dcc.Graph(id="heatmap", figure=HINT_FIG, config={"displaylogo": False, "responsive": True, "toImageButtonOptions": {"format": "png", "filename": "vcb_dim_heatmap", "scale": 2}}),
                        ]),
                        html.Div(className="section-card", style={"marginTop": "10px"}, children=[
                            html.Div(id="ts_header", children="（点击热力图某个格子后，这里显示对应案例的全部维度；悬停可临时高亮维度）", style={"fontSize": "15px", "fontWeight": "bold", "color": "#333", "marginBottom": "6px"}),
                            html.Div(style={"width": "100%", "maxHeight": "760px", "overflowY": "auto", "border": "1px solid #f0f0f0", "borderRadius": "4px"}, children=[
                                dcc.Graph(id="ts_combined", figure=_placeholder("点击热力图里的某个格子 →<br>这里显示该案例全部维度的原始数据，并高亮所选维度"), config={"displaylogo": False, "responsive": True}),
                            ]),
                        ]),
                        html.Div(className="section-card", style={"marginTop": "14px"}, children=[
                            html.Div("数据明细", style={"fontSize": "17px", "fontWeight": "700", "color": "#334155"}),
                            html.Div([html.Div("当前点击参数的逐采样数据（保持原始顺序，自动定位到首个变化点）", className="section-subtitle"), html.Button("下载 CSV", id="download_detail_button", n_clicks=0, className="secondary-button"), dcc.Download(id="download_detail_csv")], className="section-heading-row", style={"marginTop": "12px", "marginBottom": "16px", "alignItems": "center"}),
                            dash_table.DataTable(
                                id="detail_table", columns=[{"name": c, "id": c} for c in ["序号", "时间(UTC)", "维度", "值", "较上一采样变化"]], data=[], fixed_rows={"headers": True}, virtualization=True, page_action="none",
                                style_table={"height": "320px", "overflowY": "auto", "overflowX": "auto", "marginTop": "4px"}, style_cell={"fontFamily": "Microsoft YaHei, Arial", "textAlign": "left", "padding": "4px 8px", "minWidth": "90px"}, style_header={"fontWeight": "bold", "backgroundColor": "#f5f5f5"},
                                style_data_conditional=[{"if": {"filter_query": "{较上一采样变化} != ''"}, "backgroundColor": "#fff7e6"}],
                            ),
                        ]),
                    ]),
                    html.Div("提示：点击变化热力图中的格子，会同步更新下方原始数据图和数据明细。首次读取某行原始数据可能需要几秒。", className="info-note"),
                ]),
                html.Div(style={"display": "none"}, children=[
                    html.Div(className="section-card cluster-card", style={"marginTop": "12px"}, children=[
                        html.H3("案例库工单相似度", className="section-title"),
                        dcc.Graph(id="work_order_similarity_heatmap", figure=LIBRARY_SIMILARITY_FIG, config={"displaylogo": False, "responsive": True, "toImageButtonOptions": {"format": "png", "filename": "work_order_similarity_heatmap", "scale": 2}}),
                        html.Div(
                            "热力图的颜色、格子数字和悬停信息均只显示最终相似度。红框表示 RF 区域，白线表示按 0.7 阈值划分的簇。",
                            style={"margin": "4px 4px 14px", "padding": "10px 12px", "color": "#5b6472", "fontSize": "13px", "lineHeight": "1.6", "backgroundColor": "#f8fafc", "borderRadius": "6px", "border": "1px solid #e5e7eb"},
                        ),
                        html.Details(id="work_order_detail_details", open=True, className="work-order-details", children=[
                            html.Summary("工单详细信息（点击展开 / 收起）"),
                            html.Div([
                                html.Label("按工单号查询", style={"fontSize": "13px", "fontWeight": "700", "color": "#475569"}),
                                dcc.Dropdown(
                                    id="work_order_detail_selector",
                                    options=[{"label": wo, "value": wo} for wo in DETAIL_WORK_ORDERS],
                                    value=None,
                                    clearable=True,
                                    searchable=True,
                                    placeholder="输入或选择工单号",
                                    style={"marginTop": "6px"},
                                ),
                                html.Div(
                                    "也可以直接点击热力图横轴或纵轴上的工单号。",
                                    style={"fontSize": "12px", "color": "#64748b", "marginTop": "7px"},
                                ),
                            ], style={"padding": "12px 16px 0"}),
                            html.Div(
                                id="work_order_detail_panel",
                                children="请点击坐标轴上的工单号。",
                                className="work-order-detail-panel",
                            ),
                            html.Div([
                                html.Button(
                                    "重新生成该工单原始数据图",
                                    id="generate_work_order_raw_plot",
                                    n_clicks=0,
                                    className="secondary-button",
                                ),
                            ], style={"padding": "4px 16px 10px"}),
                            dcc.Loading(type="default", children=[
                                html.Div(id="work_order_raw_plot_status", style={"padding": "0 16px 8px", "color": "#475569"}),
                                html.Div(style={"padding": "0 16px 16px"}, children=[
                                    dcc.Graph(
                                        id="work_order_raw_plot",
                                        figure=_placeholder("从工单表、下拉框或热力图中选择工单后自动生成原始数据图"),
                                        config={
                                            "displaylogo": False,
                                            "responsive": True,
                                            "scrollZoom": True,
                                            "doubleClick": "reset",
                                            "toImageButtonOptions": {"format": "png", "filename": "work_order_raw_data", "scale": 2},
                                        },
                                    ),
                                ]),
                            ]),
                        ]),
                        html.Div(className="library-work-order-list", style={"marginTop": "20px"}, children=[
                            html.Div(className="section-heading-row", children=[
                                html.Div([
                                    html.H3("案例库全部工单", className="section-title"),
                                    html.Div(
                                        f"当前案例库共 {len(LIBRARY_WORK_ORDER_ROWS)} 张工单。可在任意列输入条件筛选，点击一行即可查看详情和原始数据图；无需先查询新故障。",
                                        className="section-subtitle",
                                    ),
                                ]),
                            ]),
                            dash_table.DataTable(
                                id="library_work_order_table",
                                data=LIBRARY_WORK_ORDER_ROWS,
                                columns=[
                                    {"name": column, "id": column}
                                    for column in ["工单号", "车号", "车厢号", "故障时间", "案例数", "备注"]
                                ],
                                filter_action="native",
                                filter_options={"case": "insensitive"},
                                sort_action="native",
                                sort_mode="multi",
                                page_action="native",
                                page_size=12,
                                cell_selectable=True,
                                style_table={"overflowX": "auto"},
                                style_cell={
                                    "fontFamily": "Microsoft YaHei, Arial",
                                    "textAlign": "left",
                                    "padding": "8px",
                                    "minWidth": "85px",
                                    "maxWidth": "340px",
                                    "whiteSpace": "normal",
                                },
                                style_header={"fontWeight": "bold", "backgroundColor": "#f3f6fa"},
                                style_data_conditional=[{
                                    "if": {"state": "active"},
                                    "backgroundColor": "#dbeafe",
                                    "border": "1px solid #2563eb",
                                }],
                            ),
                        ]),
                    ]),
                ]),
            ],
        ),
    ],
)


app.clientside_callback(
    """
    function(pathname, current) {
        if (current) {
            return window.dash_clientside.no_update;
        }
        if (window.crypto && window.crypto.randomUUID) {
            return window.crypto.randomUUID();
        }
        return 'session-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    }
    """,
    Output("session_id", "data"),
    Input("page_url", "pathname"),
    State("session_id", "data"),
)

@app.callback(
    Output("train", "value"),
    Output("fault_time", "value"),
    Output("car_no", "value"),
    Output("fault_code", "value"),
    Output("run", "n_clicks"),
    Input("page_url", "search"),
    State("train", "value"),
    State("fault_time", "value"),
    State("car_no", "value"),
    State("fault_code", "value"),
    State("run", "n_clicks"),
)
def apply_url_query_parameters(
    search,
    current_train,
    current_fault_time,
    current_car_no,
    current_fault_code,
    current_clicks,
):
    """Apply integration parameters from the page URL and auto-run when complete."""
    if not search:
        raise PreventUpdate

    params = parse_qs(str(search).lstrip("?"), keep_blank_values=False)

    def first(name: str) -> str:
        values = params.get(name, [])
        return str(values[0]).strip() if values else ""

    train = first("equipInstance")
    car_no = first("equipUnit")
    fault_time = first("eventTime")
    fault_code = first("eventCode")
    if not any((train, car_no, fault_time, fault_code)):
        raise PreventUpdate

    next_train = train.upper() if train else current_train
    next_car_no = normalize_car_no(car_no) if car_no else current_car_no
    next_fault_time = fault_time if fault_time else current_fault_time
    next_fault_code = fault_code.upper() if fault_code else current_fault_code
    complete = all((train, car_no, fault_time, fault_code))
    next_clicks = int(current_clicks or 0) + 1 if complete else no_update
    return next_train, next_fault_time, next_car_no, next_fault_code, next_clicks


@app.callback(
    Output("download_result_store", "data"),
    Input("run", "n_clicks"),
    State("train", "value"),
    State("car_no", "value"),
    State("fault_time", "value"),
    State("fault_code", "value"),
    State("before_seconds", "value"),
    State("after_seconds", "value"),
    State("top_k", "value"),
    State("similarity_threshold", "value"),
    State("time_is_cst", "value"),
    prevent_initial_call=True,
    running=[
        (Output("download_progress", "style"), {"display": "flex"}, {"display": "none"}),
        (Output("run", "disabled"), True, False),
    ],
)
def download_query_data(
    n_clicks, train, car_no, fault_time, fault_code,
    before_seconds, after_seconds, top_k, similarity_threshold, time_is_cst_value,
):
    train = (train or "").strip()
    car_no = normalize_car_no((car_no or "").strip())
    fault_time = (fault_time or "").strip()
    fault_code = str(fault_code or "").strip().upper()
    if not train or not car_no or not fault_time or not fault_code:
        return {
            "ok": False,
            "message": "请填写车号、故障时间、车厢号和故障码。",
            "revision": pd.Timestamp.utcnow().isoformat(),
        }

    try:
        before_seconds = float(before_seconds)
        after_seconds = float(after_seconds)
        if not np.isfinite(before_seconds) or before_seconds <= 0:
            raise ValueError("故障前时间必须大于 0 秒")
        if not np.isfinite(after_seconds) or after_seconds <= 0:
            raise ValueError("故障后时间必须大于 0 秒")
        if before_seconds > 1800 or after_seconds > 1800:
            raise ValueError("故障前、后时间均不能超过 1800 秒")
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "message": f"下载区间设置错误：{exc}",
            "revision": pd.Timestamp.utcnow().isoformat(),
        }
    before_min = before_seconds / 60.0
    after_min = after_seconds / 60.0

    time_is_cst = "cst" in (time_is_cst_value or [])
    try:
        top_k_number = float(top_k)
        if not np.isfinite(top_k_number) or not top_k_number.is_integer():
            raise ValueError("必须填写整数")
        top_k = int(top_k_number)
        if not MIN_TOP_K <= top_k <= MAX_TOP_K:
            raise ValueError(f"必须在 {MIN_TOP_K} 到 {MAX_TOP_K} 之间")
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "message": f"Top 工单数设置错误：{exc}",
            "revision": pd.Timestamp.utcnow().isoformat(),
        }
    similarity_threshold = float(
        DEFAULT_SIMILARITY_THRESHOLD if similarity_threshold is None else similarity_threshold
    )
    similarity_threshold = float(np.clip(similarity_threshold, 0.0, 1.0))

    try:
        download_result = download_fault_window(
            train=train,
            fault_time=fault_time,
            fault_code=fault_code,
            time_is_cst=time_is_cst,
            before_min=before_min,
            after_min=after_min,
            metric_definitions=gateway_metric_definitions(),
            metric_codes=gateway_metric_codes_for_car(car_no),
            metrics_data_dir=str(DATA_DIR_PATH),
            output_root=str(GATEWAY_QUERY_ROOT),
            reuse=False,
        )
    except Exception as e:
        return {
            "ok": False,
            "message": f"网关数据下载失败：{e}。可调大“故障前/故障后”时间后重试。",
            "revision": pd.Timestamp.utcnow().isoformat(),
        }

    fault_time_utc = pd.Timestamp(fault_time)
    if time_is_cst:
        fault_time_utc -= pd.Timedelta(hours=8)
    register_raw_window(
        train=train,
        car_no=car_no,
        fault_code=fault_code,
        fault_time_raw=fault_time,
        fault_time_utc=fault_time_utc,
        data_dir=download_result.data_dir,
        row_count=download_result.row_count,
        metric_count=download_result.metric_count,
    )

    return {
        "ok": True,
        "revision": pd.Timestamp.utcnow().isoformat(),
        "train": train,
        "car_no": car_no,
        "fault_time": fault_time,
        "fault_code": fault_code,
        "time_is_cst": time_is_cst,
        "top_k": top_k,
        "similarity_threshold": similarity_threshold,
        "data_dir": str(download_result.data_dir),
        "row_count": int(download_result.row_count),
        "metric_count": int(download_result.metric_count),
        "before_min": before_min,
        "after_min": after_min,
        "before_seconds": before_seconds,
        "after_seconds": after_seconds,
        "reused": bool(download_result.reused),
    }


@app.callback(
    Output("heatmap", "figure"),
    Output("top_table", "data"),
    Output("status", "children"),
    Output("query_revision", "data"),
    Output("work_order_similarity_heatmap", "figure"),
    Input("download_result_store", "data"),
    State("only_new", "value"),
    State("session_id", "data"),
    prevent_initial_call=True,
    running=[
        (Output("analysis_progress", "style"), {"display": "flex"}, {"display": "none"}),
    ],
)
def analyze_query_data(download_payload, only_new_value, session_id):
    only_new = "only" in (only_new_value or [])

    if not download_payload:
        raise PreventUpdate
    if not download_payload.get("ok"):
        set_session_query(session_id, None)
        return no_update, [], download_payload.get("message", "下载失败"), download_payload.get("revision"), no_update

    try:
        q = compute_query(
            train=download_payload["train"],
            car_no=download_payload["car_no"],
            fault_time=download_payload["fault_time"],
            time_is_cst=bool(download_payload["time_is_cst"]),
            top_k=int(download_payload["top_k"]),
            similarity_threshold=float(download_payload["similarity_threshold"]),
            fault_code=download_payload["fault_code"],
            data_dir=download_payload["data_dir"],
            download_info=download_payload,
        )
    except FileNotFoundError as e:
        set_session_query(session_id, None)
        return no_update, [], f"案例库或查询数据缺失: {e}", pd.Timestamp.utcnow().isoformat(), no_update
    except Exception as e:
        set_session_query(session_id, None)
        return no_update, [], f"查询出错: {e}", pd.Timestamp.utcnow().isoformat(), no_update

    if not q.get("ok"):
        set_session_query(session_id, None)
        return no_update, [], q.get("message", "查询失败"), pd.Timestamp.utcnow().isoformat(), no_update

    q["revision"] = pd.Timestamp.utcnow().isoformat()
    set_session_query(session_id, q)
    similarity_threshold = float(download_payload["similarity_threshold"])
    before_text = f"{float(download_payload['before_seconds']):g}秒"
    after_text = f"{float(download_payload['after_seconds']):g}秒"
    base_status = (
        f"下载窗口：故障前{before_text}、后{after_text}；"
        f"新故障 UTC 时间 {q['fault_time_utc']}；节点数 {q['node_count']}"
        f"（excluded {q['excluded_query_node_count']}，未知 {q['unknown_node_count']}）；"
        f"库内工单 {q['metadata']['work_order_no'].astype(str).nunique()}。"
    )
    if q["top_idx"]:
        status = (
            f"成功：显示 {len(q['top_idx'])} 个最终相似度 ≥ {similarity_threshold:.2f} 的工单。"
            + base_status
        )
    else:
        status = (
            f"没有工单达到最低最终相似度 {similarity_threshold:.2f}。"
            "可以降低最低相似度后重新查询。"
            + base_status
        )
    return (
        no_update,
        make_top_rows(q), status, q["revision"],
        no_update,
    )


@app.callback(
    Output("top3_knowledge_graph", "srcDoc"),
    Input("query_revision", "data"),
    Input("top_table", "data"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def update_top3_work_order_knowledge_graph(_query_revision, top_rows, session_id):
    """Keep the graph on exactly the same query revision as the result table/report."""
    if not top_rows:
        return _top3_graph_placeholder_document("当前查询没有可展示的相似工单")
    q = get_session_query(session_id)
    if not q:
        return _top3_graph_placeholder_document("请先查询相似工单")
    try:
        return build_top3_work_order_knowledge_graph_document(q)
    except Exception as exc:
        return _top3_graph_placeholder_document(f"知识图谱生成失败：{exc}")


@app.callback(
    Output("replacement_info_panel", "children"),
    Input("query_revision", "data"),
    Input("top_table", "data"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def update_replacement_information(_query_revision, top_rows, session_id):
    """Render existing Top-work-order replacement records without inventing quantities."""
    if not top_rows:
        return html.Div("请先查询相似工单。", className="replacement-empty-state")
    q = get_session_query(session_id)
    if not q:
        return html.Div("请先查询相似工单。", className="replacement-empty-state")
    try:
        payload = build_top3_work_order_knowledge_graph_payload(q)
    except Exception as exc:
        return html.Div(f"换件信息读取失败：{exc}", className="replacement-empty-state")

    nodes = list(payload.get("nodes") or [])
    work_orders = sorted(
        (node for node in nodes if node.get("type") == "work_order"),
        key=lambda node: int(node.get("rank") or 999),
    )
    components = [node for node in nodes if node.get("type") == "component"]
    rows = []
    for work_order_node in work_orders:
        work_order = _kg_clean_text(work_order_node.get("work_order"))
        replace_in = [
            _kg_clean_text(value)
            for value in (work_order_node.get("replace_in_part") or [])
            if _kg_clean_text(value)
        ]
        replace_out = [
            _kg_clean_text(value)
            for value in (work_order_node.get("replace_out_part") or [])
            if _kg_clean_text(value)
        ]
        if not replace_in and not replace_out:
            continue
        related_labels = []
        for component in components:
            related_orders = {_kg_clean_text(value) for value in (component.get("work_orders") or [])}
            label = _kg_clean_text(component.get("label"))
            if work_order in related_orders and label and label not in related_labels:
                related_labels.append(label)
        component_label = " / ".join(related_labels[:2]) or "未记录关联部件"
        record_count = len(replace_in) + len(replace_out)
        rows.append(html.Div(className="replacement-record", children=[
            html.Div("⌘", className="replacement-record-icon"),
            html.Div(className="replacement-record-main", children=[
                html.Strong(component_label),
                html.Div(className="replacement-code-line", children=[
                    html.Span(f"换入件：{'、'.join(replace_in) if replace_in else '—'}"),
                    html.Span(f"换出件：{'、'.join(replace_out) if replace_out else '—'}"),
                ]),
            ]),
            html.Div(className="replacement-record-source", children=[
                html.Span("来源工单"),
                html.Strong(work_order or "—"),
                html.Small(f"{record_count} 项"),
            ]),
        ]))

    if not rows:
        return html.Div("当前 Top 工单没有记录换入件或换出件。", className="replacement-empty-state")
    return html.Div(rows[:3], className="replacement-record-list")


@app.callback(
    Output("fault_report_panel", "children"),
    Output("fault_report_modal_content", "children"),
    Input("query_revision", "data"),
    Input("top_table", "data"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def generate_fault_report(_query_revision, top_rows, session_id):
    # 报告与 Top 工单使用同一显示条件。刷新页面或新查询清空 Top 工单时，
    # 不允许继续从进程级缓存中恢复上一份报告。
    if not top_rows:
        empty = html.Div("请先查询相似工单。", className="report-empty-state report-empty-state-error")
        return empty, empty
    q = get_session_query(session_id)
    if not q:
        empty = html.Div("请先查询相似工单。", className="report-empty-state report-empty-state-error")
        return empty, empty
    if not q.get("top_idx"):
        empty = html.Div(
            "当前查询没有达到相似度阈值的工单，暂时无法生成报告。",
            className="report-empty-state",
        )
        return empty, empty
    try:
        report = build_fault_reference_report(q)
    except Exception as exc:
        error = html.Div(
            f"报告生成失败：{exc}",
            className="report-empty-state report-empty-state-error",
        )
        return error, error
    return render_fault_reference_report(report, compact=True), render_fault_reference_report(report)


@app.callback(
    Output("fault_report_modal", "className"),
    Input("fault_report_panel", "n_clicks"),
    Input("close_fault_report", "n_clicks"),
    Input("fault_report_modal_backdrop", "n_clicks"),
    Input("query_revision", "data"),
    prevent_initial_call=True,
)
def toggle_fault_report_modal(open_clicks, _close_clicks, _backdrop_clicks, _query_revision):
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered == "fault_report_panel" and open_clicks:
        return "fault-report-modal is-open"
    return "fault-report-modal"


@app.callback(
    Output("ts_combined", "figure"),
    Output("ts_header", "children"),
    Output("detail_table", "data"),
    Output("ts_selection", "data"),
    Input("heatmap", "clickData"),
    State("query_revision", "data"),
    State("ts_selection", "data"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def on_cell_interaction(click_data, query_revision, selection, session_id):
    """点击固定案例/维度并更新原始时序与数据明细。"""
    triggered = callback_context.triggered[0]["prop_id"] if callback_context.triggered else ""
    q = get_session_query(session_id)
    if not q:
        raise PreventUpdate

    if triggered == "query_revision.data":
        seg = q["new_seg"]
        train, futc, file_partition_time = row_train_faultutc(q, 0)
        header = row_header(q, 0)
        try:
            df_raw = get_raw_df(train, futc, file_partition_time, row_data_dir(q, 0))
            counts = segment_counts(seg, "dim")
            dims = sorted(counts, key=lambda dim: (-counts[dim], str(dim)))
            dims = raw_plot_dims_with_vcb(dims, q.get("car_no"))
            default_dim = dims[0] if dims else None
            window_before_min, window_after_min = query_window_minutes(q)
            figure = build_combined_ts(
                df_raw, dims, futc, highlight_dim=default_dim, seg=seg,
                car_no=q.get("car_no"), window_before_min=window_before_min,
                window_after_min=window_after_min,
            )
            detail = build_detail_rows(
                df_raw, default_dim, futc, car_no=q.get("car_no"),
                window_before_min=window_before_min,
                window_after_min=window_after_min,
            ) if default_dim else []
            selection = {"ri": 0, "dim": str(default_dim), "revision": q.get("revision")} if default_dim else None
            return figure, header, detail, selection
        except Exception as e:
            return _placeholder(f"读取新故障原始数据失败：{e}"), header, [], None

    labels, segs = heatmap_rows(q)

    if triggered == "heatmap.clickData":
        if not click_data:
            raise PreventUpdate
        p = click_data["points"][0]
        row_label = p.get("y")
        if row_label not in labels:
            raise PreventUpdate
        customdata = p.get("customdata")
        if isinstance(customdata, (list, tuple)):
            dim = customdata[-1] if customdata else None
        else:
            dim = customdata
        if not dim:  # 兼容浏览器里尚未刷新的旧热力图数据
            dim = _col_label_to_dim(q, "node", p.get("x"))
        if not dim:
            raise PreventUpdate
        ri = labels.index(row_label)
        car_no = row_car_no(q, ri)
        selection = {
            "ri": ri,
            "dim": str(dim),
            "target_time": str(selected_event_time(segs[ri], dim, car_no=car_no) or ""),
            "revision": q.get("revision"),
        }

    if not selection or selection.get("revision") != q.get("revision"):
        raise PreventUpdate

    ri = int(selection["ri"])
    if ri < 0 or ri >= len(segs):
        raise PreventUpdate
    selected_dim = str(selection["dim"])

    seg = segs[ri]
    train, futc, file_partition_time = row_train_faultutc(q, ri)
    header = row_header(q, ri)

    try:
        df_raw = get_query_row_raw_df(q, ri, seg, selected_dim)
    except Exception as e:
        msg = f"读取原始数据失败：{e}"
        return _placeholder(msg), header, [], selection

    counts = segment_counts(seg, "dim")
    dims = set(counts.keys())
    dims.add(selected_dim)
    dims = sorted(dims, key=lambda d: (-counts.get(d, 0), str(d)))
    car_no = row_car_no(q, ri)
    dims = raw_plot_dims_with_vcb(dims, car_no)
    window_before_min, window_after_min = query_window_minutes(q)
    combined = build_combined_ts(
        df_raw, dims, futc, highlight_dim=selected_dim, seg=seg, car_no=car_no,
        window_before_min=window_before_min, window_after_min=window_after_min,
    )
    detail = build_detail_rows(
        df_raw, selected_dim, futc, car_no=car_no,
        window_before_min=window_before_min, window_after_min=window_after_min,
    )
    return combined, header, detail, selection


@app.callback(
    Output("work_order_detail_selector", "value"),
    Input("library_work_order_table", "active_cell"),
    State("query_revision", "data"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def synchronize_library_work_order_selection(
    active_cell, _query_revision, session_id,
):
    """Keep the library browser aligned with a new query or a table click."""
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered == "library_work_order_table":
        if not active_cell:
            raise PreventUpdate
        work_order = _extract_work_order_no_from_any(active_cell.get("row_id"))
        if not work_order:
            raise PreventUpdate
        return work_order

    q = get_session_query(session_id)
    if not q or not q.get("top_idx"):
        raise PreventUpdate
    first_index = int(q["top_idx"][0])
    return str(q["metadata"].iloc[first_index].get("work_order_no", ""))


@app.callback(
    Output("work_order_detail_panel", "children"),
    Output("current_work_order_detail", "data"),
    Input("work_order_detail_selection", "data"),
    Input("work_order_detail_selector", "value"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def show_work_order_detail(selection, selected_work_order, session_id):
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered == "work_order_detail_selector":
        work_order = _extract_work_order_no_from_any(selected_work_order)
    else:
        work_order = _extract_work_order_no_from_any((selection or {}).get("work_order"))
    if not work_order:
        raise PreventUpdate
    return render_work_order_detail(work_order, session_id), {"work_order": work_order}


@app.callback(
    Output("work_order_raw_plot", "figure"),
    Output("work_order_raw_plot_status", "children"),
    Input("current_work_order_detail", "data"),
    Input("generate_work_order_raw_plot", "n_clicks"),
    State("session_id", "data"),
    prevent_initial_call=True,
)
def generate_work_order_raw_plot(current_detail, _n_clicks, session_id):
    work_order = _extract_work_order_no_from_any((current_detail or {}).get("work_order"))
    if not work_order:
        return _placeholder("请先选择工单。"), "请先从下拉框选择工单，或点击坐标轴上的工单号。"

    if work_order == "新故障":
        q = get_session_query(session_id)
        if not q:
            return _placeholder("当前没有新故障查询结果。"), "请先查询新故障。"
        seg = q.get("new_seg")
        train = normalize_train(q.get("train", ""))
        car_no = normalize_car_no(q.get("car_no", ""))
        fault_utc = pd.Timestamp(q.get("fault_time_utc"))
        counts = segment_counts(seg, "dim")
        dims = sorted(counts, key=lambda dim: (-counts[dim], str(dim)))
        dims = raw_plot_dims_with_vcb(dims, car_no)
        try:
            df_raw = get_raw_df(train, fault_utc, q.get("fault_time"), q.get("data_dir", DATA_DIR))
        except Exception as exc:
            return _placeholder(f"读取新故障原始数据失败：{exc}"), f"读取原始数据失败：{exc}"
        before_minutes, after_minutes = query_window_minutes(q)
        figure = build_combined_ts(
            df_raw,
            dims,
            fault_utc,
            seg=seg,
            car_no=car_no,
            window_before_min=before_minutes,
            window_after_min=after_minutes,
        )
        return figure, "已显示本次查询下载的新故障原始数据。"

    _vectors, metadata, _node_index = load_library()
    matches = metadata[metadata["work_order_no"].astype(str) == work_order].copy()
    if matches.empty:
        return _placeholder(f"工单 {work_order} 没有可绘图的 segment。"), f"工单 {work_order} 没有可绘图的 segment。"

    matches["_time_order_num"] = pd.to_numeric(matches.get("time_order"), errors="coerce").fillna(0)
    matches = matches.sort_values(["_time_order_num", "fault_time_utc"])
    row = matches.iloc[0]
    case_id = str(row.get("case_id", ""))
    seg = load_segments().get(case_id)
    train = normalize_train(row.get("train", ""))
    car_no = normalize_car_no(row.get("car_no", ""))
    fault_utc = pd.Timestamp(row.get("fault_time_utc"))
    fault_raw = pd.Timestamp(row.get("fault_time_raw"))

    counts = segment_counts(seg, "dim")
    dims = sorted(counts, key=lambda dim: (-counts[dim], str(dim)))
    dims = raw_plot_dims_with_vcb(dims, car_no)

    try:
        df_raw = get_historical_raw_df(
            train,
            fault_utc,
            fault_raw,
            raw_codes_for_dims(dims, car_no),
            RAW_PLOT_BEFORE_MIN,
            RAW_PLOT_AFTER_MIN,
        )
    except Exception as exc:
        return (
            _placeholder(f"读取工单 {work_order} 原始数据失败：{exc}"),
            f"读取原始数据失败：{exc}",
        )

    figure = build_combined_ts(
        df_raw,
        dims,
        fault_utc,
        seg=seg,
        car_no=car_no,
        window_before_min=RAW_PLOT_BEFORE_MIN,
        window_after_min=RAW_PLOT_AFTER_MIN,
    )
    figure.update_layout(
        title=dict(
            text=(
                f"<b>工单 {work_order} · 原始数据（故障前{RAW_PLOT_BEFORE_MIN * 60:g}秒 / 后{RAW_PLOT_AFTER_MIN * 60:g}秒）</b><br><br>"
                f"<span style='font-size:13px;color:#64748b'>车组 {train}　｜　车厢 {car_no}　｜　"
                f"故障时间 {fault_raw:%Y-%m-%d %H:%M:%S} CST　｜　红线=故障时刻</span>"
            ),
            font=dict(size=16), x=0.02, xanchor="left",
        ),
        dragmode="zoom",
    )
    status = (
        f"已自动加载故障前{RAW_PLOT_BEFORE_MIN * 60:g}秒、后{RAW_PLOT_AFTER_MIN * 60:g}秒。"
        "首次读取来自 Iceberg，随后会使用服务器磁盘缓存；可滚轮缩放或框选放大。"
    )
    return figure, status


@app.callback(
    Output("download_detail_csv", "data"),
    Input("download_detail_button", "n_clicks"),
    State("detail_table", "data"),
    prevent_initial_call=True,
)
def download_detail_csv(_n_clicks, rows):
    if not rows:
        raise PreventUpdate
    columns = ["序号", "时间(UTC)", "维度", "值", "较上一采样变化"]
    df = pd.DataFrame(rows).reindex(columns=columns)
    dim = str(rows[0].get("维度", "parameter"))
    safe_dim = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", dim).strip("_") or "parameter"
    content = df.to_csv(index=False).encode("utf-8-sig")
    return dcc.send_bytes(content, f"数据明细_{safe_dim}.csv", type="text/csv;charset=utf-8")


# 数据明细保持完整时间顺序，但数据更新后把视口定位到首个变化点附近。
app.clientside_callback(
    """
    function(rows) {
        if (!rows || !rows.length) {
            return window.dash_clientside.no_update;
        }
        var changeIndex = rows.findIndex(function(row) {
            return row && row['较上一采样变化'];
        });
        if (changeIndex < 0) {
            changeIndex = 0;
        }
        window.setTimeout(function() {
            var root = document.getElementById('detail_table');
            if (!root) return;
            var candidates = Array.from(root.querySelectorAll(
                '.dash-spreadsheet-inner, .dash-spreadsheet-container, div'
            ));
            var scroller = candidates.find(function(el) {
                var style = window.getComputedStyle(el);
                return el.scrollHeight > el.clientHeight + 20 &&
                    (style.overflowY === 'auto' || style.overflowY === 'scroll');
            });
            if (!scroller) return;
            var rowHeight = 30;
            scroller.scrollTop = Math.max(0, (changeIndex - 3) * rowHeight);
        }, 220);
        return Date.now();
    }
    """,
    Output("detail_scroll_sink", "data"),
    Input("detail_table", "data"),
    prevent_initial_call=True,
)


# 悬停只在浏览器内改线条样式，不请求 Python、不重读数据，也不触发 Loading 闪烁。
app.clientside_callback(
    """
    function(hoverData) {
        var root = document.getElementById('ts_combined');
        var gd = root && root.querySelector('.js-plotly-plot');
        if (!gd || !gd.data || !gd.layout) {
            return window.dash_clientside.no_update;
        }
        var selected = (gd.layout.meta && gd.layout.meta.selected_dim) || null;
        var highlighted = selected;
        if (hoverData && hoverData.points && hoverData.points.length) {
            var cd = hoverData.points[0].customdata;
            if (Array.isArray(cd)) {
                cd = cd.length ? cd[cd.length - 1] : null;
            }
            if (cd) {
                highlighted = String(cd);
            }
        }
        var indices = [], colors = [], widths = [], opacities = [];
        gd.data.forEach(function(trace, i) {
            if (trace.meta && trace.meta.kind === 'dimension') {
                var active = highlighted && String(trace.meta.dim) === String(highlighted);
                indices.push(i);
                colors.push(active ? '#e6194b' : trace.meta.base_color);
                widths.push(active ? 4.5 : 1.8);
                opacities.push(active ? 1.0 : 0.9);
            }
        });
        if (!indices.length) {
            return window.dash_clientside.no_update;
        }
        Plotly.restyle(gd, {'line.color': colors, 'line.width': widths, opacity: opacities}, indices);
        return Date.now();
    }
    """,
    Output("ts_hover_sink", "data"),
    Input("heatmap", "hoverData"),
    prevent_initial_call=True,
)


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
