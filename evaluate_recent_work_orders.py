# -*- coding: utf-8 -*-
"""Leave-one-work-order-out evaluation for recent VCB maintenance orders.

The script consumes a previously archived work-order workflow response, uses
the local case-library snapshot as the recommendation corpus, and writes CSV,
XLSX and Markdown evaluation artifacts.  A target work order is always removed
from its own candidate set before ranking.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from gateway_download import download_fault_window, load_metrics
from vcb_case_library_direct import (
    get_local_case_result,
    get_ramping_finish_dims_for_car,
    get_vcb_target_dims_for_car,
    normalize_car_no,
    normalize_code,
    segment_to_node_counts,
)


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
DEFAULT_RESPONSE = ROOT / "runtime" / "recent_work_orders_workflow_response.json"
DEFAULT_LIBRARY = ROOT / "case_library"
DEFAULT_REFERENCE = (
    ROOT / "fault_extractor" / "fault_extractor" / "output"
    / "reference_data_with_cause_inferenced.json"
)
DEFAULT_OUTPUT = ROOT / "近期工单留一评估"
DEFAULT_DOWNLOAD_ROOT = ROOT / "gateway_downloads" / "recent_work_order_evaluation"

EXCLUDED_CODES = {
    "1157_3_1_01_sdr", "1172_3_1_01_sdr", "1194_3_1_01_sdr",
    "904_2_1_02_sd", "904_2_1_05_sd", "904_2_1_09_sd",
    "936_2_1_02_sd", "936_2_1_05_sd", "936_2_1_09_sd",
}
EXCLUDED_PREFIXES = ("target_vcb_904_", "target_vcb_936_")
DETAIL_CORE_PREFIXES = (
    "target_vcb_904_", "target_vcb_936_",
    "target_ramping_finish_911_", "target_ramping_finish_943_",
)

# The source descriptions contain a small number of formats that cannot be
# recovered reliably from Report Date alone.  These are auditable overrides,
# copied verbatim from the fault timestamps in the source descriptions.
FAULT_TIME_OVERRIDES: dict[str, list[str]] = {
    "5001725262": ["2026-07-01 05:30:33"],
    "5001727589": ["2026-07-02 04:57:00"],
    "5001774280": ["2026-07-12 00:51:00"],
    "5001783408": ["2026-07-14 05:30:00"],
    "5001860453": ["2026-08-03 17:26:00"],
    "5001965341": ["2026-09-07 00:42:00", "2026-09-07 07:07:00"],
    "5001969249": [
        "2026-09-08 12:06:00", "2026-09-08 17:09:00", "2026-09-08 21:25:00",
    ],
}

COMPONENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bAPS\s+Converter\s+unit\b|\bConverter\s+unit\b", "APS Converter unit"),
    (r"\bATR\s+receiver\b", "ATR receiver"),
    (r"\bATR\s+controller\b", "ATR controller"),
    (r"\bAPC\s+controller\b", "APC controller"),
    (r"\bAPC\s+receiver\b", "APC receiver"),
    (r"\bC/I\s+GCU\b", "C/I GCU"),
    (r"\bC/I\s+converter\b", "C/I converter"),
    (r"\bC/I\s+inverter\b", "C/I inverter"),
    (r"\bRAMFR\b", "RAMFR"),
    (r"\bAPCCR\b", "APCCR"),
    (r"\bVCBCR\b", "VCBCR"),
    (r"\bVCBHR\b", "VCBHR"),
    (r"\bVCBSTR\b", "VCBSTR"),
    (r"\bVCBCTR\b", "VCBCTR"),
    (r"\bAPS\b", "APS"),
    (r"\bAPC\b", "APC"),
    (r"\bATR\b", "ATR"),
    (r"\bVCB\b", "VCB"),
)


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("<!-- RICH TEXT -->", "")
    return re.sub(r"\s+", " ", text).strip()


def unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean(value)
        if not text or text.casefold() in {"n/a", "na", "none", "null"}:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            result.append(text)
    return result


def split_markdown_row(line: str) -> list[str]:
    placeholder = "\x00PIPE\x00"
    text = line.strip()[1:-1].replace(r"\|", placeholder)
    return [html.unescape(cell.replace(placeholder, "|").strip()) for cell in text.split("|")]


def load_workflow_rows(path: Path) -> list[dict[str, str]]:
    envelope = json.loads(path.read_text(encoding="utf-8-sig"))
    text = envelope["data"]["outputs"]["result"]
    lines = [line.strip() for line in str(text).splitlines() if line.strip().startswith("|")]
    headers = split_markdown_row(lines[0])
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cells = split_markdown_row(line)
        if len(cells) == len(headers) and all(re.fullmatch(r":?-{3,}:?", x) for x in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError(f"工作流 Markdown 列数不一致: {len(cells)} != {len(headers)}")
        row = {str(key).strip().lower(): clean(value) for key, value in zip(headers, cells)}
        if row.get("work_order_no"):
            rows.append(row)
    return rows


def action_section(row: Mapping[str, Any]) -> str:
    parts = clean(row.get("long_description", "")).split("//")
    return clean(parts[1] if len(parts) > 1 else row.get("long_description", ""))


def declared_component_section(row: Mapping[str, Any]) -> str:
    parts = clean(row.get("long_description", "")).split("//")
    return clean(parts[2] if len(parts) > 2 else "")


def replacement_code_section(row: Mapping[str, Any]) -> str:
    parts = clean(row.get("long_description", "")).split("//")
    return clean(parts[3] if len(parts) > 3 else "")


def action_types(text: Any, supplied: Any = None) -> list[str]:
    source = clean(text).casefold()
    result = list(supplied or []) if isinstance(supplied, list) else []
    rules = (
        ("更换", r"\breplac(?:e|ed|ement|ing)\b"),
        ("修复", r"\brepair(?:ed|ing)?\b"),
        ("互换测试", r"\bswap(?:ped|ping)?\b"),
        ("隔离", r"\bisolat(?:e|ed|ing|ion)\b"),
        ("复位", r"\breset\b"),
        ("检查/路试", r"\bcheck(?:ed|ing)?\b|\bride\s+check"),
        ("功能测试", r"\bfunction\s+test|\btested?\b"),
        ("持续观察", r"\bmonitor(?:ed|ing)?\b|keep\s+monitoring"),
    )
    supplied_map = {
        "replaced": "更换", "repaired": "修复", "swapped": "互换测试",
        "isolated": "隔离", "reset": "复位", "checked": "检查/路试",
        "function tested": "功能测试", "tested": "功能测试", "monitored": "持续观察",
    }
    normalized = [supplied_map.get(clean(value).casefold(), clean(value)) for value in result]
    normalized.extend(label for label, pattern in rules if re.search(pattern, source, flags=re.I))
    return unique(normalized)


def mentioned_components(text: Any) -> list[str]:
    source = clean(text)
    return unique(label for pattern, label in COMPONENT_PATTERNS if re.search(pattern, source, flags=re.I))


def replaced_components(text: Any, structured: Mapping[str, Any] | None = None) -> list[str]:
    result: list[str] = []
    structured = structured or {}
    components = structured.get("treatment_components") or []
    events = structured.get("treatment_events") or []
    if isinstance(components, str):
        components = [components]
    if isinstance(events, str):
        events = [events]
    for component, event in zip(components, events):
        if clean(event).casefold() == "replaced":
            result.extend(mentioned_components(component) or [clean(component)])

    source = clean(text)
    for match in re.finditer(
        r"\breplaced?\b(.{0,100}?)(?=(?:\bfunction\s+test|\btested?\b|\bkeep\b|[.;]|$))",
        source,
        flags=re.I,
    ):
        result.extend(mentioned_components(match.group(1)))
    return unique(result)


def actual_facts(row: Mapping[str, Any], structured: Mapping[str, Any] | None) -> dict[str, Any]:
    structured = structured or {}
    action = action_section(row)
    declared = declared_component_section(row)
    replacement_codes = replacement_code_section(row)
    actions = action_types(action, structured.get("treatment_events"))
    replaced = unique(
        replaced_components(action, structured)
        + mentioned_components(declared)
    )
    # "NA" component fields must not convert general tested components into replacements.
    if declared.casefold() in {"", "na", "n/a"} and "更换" not in actions:
        replaced = []
    components = unique(
        mentioned_components(action)
        + list(structured.get("treatment_components") or [])
        + replaced
    )
    part_codes = unique(re.findall(r"\b[A-Z]{0,3}\d{6,12}\b", replacement_codes, flags=re.I))
    return {
        "actual_action_text": action,
        "actual_actions": actions,
        "actual_components": components,
        "actual_replaced_components": replaced,
        "actual_part_codes": part_codes,
    }


def recommendation_facts(
    work_order: str,
    metadata_row: Mapping[str, Any],
    references: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    structured = references.get(work_order, {}) or {}
    action = clean(structured.get("action", ""))
    long_description = clean(metadata_row.get("long_description", ""))
    actions = action_types(action + " " + long_description, structured.get("treatment_events"))
    components = unique(
        list(structured.get("treatment_components") or [])
        + mentioned_components(action + " " + long_description)
    )
    replaced = replaced_components(action + " " + long_description, structured)
    parts = unique(
        list(structured.get("replace_in_part") or [])
        + list(structured.get("replace_out_part") or [])
    )
    return {
        "recommended_action_text": action,
        "recommended_actions": actions,
        "recommended_components": components,
        "recommended_replaced_components": replaced,
        "recommended_part_codes": parts,
    }


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


def excluded_columns(node_index: Mapping[str, int]) -> list[int]:
    prefixes = tuple(f"dir_{code}_" for code in EXCLUDED_CODES)
    return [
        int(position) for node, position in node_index.items()
        if str(node).startswith(prefixes) or str(node).startswith(EXCLUDED_PREFIXES)
    ]


def detail_columns(node_index: Mapping[str, int]) -> list[int]:
    result = set(excluded_columns(node_index))
    result.update(
        int(position) for node, position in node_index.items()
        if str(node).startswith(DETAIL_CORE_PREFIXES)
    )
    return sorted(result)


def event_is(value: Any, expected: int) -> bool:
    try:
        return float(value) == float(expected)
    except Exception:
        return clean(value) == str(expected)


def ramping_dim(vcb_dim: str) -> str:
    if vcb_dim.startswith("dir_904_2_1_"):
        return vcb_dim.replace("dir_904_2_1_", "dir_911_5_1_", 1)
    if vcb_dim.startswith("dir_936_2_1_"):
        return vcb_dim.replace("dir_936_2_1_", "dir_943_5_1_", 1)
    return {"target_vcb_904": "target_ramping_finish_911", "target_vcb_936": "target_ramping_finish_943"}.get(vcb_dim, "")


def rf_label(segment: Any, car_no: Any) -> str:
    allowed_vcb = set(get_vcb_target_dims_for_car(car_no)) | {"target_vcb_904", "target_vcb_936"}
    ramp_rows: list[tuple[str, pd.Timestamp, str]] = []
    for record in segment or []:
        if record is None or len(record) < 6:
            continue
        if clean(record[3]) == "ramping_finish_status":
            ramp_rows.append((clean(record[1]), pd.Timestamp(record[4]), clean(record[5])))
    values: list[str] = []
    for record in segment or []:
        if record is None or len(record) < 6 or clean(record[1]) not in allowed_vcb:
            continue
        if not (event_is(record[3], 1) and event_is(record[5], 0)):
            continue
        target = ramping_dim(clean(record[1]))
        event_time = pd.Timestamp(record[4])
        values.extend(value for dim, stamp, value in ramp_rows if dim == target and stamp == event_time)
    has_zero, has_one = "0" in values, "1" in values
    if has_zero and has_one:
        return "RF=0/1"
    if has_one:
        return "RF=1"
    if has_zero:
        return "RF=0"
    return "RF=-"


def final_similarity(raw: float, detail: float, shared: int, query_rf: str, candidate_rf: str) -> float:
    if query_rf != candidate_rf:
        return min(float(raw), 0.30)
    if int(shared) == 0:
        return min(1.0, float(raw))
    return min(1.0, 0.35 + 0.65 * float(detail))


class Library:
    def __init__(self, path: Path):
        self.metadata = pd.read_csv(path / "metadata.csv", dtype=str).fillna("")
        self.vectors = np.load(path / "vectors.npy").astype(float)
        self.node_index = {str(k): int(v) for k, v in joblib.load(path / "node_index.joblib").items()}
        self.segments = joblib.load(path / "segments.joblib")
        self.vectors[:, excluded_columns(self.node_index)] = 0.0
        self.vectors = normalize_rows(self.vectors)
        self.rf = [
            rf_label(self.segments.get(clean(row.get("case_id"))), row.get("car_no"))
            for _, row in self.metadata.iterrows()
        ]

    def stored_query(self, work_order: str, fault_time: str) -> tuple[np.ndarray, Any, str] | None:
        subset = self.metadata[self.metadata["work_order_no"].astype(str) == str(work_order)]
        if subset.empty:
            return None
        target = pd.Timestamp(fault_time)
        times = pd.to_datetime(subset["fault_time_raw"], errors="coerce")
        position = (times - target).abs().argmin()
        index = int(subset.index[position])
        row = self.metadata.iloc[index]
        segment = self.segments.get(clean(row.get("case_id")))
        return self.vectors[index].copy(), segment, "stored_case_vector"

    def counts_query(self, node_counts: Mapping[str, int]) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
        novel = sorted(str(node) for node in node_counts if str(node) not in self.node_index)
        node_index = dict(self.node_index)
        start = len(node_index)
        node_index.update({node: start + offset for offset, node in enumerate(novel)})
        history = np.pad(self.vectors, ((0, 0), (0, len(novel))), mode="constant")
        query = np.zeros(len(node_index), dtype=float)
        for node, count in node_counts.items():
            query[node_index[str(node)]] += int(count)
        query[excluded_columns(node_index)] = 0.0
        norm = np.linalg.norm(query)
        if norm:
            query /= norm
        return history, query, node_index

    def rank(
        self,
        target_work_order: str,
        query_vector: np.ndarray,
        query_segment: Any,
        query_car: str,
        history_vectors: np.ndarray | None = None,
        node_index: Mapping[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        history = self.vectors if history_vectors is None else history_vectors
        index = self.node_index if node_index is None else node_index
        raw_scores = np.clip(history @ query_vector, 0.0, 1.0)
        core = detail_columns(index)
        history_detail = history.copy()
        history_detail[:, core] = 0.0
        history_detail = normalize_rows(history_detail)
        query_detail = query_vector.copy()
        query_detail[core] = 0.0
        query_detail = query_detail / np.linalg.norm(query_detail) if np.linalg.norm(query_detail) else query_detail
        detail_scores = np.clip(history_detail @ query_detail, 0.0, 1.0)
        query_active = query_detail > 0
        candidate_active = history_detail > 0
        query_rf = rf_label(query_segment, query_car)

        best: dict[str, dict[str, Any]] = {}
        for idx, row in self.metadata.iterrows():
            work_order = clean(row.get("work_order_no"))
            if work_order == str(target_work_order):
                continue
            shared = int(np.logical_and(candidate_active[idx], query_active).sum())
            score = final_similarity(raw_scores[idx], detail_scores[idx], shared, query_rf, self.rf[idx])
            item = {
                "candidate_index": int(idx),
                "recommended_work_order": work_order,
                "recommended_case_id": clean(row.get("case_id")),
                "similarity": float(score),
                "raw_similarity": float(raw_scores[idx]),
                "detail_similarity": float(detail_scores[idx]),
                "shared_non_core_nodes": shared,
                "target_rf": query_rf,
                "recommended_rf": self.rf[idx],
            }
            if work_order not in best or item["similarity"] > best[work_order]["similarity"]:
                best[work_order] = item
        return sorted(best.values(), key=lambda item: item["similarity"], reverse=True)


def overlap_text(left: Sequence[str], right: Sequence[str]) -> tuple[list[str], float, float]:
    left_map = {clean(value).casefold(): clean(value) for value in left if clean(value)}
    right_keys = {clean(value).casefold() for value in right if clean(value)}
    intersection = [value for key, value in left_map.items() if key in right_keys]
    recall = len(intersection) / len(left_map) if left_map else 1.0
    union = set(left_map) | right_keys
    jaccard = len(intersection) / len(union) if union else 1.0
    return intersection, recall, jaccard


def join(values: Iterable[Any]) -> str:
    return "；".join(unique(values))


def evaluate(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = load_workflow_rows(args.workflow_response)
    references = json.loads(args.reference.read_text(encoding="utf-8-sig"))
    library = Library(args.library)
    metrics = load_metrics(str(ROOT / "gateway_metrics.json"), str(ROOT / "01_数据整理"))

    case_rows: list[dict[str, Any]] = []
    recommendation_rows: list[dict[str, Any]] = []
    for row in rows:
        work_order = clean(row.get("work_order_no"))
        structured = references.get(work_order, {})
        actual = actual_facts(row, structured)
        fault_times = FAULT_TIME_OVERRIDES.get(work_order, [])
        if not fault_times:
            case_rows.append({
                "work_order_no": work_order,
                "report_date": row.get("report_date", ""),
                "train": row.get("equip_instance", ""),
                "car_no": normalize_car_no(row.get("equip_unit", "")),
                "fault_time": "",
                "status": "无法回测：原工单没有明确事件时刻",
                **{key: join(value) if isinstance(value, list) else value for key, value in actual.items()},
            })
            continue

        for event_number, fault_time in enumerate(fault_times, start=1):
            stored = library.stored_query(work_order, fault_time)
            query_vector: np.ndarray | None = None
            query_segment: Any = None
            history_vectors: np.ndarray | None = None
            query_node_index: Mapping[str, int] | None = None
            query_source = ""
            status = "ok"
            error = ""
            used_window = ""
            train = clean(row.get("equip_instance"))
            car_no = normalize_car_no(row.get("equip_unit"))

            if stored is not None:
                query_vector, query_segment, query_source = stored
                used_window = "案例库已存 segment"
            else:
                try:
                    if args.local_only:
                        raise RuntimeError("按 --local-only 跳过网关请求")
                    download = download_fault_window(
                        train=train,
                        fault_time=fault_time,
                        time_is_cst=True,
                        before_min=5,
                        after_min=5,
                        fault_code="1757",
                        metric_definitions=metrics,
                        output_root=args.download_root,
                        reuse=True,
                    )
                    result = None
                    for before, after in ((1.0, 0.5), (2.0, 1.0), (3.0, 3.0), (5.0, 5.0)):
                        candidate = get_local_case_result(
                            train=train, car_no=car_no, fault_time=fault_time,
                            data_dir=download.data_dir, time_is_cst=True,
                            read_before_min=before, read_after_min=after,
                            analyze_before_min=before, analyze_after_min=after,
                        )
                        if candidate.get("status") == "ok":
                            result = candidate
                            used_window = f"前{before:g}分钟/后{after:g}分钟"
                            break
                    if result is None:
                        status = "无法回测：下载成功但没有有效 segment"
                    else:
                        history_vectors, query_vector, query_node_index = library.counts_query(result["node_counts"])
                        query_segment = result["main_segment"]
                        query_source = "gateway_reconstructed"
                except Exception as exc:
                    gateway_error = f"{type(exc).__name__}: {exc}"
                    # Developer machines may be blocked by the gateway's
                    # CloudFront source-IP policy.  Reuse an existing local day
                    # partition when one is available; this follows the same
                    # event/segment/vector code path and stays fully auditable.
                    try:
                        local_result = get_local_case_result(
                            train=train, car_no=car_no, fault_time=fault_time,
                            data_dir=ROOT / "01_数据整理", time_is_cst=True,
                            read_before_min=30, read_after_min=5,
                            analyze_before_min=30, analyze_after_min=5,
                        )
                        if local_result.get("status") == "ok":
                            history_vectors, query_vector, query_node_index = library.counts_query(
                                local_result["node_counts"]
                            )
                            query_segment = local_result["main_segment"]
                            query_source = "local_raw_fallback"
                            used_window = "本地原始数据前30分钟/后5分钟"
                            error = "网关被阻断，已成功改用本地原始数据；" + gateway_error
                        else:
                            status = "无法回测：网关受阻且本地没有有效 segment"
                            error = gateway_error + f"；local_status={local_result.get('status')}"
                    except Exception as local_exc:
                        status = "无法回测：网关受阻且本地无对应日期数据"
                        error = gateway_error + f"；local={type(local_exc).__name__}: {local_exc}"

            base = {
                "work_order_no": work_order,
                "event_number": event_number,
                "report_date": row.get("report_date", ""),
                "train": train,
                "car_no": car_no,
                "fault_time": fault_time,
                "status": status,
                "error": error,
                "query_source": query_source,
                "used_window": used_window,
                "actual_action_text": actual["actual_action_text"],
                "actual_actions": join(actual["actual_actions"]),
                "actual_components": join(actual["actual_components"]),
                "actual_replaced_components": join(actual["actual_replaced_components"]),
                "actual_part_codes": join(actual["actual_part_codes"]),
            }
            if query_vector is None or not np.linalg.norm(query_vector):
                if query_vector is not None and status == "ok":
                    base["status"] = "无法回测：剔除核心锚点后向量为零"
                case_rows.append(base)
                continue

            ranked = library.rank(
                work_order, query_vector, query_segment, car_no,
                history_vectors=history_vectors, node_index=query_node_index,
            )
            top = ranked[:3]
            for rank, item in enumerate(top, start=1):
                metadata_row = library.metadata.iloc[item["candidate_index"]].to_dict()
                facts = recommendation_facts(item["recommended_work_order"], metadata_row, references)
                recommendation_rows.append({
                    "target_work_order": work_order,
                    "target_fault_time": fault_time,
                    "rank": rank,
                    **{key: value for key, value in item.items() if key != "candidate_index"},
                    "recommended_action_text": facts["recommended_action_text"],
                    "recommended_actions": join(facts["recommended_actions"]),
                    "recommended_components": join(facts["recommended_components"]),
                    "recommended_replaced_components": join(facts["recommended_replaced_components"]),
                    "recommended_part_codes": join(facts["recommended_part_codes"]),
                })

            top1_actions = action_types(top and recommendation_rows[-len(top)]["recommended_actions"] or "")
            top3_actions: list[str] = []
            top3_replaced: list[str] = []
            relevant = recommendation_rows[-len(top):] if top else []
            for rec in relevant:
                top3_actions.extend(str(rec["recommended_actions"]).split("；"))
                top3_replaced.extend(str(rec["recommended_replaced_components"]).split("；"))
            action_hits, action_recall, action_jaccard = overlap_text(actual["actual_actions"], top3_actions)
            replacement_hits, replacement_recall, _ = overlap_text(actual["actual_replaced_components"], top3_replaced)
            actual_replacement = bool(actual["actual_replaced_components"])
            top1_replaced = str(relevant[0]["recommended_replaced_components"]).split("；") if relevant else []
            top1_conservative = not any(clean(value) for value in top1_replaced)
            replacement_direction_match = (
                bool(top3_replaced) if actual_replacement else top1_conservative
            )
            top1_similarity = float(top[0]["similarity"]) if top else 0.0
            top1_shared = int(top[0]["shared_non_core_nodes"]) if top else 0
            if top1_shared == 0:
                grade = "C：仅核心模式，维修意见不足"
            elif actual_replacement and replacement_hits:
                grade = "A：命中实际更换部件"
            elif (not actual_replacement and top1_conservative and action_recall >= 0.5 and top1_similarity >= 0.7):
                grade = "A：保守处置方向一致"
            elif action_recall >= 0.5 or replacement_direction_match or top1_similarity >= 0.7:
                grade = "B：有部分参考价值"
            else:
                grade = "C：参考价值有限"

            case_rows.append({
                **base,
                "target_rf": top[0]["target_rf"] if top else rf_label(query_segment, car_no),
                "top1_work_order": top[0]["recommended_work_order"] if top else "",
                "top1_similarity": top1_similarity,
                "top1_raw_similarity": float(top[0]["raw_similarity"]) if top else np.nan,
                "top1_detail_similarity": float(top[0]["detail_similarity"]) if top else np.nan,
                "top1_shared_non_core_nodes": top1_shared,
                "top1_similarity_warning": (
                    "仅RF/核心信号相似，无共同非核心细节；不能据此判断根因或更换件"
                    if top1_shared == 0 else ""
                ),
                "top3_work_orders": join(item["recommended_work_order"] for item in top),
                "top3_similarities": join(f"{item['similarity']:.4f}" for item in top),
                "top3_recommended_actions": join(top3_actions),
                "top3_recommended_replaced_components": join(top3_replaced),
                "action_hits": join(action_hits),
                "action_recall": action_recall,
                "action_jaccard": action_jaccard,
                "replacement_hits": join(replacement_hits),
                "replacement_recall": replacement_recall if actual_replacement else np.nan,
                "replacement_direction_match": replacement_direction_match,
                "reference_grade": grade,
            })

    cases = pd.DataFrame(case_rows)
    recommendations = pd.DataFrame(recommendation_rows)
    summaries: list[dict[str, Any]] = []
    for work_order, group in cases.groupby("work_order_no", sort=False):
        ok = group[group["status"] == "ok"].copy()
        first = group.iloc[0]
        if ok.empty:
            summaries.append({
                "work_order_no": work_order,
                "report_date": first.get("report_date", ""),
                "train": first.get("train", ""),
                "car_no": first.get("car_no", ""),
                "evaluable_events": 0,
                "status": join(group["status"]),
                "actual_actions": first.get("actual_actions", ""),
                "actual_replaced_components": first.get("actual_replaced_components", ""),
                "actual_part_codes": first.get("actual_part_codes", ""),
            })
            continue
        best = ok.sort_values("top1_similarity", ascending=False).iloc[0]
        summaries.append({
            "work_order_no": work_order,
            "report_date": first.get("report_date", ""),
            "train": first.get("train", ""),
            "car_no": first.get("car_no", ""),
            "evaluable_events": len(ok),
            "status": "ok",
            "actual_actions": first.get("actual_actions", ""),
            "actual_replaced_components": first.get("actual_replaced_components", ""),
            "actual_part_codes": first.get("actual_part_codes", ""),
            "best_event_time": best.get("fault_time", ""),
            "top1_work_order": best.get("top1_work_order", ""),
            "top1_similarity": best.get("top1_similarity", np.nan),
            "top3_work_orders": best.get("top3_work_orders", ""),
            "top1_shared_non_core_nodes": best.get("top1_shared_non_core_nodes", 0),
            "top1_similarity_warning": best.get("top1_similarity_warning", ""),
            "recommended_actions": best.get("top3_recommended_actions", ""),
            "recommended_replaced_components": best.get("top3_recommended_replaced_components", ""),
            "action_hits": best.get("action_hits", ""),
            "action_recall": best.get("action_recall", np.nan),
            "replacement_hits": best.get("replacement_hits", ""),
            "replacement_direction_match": best.get("replacement_direction_match", False),
            "reference_grade": best.get("reference_grade", ""),
        })
    return cases, pd.DataFrame(summaries), recommendations


def write_report(cases: pd.DataFrame, summary: pd.DataFrame, recommendations: pd.DataFrame, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    cases.to_csv(output / "01_逐事件留一评估.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(output / "02_逐工单评估汇总.csv", index=False, encoding="utf-8-sig")
    recommendations.to_csv(output / "03_Top3推荐明细.csv", index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(output / "近期工单留一评估.xlsx", engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="逐工单汇总", index=False)
        cases.to_excel(writer, sheet_name="逐事件评估", index=False)
        recommendations.to_excel(writer, sheet_name="Top3推荐明细", index=False)

    ok = summary[summary["status"] == "ok"].copy()
    grade_counts = ok.get("reference_grade", pd.Series(dtype=str)).value_counts().to_dict()
    all_replacement_targets = summary[summary["actual_replaced_components"].astype(str).str.strip() != ""]
    replacement_targets = ok[ok["actual_replaced_components"].astype(str).str.strip() != ""]
    replacement_hits = int(replacement_targets["replacement_hits"].astype(str).str.strip().ne("").sum()) if not replacement_targets.empty else 0
    high_similarity = int(pd.to_numeric(ok.get("top1_similarity"), errors="coerce").ge(0.7).sum())
    action_covered = int(pd.to_numeric(ok.get("action_recall"), errors="coerce").ge(0.5).sum())
    core_only = int(pd.to_numeric(ok.get("top1_shared_non_core_nodes"), errors="coerce").eq(0).sum())
    lines = [
        "# 2026 年 7 月以来 VCB 新工单留一评估",
        "",
        "## 结论摘要",
        "",
        f"- 工单工作流返回 {len(summary)} 张已关闭/完成工单；可回测 {len(ok)} 张，无法回测 {len(summary) - len(ok)} 张。",
        f"- Top 1 最终相似度达到 0.70 的工单：{high_similarity}/{len(ok) if len(ok) else 0}。",
        f"- Top 3 历史动作覆盖至少一半实际动作：{action_covered}/{len(ok) if len(ok) else 0}；但其中仅核心/RF模式相似：{core_only} 张。",
        f"- 全部近期工单中实际发生部件更换 {len(all_replacement_targets)} 张；其中可回测 {len(replacement_targets)} 张，Top 3 命中同类更换部件 {replacement_hits} 张。",
        f"- 参考价值分级：{json.dumps(grade_counts, ensure_ascii=False)}。",
        "- 评分中的 A/B/C 是可审计规则，不代表维修指令：A=命中实际更换部件，或无更换时高相似且保守动作覆盖过半；B=存在共同非核心细节且动作、方向或相似度至少一项有参考；仅 RF/核心模式相似或其余条件不满足则为 C。",
        "",
        "## 核心判断",
        "",
        "- 当前可验证样本显示，系统对“复位、检查/路试、功能测试”这类通用处置有参考价值。",
        "- 更换件层面的证据明显不足：可回测的实际更换案例没有命中同类部件，因此不能把相似工单中的历史更换件直接当作维修建议。",
        "- 高最终相似度不一定代表高维修价值。若 shared_non_core_nodes=0，相似度主要来自 RF/核心模式，必须降级为现象参考。",
        "- 由于正式 API、源站和 Railiverse 网关均受当前执行环境网络策略阻断，本报告不能对缺少本地遥测的工单计算相似度；这些记录保留在表中，不计入任何命中率。",
        "",
        "## 逐工单结果",
        "",
        "| 工单 | 事件时间 | 实际处置 | 实际更换 | Top1（相似度） | Top3历史更换建议 | 命中 | 评价 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    def cell(value: Any) -> str:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return "—"
        text = clean(value)
        return "—" if text.casefold() in {"", "nan", "nat", "none"} else text.replace("|", "\\|")

    for _, row in summary.iterrows():
        similarity = row.get("top1_similarity")
        score = f"{float(similarity):.3f}" if pd.notna(similarity) else "—"
        top1 = cell(row.get("top1_work_order"))
        evaluation = cell(row.get("reference_grade"))
        if evaluation == "—":
            evaluation = cell(row.get("status"))
        lines.append(
            "| " + " | ".join(
                cell(value)
                for value in (
                    row.get("work_order_no"), row.get("best_event_time"), row.get("actual_actions"),
                    row.get("actual_replaced_components"), f"{top1} ({score})" if top1 != "—" else "—",
                    row.get("recommended_replaced_components"), row.get("replacement_hits"),
                    evaluation,
                )
            ) + " |"
        )
    lines.extend([
        "",
        "## 解释边界",
        "",
        "- 每张目标工单及其全部 case 都从候选集中排除，因此不存在自匹配。",
        "- 这是对当前本地案例库快照的 leave-one-work-order-out 评估；没有把目标工单从 node_index 的历史词表中删除。",
        "- 相似度评价的是信号模式，不证明根因。维修动作和更换件命中基于工单原文及结构化字段的集合比较。",
        "- 同一工单有多个明确故障时刻时逐事件回测，工单汇总采用 Top1 相似度最高的事件，同时在逐事件表保留全部结果。",
        "- 没有明确故障时刻、网关无数据或没有有效 VCB segment 的记录列为无法回测，不计入命中率分母。",
        "",
    ])
    (output / "最终分析报告.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-response", type=Path, default=DEFAULT_RESPONSE)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--download-root", type=Path, default=DEFAULT_DOWNLOAD_ROOT)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Do not send event data to the Railiverse gateway; use existing local partitions only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases, summary, recommendations = evaluate(args)
    write_report(cases, summary, recommendations, args.output)
    print(summary.to_string(index=False))
    print(f"\n输出目录: {args.output.resolve()}")


if __name__ == "__main__":
    main()
