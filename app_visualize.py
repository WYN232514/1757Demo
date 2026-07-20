# -*- coding: utf-8 -*-
"""
VCB/1757 相似工单可视化 —— 维度变化热力图版（Plotly Dash）

功能：
    输入一条新故障（车号 + 故障时间 + 车厢号 + 故障码）-> 网关下载 -> 生成故障指纹向量 ->
    检索 Top5 最相似历史工单 ->
    画一张「维度变化热力图」：
        行 = 新故障 + Top5 工单（共 6 行）
        列 = 各原始数据变量维度（dir_xxx 那些 code）
        格子 = 该案例在该维度上「变了几次」（0 = 没变，留白；变得越多颜色越深，并标数字）
    纵向看同一维度上新故障与 Top5 是否「同步变化」，一眼看出它们为什么相似、差异在哪。
    图上方给出 Top5 相似工单表格。

说明：
    - 复用 vcb_case_library_direct 的向量化逻辑，相似度在内存里算余弦，不依赖 Qdrant。
    - 每条案例「某维度变了几次」直接由它的 segment 记录统计得到（rcd[1] 即维度），
      与建库时的向量口径一致：
        * 新故障   -> get_local_case_result(...) 的 main_segment
        * 历史工单 -> case_library/segments.joblib（按 case_id 存）
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
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd

import dash
from dash import dcc, html, dash_table, callback_context, no_update
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
from scipy.cluster.hierarchy import fcluster, leaves_list, linkage
from scipy.spatial.distance import squareform

from vcb_case_library_direct import (
    DEFAULT_DATA_DIR,
    DEFAULT_SAVE_DIR,
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
)
from gateway_download import (
    DEFAULT_AFTER_MIN as GATEWAY_AFTER_MIN,
    DEFAULT_BEFORE_MIN as GATEWAY_BEFORE_MIN,
    download_fault_window,
)

# ============================================================
# 配置
# ============================================================

DATA_DIR = os.getenv("DATA_DIR", DEFAULT_DATA_DIR)
RAW_PLOT_BEFORE_MIN = 3
RAW_PLOT_AFTER_MIN = 3
LIBRARY_DIR = os.getenv("CASE_LIBRARY_DIR", DEFAULT_SAVE_DIR)
DEFAULT_TOP_K = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.7
CLUSTER_SIM_THRESHOLD = 0.7
HOST = "0.0.0.0"
PORT = 8050
FAULT_EXTRACTOR_DIR = Path(__file__).resolve().parent / "fault_extractor" / "fault_extractor"
FAULT_REFERENCE_DATA = FAULT_EXTRACTOR_DIR / "output" / "reference_data_with_cause_inferenced.json"

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

# 缓存最近一次查询结果：切换"只看新故障维度"开关时无需重新读原始数据
_LAST_QUERY: Dict[str, Any] = {"q": None}
# 缓存案例库与 segments（避免每次查询重复加载）
_CACHE: Dict[str, Any] = {}


# ============================================================
# 案例库 / segments 加载
# ============================================================


def load_library():
    d = Path(LIBRARY_DIR)
    watched = [d / "vectors.npy", d / "metadata.csv", d / "node_index.joblib"]
    signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in watched)
    if "lib" not in _CACHE or _CACHE.get("lib_signature") != signature:
        vectors = np.load(d / "vectors.npy").astype(float)
        metadata = pd.read_csv(d / "metadata.csv")
        node_index = joblib.load(d / "node_index.joblib")
        if EXCLUDE_SELECTED_CODES_FROM_SIMILARITY:
            excluded_cols = excluded_node_columns(node_index)
            vectors[:, excluded_cols] = 0.0
            vectors = normalize_vector_rows(vectors)
        _CACHE["lib"] = (vectors, metadata, node_index)
        _CACHE["lib_signature"] = signature
    return _CACHE["lib"]


def load_segments() -> Dict[str, Any]:
    p = Path(LIBRARY_DIR) / "segments.joblib"
    signature = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
    if "seg" not in _CACHE or _CACHE.get("seg_signature") != signature:
        _CACHE["seg"] = joblib.load(p) if p.exists() else {}
        _CACHE["seg_signature"] = signature
    return _CACHE["seg"]


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


def _load_name_map_file(path: Path, code_col: str, desc_col: str) -> Dict[str, str]:
    """Read code -> description display names."""
    try:
        df = pd.read_excel(path, dtype=str)
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        desc = str(row.get(desc_col, "")).strip()
        if not code or code.lower() == "nan" or not desc or desc.lower() == "nan":
            continue
        out[normalize_code(code)] = desc
    return out


def load_name_map() -> Dict[str, str]:
    """Use the supplemental code table as the single source for display names."""
    root = Path(__file__).resolve().parent
    code_col = "\u7f16\u7801"
    desc_col = "\u63cf\u8ff0"
    for path in root.glob("*.xlsx"):
        if path.name.startswith("~$"):
            continue
        try:
            columns = set(pd.read_excel(path, dtype=str, nrows=1).columns.astype(str))
        except Exception:
            continue
        if {code_col, desc_col}.issubset(columns):
            return _load_name_map_file(path, code_col, desc_col)
    return {}


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

    result = get_local_case_result(
        train=train,
        car_no=car_no,
        fault_time=fault_time,
        data_dir=data_dir,
        time_is_cst=time_is_cst,
    )
    status = result.get("status", "unknown")
    if status != "ok":
        return {"ok": False, "message": f"新故障未能生成向量，status = {status}"}

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
        if query_rf_label != history_rf_label:
            sims[idx] = min(float(raw_similarity), 0.30)
        elif shared_non_core == 0:
            sims[idx] = min(float(raw_similarity), 0.45)
        else:
            sims[idx] = min(1.0, 0.35 + 0.65 * detail_similarity)

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
    if not FAULT_REFERENCE_DATA.exists():
        raise FileNotFoundError(f"找不到 fault_extractor 参考数据：{FAULT_REFERENCE_DATA}")

    similarities: Dict[str, float] = {}
    metadata = q["metadata"]
    sims = q["sims"]
    for idx in q["top_idx"]:
        work_order = str(metadata.iloc[idx].get("work_order_no", "")).strip()
        if work_order:
            similarities[work_order] = max(similarities.get(work_order, 0.0), float(sims[idx]))

    # report_syn 使用同目录的 llm_api_tool；将完整 fault_extractor 项目放在导入路径最前。
    extractor_path = str(FAULT_EXTRACTOR_DIR)
    if extractor_path not in sys.path:
        sys.path.insert(0, extractor_path)
    from report_syn import report_synthesis_details

    return report_synthesis_details(similarities, FAULT_REFERENCE_DATA)


def _report_fact_items(facts: Mapping[str, Any], key: str, limit: int = 6) -> List[str]:
    items: List[str] = []
    for value in list(facts.get(key, []) or [])[:limit]:
        text = value.get("item") if isinstance(value, Mapping) else value
        text = str(text or "").strip()
        if text and text.lower() not in {"n/a", "na", "none"}:
            items.append(text)
    return items


def _report_list(items: Sequence[Any], class_name: str, ordered: bool = False):
    cleaned = [str(item).strip() for item in items if str(item or "").strip()]
    tag = html.Ol if ordered else html.Ul
    return tag([html.Li(item) for item in cleaned], className=class_name)


def render_fault_reference_report(details: Mapping[str, Any]):
    """将报告渲染成结论优先的检修决策卡片。"""
    cases = list(details.get("cases", []) or [])
    if not cases:
        return html.Div("参考数据中未找到匹配工单，暂时无法生成报告。", className="report-empty")

    facts = dict(details.get("synthesis_facts", {}) or {})
    narrative = dict(details.get("narrative", {}) or {})
    cause_focus = _report_fact_items(facts, "top_cause_components", 4)
    component_focus = _report_fact_items(facts, "top_components", 8)
    likely_locations = cause_focus or component_focus[:4]
    primary_location = likely_locations[0] if likely_locations else "VCB 相关控制链路"
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
        f"{len(cases)} 个匹配历史工单中，当前最应优先验证的是 {primary_location}。"
        "该判断用于安排排查顺序，不等同于根因已确认。"
    )

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
        ], className="report-evidence-row"))

    caution_items = list(narrative.get("cautions", []) or [])
    return html.Div([
        html.Section([
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
        ], className="report-hero"),

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
    ], className="fault-report")


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
    """新故障和网关入库工单读取各自缓存；老工单仍读取 01_数据整理。"""
    if ri == 0:
        return str(q.get("data_dir", DATA_DIR))
    idx = q["top_idx"][ri - 1]
    value = q["metadata"].iloc[idx].get("gateway_data_dir", "")
    text = str(value or "").strip()
    if text and text.lower() not in {"nan", "none", "null"}:
        return text
    return DATA_DIR


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
    if car in {"02", "05", "09"}:
        virtual_prefixes = {
            "target_vcb_904": f"904_2_1_{car}_sd",
            "target_vcb_936": f"936_2_1_{car}_sd",
            "target_ramping_finish_911": f"911_5_1_{car}_sd",
            "target_ramping_finish_943": f"943_5_1_{car}_sd",
        }
        if dim in virtual_prefixes:
            return virtual_prefixes[dim]
    return normalize_code(strip_dim(dim))


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


def build_single_ts(df_raw, dim, fault_utc, car_no: Any = None) -> go.Figure:
    """单变量原始逐采样时序：画满故障前后各3分钟，首尾延伸到窗口边界。"""
    label = dim_display(dim)
    t0 = pd.Timestamp(fault_utc) - pd.Timedelta(minutes=RAW_PLOT_BEFORE_MIN)
    t1 = pd.Timestamp(fault_utc) + pd.Timedelta(minutes=RAW_PLOT_AFTER_MIN)
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
    window_before_min: int = RAW_PLOT_BEFORE_MIN,
    window_after_min: int = RAW_PLOT_AFTER_MIN,
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


def build_detail_rows(df_raw, dim, fault_utc, car_no: Any = None):
    """所点参数在分析窗口内的原始逐采样明细行（给数据明细表）。"""
    label = dim_display(dim)
    t0 = pd.Timestamp(fault_utc) - pd.Timedelta(minutes=RAW_PLOT_BEFORE_MIN)
    t1 = pd.Timestamp(fault_utc) + pd.Timedelta(minutes=RAW_PLOT_AFTER_MIN)
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


def build_work_order_similarity_heatmap(query: Dict[str, Any] | None = None, show_details: bool = False) -> go.Figure:
    """RF分区 + 区内detail相似度的工单级热力图；按display_similarity>=0.7分簇。"""
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
        rf_lookup[new_label] = _segment_rf_label(query.get("new_seg"), query.get("car_no"))
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
                    rf_a = rf_lookup.get(wo_a, "RF=-")
                    rf_b = rf_lookup.get(wo_b, "RF=-")

                    if wo_a == wo_b:
                        display_similarity = 1.0
                    elif rf_a != rf_b:
                        display_similarity = min(raw_similarity, 0.30)
                    elif shared_non_core == 0:
                        display_similarity = min(raw_similarity, 0.45)
                    else:
                        display_similarity = min(1.0, 0.35 + 0.65 * detail_similarity)

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
    ordered_raw = raw_similarities[np.ix_(ordered_indices, ordered_indices)]
    ordered_detail = detail_similarities[np.ix_(ordered_indices, ordered_indices)]
    ordered_shared = shared_counts[np.ix_(ordered_indices, ordered_indices)]
    ordered_pairs = best_case_pairs[np.ix_(ordered_indices, ordered_indices)]

    max_shared_by_idx = shared_counts.copy()
    np.fill_diagonal(max_shared_by_idx, 0)
    max_detail_by_idx = detail_similarities.copy()
    np.fill_diagonal(max_detail_by_idx, 0)

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

    customdata = np.empty((n, n, 7), dtype=object)
    for row_pos, row_idx in enumerate(ordered_indices):
        for col_pos, col_idx in enumerate(ordered_indices):
            customdata[row_pos, col_pos] = [
                labels[row_idx],
                labels[col_idx],
                ordered_pairs[row_pos, col_pos],
                f"{ordered_raw[row_pos, col_pos]:.4f}",
                f"{ordered_detail[row_pos, col_pos]:.4f}",
                str(int(ordered_shared[row_pos, col_pos])),
                f"{ordered[row_pos, col_pos]:.4f}",
            ]

    text = np.empty((n, n), dtype=object)
    for row in range(n):
        for col in range(n):
            if show_details:
                text[row, col] = (
                    f"{ordered[row, col]:.2f}<br>"
                    f"R{ordered_raw[row, col]:.2f}<br>"
                    f"D{ordered_detail[row, col]:.2f}/N{int(ordered_shared[row, col])}"
                )
            else:
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
        textfont=dict(size=9 if show_details else 11),
        customdata=customdata,
        hovertemplate=(
            "<b>%{customdata[0]} → %{customdata[1]}</b>"
            "<br>最终相似度 display：%{customdata[6]}"
            "<br>原始相似度 raw：%{customdata[3]}"
            "<br>细节相似度 detail：%{customdata[4]}"
            "<br>共同非核心节点 N：%{customdata[5]}"
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
        xaxis=dict(title="先按 RF 分区；区内按 display similarity 完全链路分簇（阈值0.7）", tickangle=-55, side="bottom", automargin=True),
        yaxis=dict(
            title="红框=RF区域；白线=0.7簇边界；格子=display / raw / detail+N" if show_details else "红框=RF区域；白线=0.7簇边界；格子=最终相似度",
            autorange="reversed",
            automargin=True,
        ),
        annotations=[
            *list(fig.layout.annotations or []),
            dict(
                xref="paper", yref="paper", x=0, y=1.08, showarrow=False, align="left",
                text=(
                    "display=最终相似度；R=原始相似度；D=去掉VCB/RF后的细节相似度；N=共同非核心节点数。仅共享VCB/RF时最高封顶0.45。"
                    if show_details
                    else "格子数字为最终相似度 display similarity；红框为 RF 区域，白线为 0.7 簇边界。勾选下方开关可查看 R/D/N 细节。"
                ),
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
    """读取工单 Excel；只缓存业务字段，不依赖案例库 metadata 的空 long_description。"""
    if "work_order_records" in _CACHE:
        return _CACHE["work_order_records"]
    root = Path(__file__).resolve().parent
    records: Dict[str, Dict[str, str]] = {}
    candidates = [root / "工单.xlsx", *sorted(root.glob("*.xlsx")), root / "工单.xls"]
    seen_paths = set()
    for path in candidates:
        if path in seen_paths or not path.exists() or path.name.startswith("~$"):
            continue
        seen_paths.add(path)
        try:
            df = pd.read_excel(path, sheet_name=0, header=2, dtype=str)
        except Exception:
            continue
        if "Work Order NO." not in df.columns or "Long Description" not in df.columns:
            continue
        for _, row in df.iterrows():
            wo = clean_work_order_value(row.get("Work Order NO.")).removesuffix(".0")
            if not wo:
                continue
            records[wo] = {
                str(column): clean_work_order_value(row.get(column)) for column in df.columns
            }
        if records:
            break
    _CACHE["work_order_records"] = records
    return records


def load_structured_work_order_records() -> Dict[str, Dict[str, Any]]:
    """读取 fault_extractor 生成的结构化工单知识库。"""
    if "structured_work_order_records" in _CACHE:
        return _CACHE["structured_work_order_records"]
    records: Dict[str, Dict[str, Any]] = {}
    if FAULT_REFERENCE_DATA.exists():
        try:
            raw = json.loads(FAULT_REFERENCE_DATA.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                records = {str(key).strip(): value for key, value in raw.items() if isinstance(value, dict)}
        except Exception:
            records = {}
    _CACHE["structured_work_order_records"] = records
    return records


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


def render_work_order_detail(work_order_no: str):
    raw_work_order = str(work_order_no or "").strip()
    match = re.search(r"\d{10}", raw_work_order)
    wo = match.group(0) if match else raw_work_order.removeprefix("★").strip()
    if wo == "新故障" or "新故障" in raw_work_order:
        q = _LAST_QUERY.get("q")
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

app = dash.Dash(__name__)
app.title = "VCB 相似工单 · Excluded Codes"
server = app.server  # 供 gunicorn app_visualize:server 部署

HINT_FIG = _placeholder("⬆ 输入车号、故障时间、车厢号和故障码，点【查询】后自动下载并分析")
LIBRARY_SIMILARITY_FIG = build_work_order_similarity_heatmap()
_DETAIL_METADATA = load_library()[1]
DETAIL_WORK_ORDERS = sorted(_DETAIL_METADATA["work_order_no"].astype(str).dropna().unique().tolist())

app.layout = html.Div(
    className="app-shell",
    children=[
        dcc.Store(id="ts_selection", data=None),
        dcc.Store(id="query_revision", data=None),
        dcc.Store(id="ts_hover_sink", data=None),
        dcc.Store(id="detail_scroll_sink", data=None),
        dcc.Store(id="work_order_detail_selection", data=None),
        dcc.Store(id="current_work_order_detail", data=None),
        html.H2("VCB / 1757 相似工单分析", className="app-title"),
        html.Div(
            "Excluded Codes · 排除 9 个低区分度 code 后重新归一化",
            className="mode-badge",
        ),
        html.Div(
            className="control-panel",
            style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "alignItems": "flex-end", "marginBottom": "10px"},
            children=[
                html.Div([html.Label("车号（如 T9）"), dcc.Input(id="train", type="text", value="", style={"width": "120px"})]),
                html.Div([html.Label("故障时间（YYYY-MM-DD HH:MM:SS）"), dcc.Input(id="fault_time", type="text", value="", style={"width": "220px"})]),
                html.Div([html.Label("车厢号（如 02/05/09）"), dcc.Input(id="car_no", type="text", value="", style={"width": "110px"})]),
                html.Div([html.Label("故障码（如 P026）"), dcc.Input(id="fault_code", type="text", value="", style={"width": "120px"})]),
                html.Div([html.Label("Top K"), dcc.Input(id="top_k", type="number", value=DEFAULT_TOP_K, min=1, max=20, style={"width": "80px"})]),
                html.Div([html.Label("最低相似度"), dcc.Input(
                    id="similarity_threshold", type="number", value=DEFAULT_SIMILARITY_THRESHOLD,
                    min=0, max=1, step=0.05, style={"width": "100px"},
                )]),
                dcc.Checklist(id="time_is_cst", options=[{"label": " 时间是北京时间(CST)", "value": "cst"}], value=["cst"]),
                dcc.Checklist(id="only_new", options=[{"label": " 只显示新故障变化过的列", "value": "only"}], value=["only"]),
                html.Button("查询相似工单", id="run", n_clicks=0, className="primary-button"),
            ],
        ),
        html.Div(id="status", className="status-line"),
        dcc.Tabs(
            id="main_tabs", value="tab_top_report",
            style={"marginTop": "12px"},
            colors={"border": "#dbe3ec", "primary": "#2563eb", "background": "#f8fafc"},
            children=[
                dcc.Tab(label="相似工单与报告", value="tab_top_report", children=[
                    html.Div(className="section-card", style={"marginTop": "12px"}, children=[
                        html.H3("Top 相似工单", className="section-title"),
                        dash_table.DataTable(
                            id="top_table",
                            data=[],
                            columns=[{"name": c, "id": c} for c in ["排名", "工单号", "车号", "故障时间", "相似度", "变化维度数", "备注"]],
                            style_cell={"fontFamily": "Microsoft YaHei, Arial", "textAlign": "left", "padding": "9px", "maxWidth": "320px", "whiteSpace": "normal", "border": "none"},
                            style_header={"fontWeight": "bold", "backgroundColor": "#f3f6fa", "border": "none"},
                            style_data_conditional=[{"if": {"row_index": 0}, "backgroundColor": "#fff1f3"}],
                        ),
                    ]),
                    html.Div(className="section-card report-section", style={"marginTop": "12px"}, children=[
                        html.Div(className="report-section-heading", children=[
                            html.Div([
                                html.Div("故障维修参考报告", className="report-section-title"),
                                html.Div("结论优先 · 排查与维修建议", className="report-section-subtitle"),
                            ]),
                            html.Span("基于相似历史工单", className="report-source-badge"),
                        ]),
                        dcc.Loading(type="default", children=html.Div(
                            id="fault_report_panel", children=html.Div(
                                "完成相似工单查询后自动生成参考报告。",
                                className="report-empty-state",
                            ),
                            className="fault-report-panel",
                        )),
                    ]),
                ]),
                dcc.Tab(label="变化与原始数据", value="tab_changes", children=[
                    dcc.Loading(type="default", children=[
                        html.Div(className="section-card graph-card", style={"overflowX": "auto", "marginTop": "12px"}, children=[
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
                dcc.Tab(label="案例库工单", value="tab_library", children=[
                    html.Div(className="section-card cluster-card", style={"marginTop": "12px"}, children=[
                        dcc.Graph(id="work_order_similarity_heatmap", figure=LIBRARY_SIMILARITY_FIG, config={"displaylogo": False, "responsive": True, "toImageButtonOptions": {"format": "png", "filename": "work_order_similarity_heatmap", "scale": 2}}),
                        dcc.Checklist(
                            id="work_order_similarity_detail_toggle",
                            options=[{"label": " 显示 R/D/N 细节", "value": "detail"}],
                            value=[],
                            style={"margin": "2px 4px 8px", "fontSize": "13px", "color": "#374151"},
                        ),
                        html.Div(
                            "热力图使用 RF 分区 + 区内二级相似度：颜色和格子数字均为最终相似度 display similarity。红框表示 RF 区域，白线表示按 0.7 阈值划分的簇。勾选“显示 R/D/N 细节”后，格子内会显示 R=原始相似度、D=去掉 VCB/RF 后的细节相似度、N=共同非核心节点数。",
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
                                    "生成该工单原始数据图",
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
                                        figure=_placeholder("选择工单后点击“生成该工单原始数据图”"),
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
                    ]),
                ]),
            ],
        ),
    ],
)


@app.callback(
    Output("heatmap", "figure"),
    Output("top_table", "data"),
    Output("status", "children"),
    Output("query_revision", "data"),
    Output("work_order_similarity_heatmap", "figure"),
    Input("run", "n_clicks"),
    Input("only_new", "value"),
    Input("work_order_similarity_detail_toggle", "value"),
    State("train", "value"),
    State("car_no", "value"),
    State("fault_time", "value"),
    State("fault_code", "value"),
    State("top_k", "value"),
    State("similarity_threshold", "value"),
    State("time_is_cst", "value"),
    prevent_initial_call=True,
)
def on_run(n_clicks, only_new_value, work_order_detail_value, train, car_no, fault_time, fault_code, top_k, similarity_threshold, time_is_cst_value):
    only_new = "only" in (only_new_value or [])
    show_work_order_details = "detail" in (work_order_detail_value or [])
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else None

    # 只切换显示列开关：用缓存重画，不重读原始数据
    if triggered == "only_new":
        q = _LAST_QUERY.get("q")
        if not q:
            raise PreventUpdate
        return build_heatmap(q, only_new, "node", "count"), no_update, no_update, no_update, no_update
    if triggered == "work_order_similarity_detail_toggle":
        q = _LAST_QUERY.get("q")
        return no_update, no_update, no_update, no_update, build_work_order_similarity_heatmap(q, show_work_order_details)

    train = (train or "").strip()
    car_no = normalize_car_no((car_no or "").strip())
    fault_time = (fault_time or "").strip()
    fault_code = (fault_code or "").strip().upper()
    if not train or not car_no or not fault_time or not fault_code:
        # Top 工单和参考报告必须同时清空，避免报告继续显示上一次查询结果。
        _LAST_QUERY["q"] = None
        return HINT_FIG, [], "请填写车号、故障时间、车厢号和故障码。", pd.Timestamp.utcnow().isoformat(), no_update

    time_is_cst = "cst" in (time_is_cst_value or [])
    top_k = int(top_k or DEFAULT_TOP_K)
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
            before_min=GATEWAY_BEFORE_MIN,
            after_min=GATEWAY_AFTER_MIN,
            metrics_data_dir=DATA_DIR,
            reuse=True,
        )
    except Exception as e:
        _LAST_QUERY["q"] = None
        return HINT_FIG, [], f"网关数据下载失败: {e}", pd.Timestamp.utcnow().isoformat(), no_update

    try:
        q = compute_query(
            train=train,
            car_no=car_no,
            fault_time=fault_time,
            fault_code=fault_code,
            data_dir=download_result.data_dir,
            download_info=download_result,
            time_is_cst=time_is_cst,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )
    except FileNotFoundError as e:
        _LAST_QUERY["q"] = None
        return HINT_FIG, [], f"案例库或原始数据缺失: {e}", pd.Timestamp.utcnow().isoformat(), no_update
    except Exception as e:
        _LAST_QUERY["q"] = None
        return HINT_FIG, [], f"查询出错: {e}", pd.Timestamp.utcnow().isoformat(), no_update

    if not q.get("ok"):
        _LAST_QUERY["q"] = None
        return HINT_FIG, [], q.get("message", "查询失败"), pd.Timestamp.utcnow().isoformat(), no_update

    q["revision"] = pd.Timestamp.utcnow().isoformat()
    _LAST_QUERY["q"] = q
    base_status = (
        f"故障码 {q['fault_code']}；网关{'复用缓存' if q['download_info'].reused else '下载完成'} "
        f"{q['download_info'].metric_count} 个变量、{q['download_info'].row_count} 行；"
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
            "仍显示新故障的变化热力图、原始数据，以及下方包含新故障的工单聚类热力图。"
            + base_status
        )
    return (
        build_heatmap(q, only_new, "node", "count"),
        make_top_rows(q), status, q["revision"],
        build_work_order_similarity_heatmap(q, show_work_order_details),
    )


@app.callback(
    Output("fault_report_panel", "children"),
    Input("query_revision", "data"),
    Input("top_table", "data"),
    prevent_initial_call=True,
)
def generate_fault_report(_query_revision, top_rows):
    # 报告与 Top 工单使用同一显示条件。刷新页面或新查询清空 Top 工单时，
    # 不允许继续从进程级缓存中恢复上一份报告。
    if not top_rows:
        return html.Div("请先查询相似工单。", className="report-empty-state report-empty-state-error")
    q = _LAST_QUERY.get("q")
    if not q:
        return html.Div("请先查询相似工单。", className="report-empty-state report-empty-state-error")
    if not q.get("top_idx"):
        return html.Div(
            "当前查询没有达到相似度阈值的工单，暂时无法生成报告。",
            className="report-empty-state",
        )
    try:
        report = build_fault_reference_report(q)
    except Exception as exc:
        return html.Div(
            f"报告生成失败：{exc}",
            className="report-empty-state report-empty-state-error",
        )
    return render_fault_reference_report(report)


@app.callback(
    Output("ts_combined", "figure"),
    Output("ts_header", "children"),
    Output("detail_table", "data"),
    Output("ts_selection", "data"),
    Input("heatmap", "clickData"),
    Input("query_revision", "data"),
    State("ts_selection", "data"),
    prevent_initial_call=True,
)
def on_cell_interaction(click_data, query_revision, selection):
    """点击固定案例/维度并更新原始时序与数据明细。"""
    triggered = callback_context.triggered[0]["prop_id"] if callback_context.triggered else ""
    q = _LAST_QUERY.get("q")
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
            figure = build_combined_ts(df_raw, dims, futc, highlight_dim=default_dim, seg=seg, car_no=q.get("car_no"))
            detail = build_detail_rows(df_raw, default_dim, futc, car_no=q.get("car_no")) if default_dim else []
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
        df_raw = get_raw_df(train, futc, file_partition_time, row_data_dir(q, ri))
    except Exception as e:
        msg = f"读取原始数据失败：{e}"
        return _placeholder(msg), header, [], selection

    counts = segment_counts(seg, "dim")
    dims = set(counts.keys())
    dims.add(selected_dim)
    dims = sorted(dims, key=lambda d: (-counts.get(d, 0), str(d)))
    car_no = row_car_no(q, ri)
    dims = raw_plot_dims_with_vcb(dims, car_no)
    combined = build_combined_ts(df_raw, dims, futc, highlight_dim=selected_dim, seg=seg, car_no=car_no)
    detail = build_detail_rows(df_raw, selected_dim, futc, car_no=car_no)
    return combined, header, detail, selection


@app.callback(
    Output("work_order_detail_panel", "children"),
    Output("current_work_order_detail", "data"),
    Input("work_order_detail_selection", "data"),
    Input("work_order_detail_selector", "value"),
    prevent_initial_call=True,
)
def show_work_order_detail(selection, selected_work_order):
    triggered = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if triggered == "work_order_detail_selector":
        work_order = _extract_work_order_no_from_any(selected_work_order)
    else:
        work_order = _extract_work_order_no_from_any((selection or {}).get("work_order"))
    if not work_order:
        raise PreventUpdate
    return render_work_order_detail(work_order), {"work_order": work_order}


@app.callback(
    Output("work_order_raw_plot", "figure"),
    Output("work_order_raw_plot_status", "children"),
    Input("generate_work_order_raw_plot", "n_clicks"),
    State("current_work_order_detail", "data"),
    prevent_initial_call=True,
)
def generate_work_order_raw_plot(_n_clicks, current_detail):
    work_order = _extract_work_order_no_from_any((current_detail or {}).get("work_order"))
    if not work_order:
        return _placeholder("请先选择工单。"), "请先从下拉框选择工单，或点击坐标轴上的工单号。"

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

    try:
        gateway_dir = str(row.get("gateway_data_dir", "") or "").strip()
        source_dir = gateway_dir if gateway_dir.lower() not in {"", "nan", "none", "null"} else DATA_DIR
        df_raw = get_raw_df(train, fault_utc, fault_raw, source_dir)
    except Exception as exc:
        return _placeholder(f"读取工单 {work_order} 原始数据失败：{exc}"), f"读取原始数据失败：{exc}"

    counts = segment_counts(seg, "dim")
    dims = sorted(counts, key=lambda dim: (-counts[dim], str(dim)))
    dims = raw_plot_dims_with_vcb(dims, car_no)
    figure = build_combined_ts(
        df_raw,
        dims,
        fault_utc,
        seg=seg,
        car_no=car_no,
        window_before_min=10,
        window_after_min=10,
    )
    figure.update_layout(
        title=dict(
            text=(
                f"<b>工单 {work_order} · 原始数据（故障前后各10分钟）</b><br><br>"
                f"<span style='font-size:13px;color:#64748b'>车组 {train}　｜　车厢 {car_no}　｜　"
                f"故障时间 {fault_raw:%Y-%m-%d %H:%M:%S} CST　｜　红线=故障时刻</span>"
            ),
            font=dict(size=16), x=0.02, xanchor="left",
        ),
        dragmode="zoom",
    )
    status = (
        f"当前绘制案例：{case_id}；初始范围为故障前后各10分钟。"
        "可滚轮缩放或框选放大，双击图表恢复完整时间范围。"
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
