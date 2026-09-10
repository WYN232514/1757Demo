#!/usr/bin/env python3
"""Build an evidence-backed VCB Trip knowledge graph from the case library.

The graph follows this path:

    VCB Trip -> normalized signal manifestation -> involved equipment
             -> maintenance method -> source work order

Signal manifestations come from the exact normalized nodes used by the
similarity model.  Equipment and maintenance facts come from the existing
work-order workbook and its structured extraction.  Every inferred
association keeps its work-order evidence so co-occurrence is not presented as
proven causation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import joblib
import pandas as pd

from vcb_case_library_direct import (
    _load_qdrant_display_name_map,
    segment_to_node_counts,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_LIBRARY_DIR = ROOT / "case_library"
DEFAULT_REFERENCE_JSON = (
    ROOT
    / "fault_extractor"
    / "fault_extractor"
    / "output"
    / "reference_data_with_cause_inferenced.json"
)
DEFAULT_OUTPUT_DIR = ROOT / "knowledge_graph"

PHENOMENON_ID = "phenomenon:vcb_trip"

TARGET_DIM_CODES = {
    "target_ramping_finish_911": "911_5_1_02_sd",
    "target_ramping_finish_943": "943_5_1_02_sd",
    "target_vcb_904": "904_2_1_02_sd",
    "target_vcb_936": "936_2_1_02_sd",
}

EVENT_LABELS = {
    "reset": "复位",
    "checked": "检查",
    "replaced": "更换",
    "function tested": "功能测试",
    "tested": "测试",
    "swapped": "互换测试",
    "isolated": "隔离",
    "repaired": "修复",
    "monitored": "持续监测",
}

EVENT_KEYWORDS = {
    "replaced": ("replaced", "replace ", "replacement"),
    "repaired": ("repaired", "repair "),
    "swapped": ("swapped", "swap "),
    "isolated": ("isolated", "isolate "),
    "function tested": ("function test", "tested", "test "),
    "checked": ("checked", "check ", "inspection"),
    "reset": ("reset",),
}

MAINTENANCE_GROUPS = {
    "inspection_restore": {
        "label": "复位、检查与功能确认",
        "events": {"reset", "checked", "function tested", "tested", "monitored"},
        "basis": "复位、状态检查和功能测试在历史工单中通常作为一套恢复确认流程连续出现",
    },
    "replacement": {
        "label": "部件更换",
        "events": {"replaced"},
        "basis": "将不同部件的更换动作合并为维修策略，具体更换件保留在节点明细中",
    },
    "swap_test": {
        "label": "互换测试与对比排查",
        "events": {"swapped"},
        "basis": "通过互换部件或板卡进行故障定位",
    },
    "isolation": {
        "label": "设备隔离处置",
        "events": {"isolated"},
        "basis": "将隔离相关动作归为同一安全处置策略",
    },
    "repair": {
        "label": "线缆、端子与部件修复",
        "events": {"repaired"},
        "basis": "将工单中的实体修复动作归并，具体对象保留在节点明细中",
    },
}

# Supplements the structured parser when a device is explicitly present in the
# raw work-order action but missing from treatment_components.
EQUIPMENT_LEXICON = (
    "APS Converter unit",
    "C/I converter & inverter power unit",
    "C/I inverter power unit",
    "C/I converter",
    "C/I inverter",
    "C/I GCU",
    "APC controller",
    "APC receiver",
    "ATR controller",
    "ATR receiver",
    "main transformer cable lug",
    "main transformer terminal wire",
    "main transformer cable",
    "main transformer",
    "APC Available signal circuit",
    "VCB ramping-down finish signal circuit",
    "VCB close-control relay/power-supply chain",
    "VCB trip detection/control circuit",
    "VCB trip/control circuit",
    "APC/ATR/VCB control chain",
    "HTJB jumper",
    "OVT unit",
    "pantograph",
    "APCCR",
    "RAMFR",
    "VCBCR",
    "VCBHR",
    "VCBSTR",
    "VCBCTR",
    "VDOS",
    "PADR",
    "PDR",
    "EPDR",
    "ACOR",
    "MTTHR",
    "PUR",
    "APS",
    "APC",
    "ATR",
    "VCB",
    "C/I",
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = html.unescape(str(value)).replace("<!-- RICH TEXT -->", "")
    return re.sub(r"\s+", " ", text).strip()


def unique_text(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value)
        key = text.casefold()
        if not text or key in {"n/a", "na", "none", "null"} or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:14]
    return f"{prefix}:{digest}"


def find_work_order_workbook(root: Path) -> Tuple[Path, pd.DataFrame]:
    """Find the workbook whose header contains Work Order NO."""
    candidates: List[Tuple[int, Path, pd.DataFrame]] = []
    for path in sorted(root.glob("*.xls*")):
        if path.name.startswith("~$"):
            continue
        try:
            excel = pd.ExcelFile(path)
        except Exception:
            continue
        for sheet in excel.sheet_names:
            try:
                raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
            except Exception:
                continue
            header_index = None
            for row_index in range(min(12, len(raw))):
                values = {clean_text(value) for value in raw.iloc[row_index].tolist()}
                if "Work Order NO." in values:
                    header_index = row_index
                    break
            if header_index is None:
                continue
            headers = [clean_text(value) or f"column_{i}" for i, value in enumerate(raw.iloc[header_index])]
            frame = raw.iloc[header_index + 1 :].copy()
            frame.columns = headers
            frame = frame.loc[:, ~frame.columns.duplicated()].fillna("")
            frame["Work Order NO."] = frame["Work Order NO."].map(clean_text).str.removesuffix(".0")
            frame = frame[frame["Work Order NO."] != ""].copy()
            candidates.append((len(frame), path, frame))
    if not candidates:
        raise FileNotFoundError("没有找到包含 Work Order NO. 列的工单 xls/xlsx 文件")
    _count, path, frame = max(candidates, key=lambda item: (item[0], item[1].suffix.lower() == ".xlsx"))
    return path, frame


def load_structured_reference(path: Path) -> Dict[str, Dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"结构化工单不是对象格式：{path}")
    return {
        clean_text(key).removesuffix(".0"): dict(value)
        for key, value in raw.items()
        if isinstance(value, Mapping)
    }


def normalize_equipment(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"^(?:P|M|R)\d{3}\s+", "", text, flags=re.I)
    text = text.strip(" ,.;:/")
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


def split_component_field(value: Any) -> List[str]:
    # Do not split on '/': names such as C/I and APC/ATR/VCB are meaningful
    # equipment or control-chain labels in the work orders.
    return unique_text(normalize_equipment(item) for item in re.split(r"[,;]", clean_text(value)))


def equipment_group(equipment_name: str) -> Tuple[str, str, str]:
    """Keep every concrete equipment name as an independent graph node."""
    name = normalize_equipment(equipment_name)
    return (
        "single_" + hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:12],
        name,
        "按工单原文中的具体部件独立建点，不按共同出现关系合并",
    )


def maintenance_group(event: str, equipment_group_key: str, equipment_group_label: str) -> Tuple[str, str, str]:
    normalized = clean_text(event).casefold()
    if normalized in EVENT_LABELS:
        action_label = "功能测试" if normalized == "tested" else EVENT_LABELS[normalized]
        action_key = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
        return (
            f"{action_key}:{equipment_group_key}",
            f"{action_label} {equipment_group_label}",
            "按工单中的具体部件—动作配对独立建点",
        )
    return (
        f"other_maintenance:{equipment_group_key}",
        f"其他维修处置 {equipment_group_label}",
        "未归入标准策略的维修动作，原始动作保留在节点明细中",
    )


def infer_event_for_mention(action: str, mention_start: int) -> str:
    before = action.casefold()[max(0, mention_start - 100) : mention_start]
    matches: List[Tuple[int, str]] = []
    for event, keywords in EVENT_KEYWORDS.items():
        for keyword in keywords:
            position = before.rfind(keyword)
            if position >= 0:
                matches.append((position, event))
    return max(matches)[1] if matches else ""


def extract_equipment_and_actions(structured: Mapping[str, Any]) -> Tuple[List[str], List[Tuple[str, str]]]:
    equipment: List[str] = []
    action_pairs: List[Tuple[str, str]] = []

    cause_components = structured.get("cause_components") or []
    treatment_components = structured.get("treatment_components") or []
    treatment_events = structured.get("treatment_events") or []
    if isinstance(cause_components, str):
        cause_components = [cause_components]
    if isinstance(treatment_components, str):
        treatment_components = [treatment_components]
    if isinstance(treatment_events, str):
        treatment_events = [treatment_events]

    equipment.extend(normalize_equipment(item) for item in cause_components)
    for component, event in zip(treatment_components, treatment_events):
        component_text = normalize_equipment(component)
        event_text = clean_text(event).casefold()
        if component_text and event_text:
            equipment.append(component_text)
            action_pairs.append((component_text, event_text))

    equipment.extend(split_component_field(structured.get("component")))

    action = clean_text(structured.get("action"))
    action_lower = action.casefold()
    # Prefer longest phrases so "ATR controller" is not reduced to "ATR".
    occupied: List[Tuple[int, int]] = []
    for phrase in sorted(EQUIPMENT_LEXICON, key=len, reverse=True):
        phrase_lower = phrase.casefold()
        for match in re.finditer(re.escape(phrase_lower), action_lower):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            occupied.append(span)
            component_text = normalize_equipment(action[span[0] : span[1]])
            equipment.append(component_text)
            event = infer_event_for_mention(action, span[0])
            if event:
                action_pairs.append((component_text, event))

    equipment = unique_text(equipment)
    seen_pairs: set[Tuple[str, str]] = set()
    normalized_pairs: List[Tuple[str, str]] = []
    for component, event in action_pairs:
        component = normalize_equipment(component)
        event = clean_text(event).casefold()
        pair = (component.casefold(), event)
        if not component or not event or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        normalized_pairs.append((component, event))
    return equipment, normalized_pairs


def parse_signal_node(
    node_key: str,
    dimension_prefixes: Sequence[str],
    display_names: Mapping[str, str],
) -> Dict[str, str]:
    dimension = ""
    for candidate in dimension_prefixes:
        prefix = candidate + "_"
        if node_key.startswith(prefix):
            dimension = candidate
            break
    if dimension:
        remainder = node_key[len(dimension) + 1 :]
        previous, current = remainder.rsplit("_", 1) if "_" in remainder else (remainder, "")
    else:
        previous, current = "", ""
        dimension = node_key

    code = TARGET_DIM_CODES.get(dimension, dimension.removeprefix("dir_"))
    description = clean_text(display_names.get(code)) or code
    if previous == "ramping_finish_status":
        label = f"{description}：完成状态={current}"
    else:
        label = f"{description}：{previous} → {current}"
    return {
        "dimension": dimension,
        "code": code,
        "description": description,
        "previous_value": previous,
        "current_value": current,
        "label": label,
    }


def exclude_signal(parsed: Mapping[str, str]) -> bool:
    """Return True for signals intentionally omitted from the knowledge graph."""
    dimension = clean_text(parsed.get("dimension")).casefold()
    return dimension.startswith("dir_distance")


def canonical_signal_key(parsed: Mapping[str, str]) -> str:
    """Merge the same business signal across carriage/unit-specific codes."""
    description = re.sub(r"\s+", " ", clean_text(parsed.get("description"))).casefold()
    previous = clean_text(parsed.get("previous_value"))
    current = clean_text(parsed.get("current_value"))
    return f"{description}|{previous}|{current}"


class GraphBuilder:
    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def add_node(self, node_id: str, node_type: str, label: str, **properties: Any) -> None:
        node = self.nodes.setdefault(
            node_id,
            {"id": node_id, "type": node_type, "label": label, "work_orders": [], "case_ids": []},
        )
        node.update({key: value for key, value in properties.items() if value not in (None, "")})

    def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        label: str,
        work_order: str = "",
        case_ids: Iterable[str] = (),
        **properties: Any,
    ) -> None:
        key = (source, target, edge_type)
        edge = self.edges.setdefault(
            key,
            {
                "id": stable_id("edge", "|".join(key)),
                "source": source,
                "target": target,
                "type": edge_type,
                "label": label,
                "work_orders": [],
                "case_ids": [],
                "weight": 0,
            },
        )
        if work_order and work_order not in edge["work_orders"]:
            edge["work_orders"].append(work_order)
        for case_id in case_ids:
            case_id = clean_text(case_id)
            if case_id and case_id not in edge["case_ids"]:
                edge["case_ids"].append(case_id)
        edge["weight"] += 1
        edge.update({key_: value for key_, value in properties.items() if value not in (None, "")})

    def attach_evidence(self, node_id: str, work_order: str, case_ids: Iterable[str] = ()) -> None:
        node = self.nodes[node_id]
        if work_order and work_order not in node["work_orders"]:
            node["work_orders"].append(work_order)
        for case_id in case_ids:
            case_id = clean_text(case_id)
            if case_id and case_id not in node["case_ids"]:
                node["case_ids"].append(case_id)

    def finalize(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        for node in self.nodes.values():
            node["work_orders"] = sorted(node.get("work_orders", []))
            node["case_ids"] = sorted(node.get("case_ids", []))
            node["work_order_count"] = len(node["work_orders"])
            node["case_count"] = len(node["case_ids"])
        for edge in self.edges.values():
            edge["work_orders"] = sorted(edge.get("work_orders", []))
            edge["case_ids"] = sorted(edge.get("case_ids", []))
            edge["evidence_count"] = len(edge["work_orders"])
        nodes = sorted(self.nodes.values(), key=lambda item: (item["type"], item["label"], item["id"]))
        edges = sorted(self.edges.values(), key=lambda item: (item["type"], item["source"], item["target"]))
        return nodes, edges


def build_graph(library_dir: Path, reference_json: Path, project_root: Path) -> Dict[str, Any]:
    metadata = pd.read_csv(library_dir / "metadata.csv", dtype=str).fillna("")
    segments: Dict[str, Sequence[Tuple[Any, ...]]] = joblib.load(library_dir / "segments.joblib")
    node_index: Dict[str, int] = joblib.load(library_dir / "node_index.joblib")
    reference = load_structured_reference(reference_json)
    workbook_path, workbook = find_work_order_workbook(project_root)
    workbook_records = {
        clean_text(row.get("Work Order NO.")).removesuffix(".0"): {
            clean_text(column): clean_text(value)
            for column, value in row.items()
            if clean_text(column) and clean_text(value)
        }
        for _, row in workbook.iterrows()
    }

    metadata["work_order_no"] = metadata["work_order_no"].map(clean_text).str.removesuffix(".0")
    metadata["case_id"] = metadata["case_id"].map(clean_text)
    library_work_orders = sorted(metadata["work_order_no"].dropna().unique().tolist())

    case_rows = {clean_text(row["case_id"]): row for _, row in metadata.iterrows()}
    work_order_cases: Dict[str, List[str]] = defaultdict(list)
    work_order_signal_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for case_id, row in case_rows.items():
        work_order = clean_text(row.get("work_order_no"))
        if not work_order or case_id not in segments:
            continue
        work_order_cases[work_order].append(case_id)
        counts = segment_to_node_counts(segments[case_id], car_no=row.get("car_no", ""))
        for node_key, count in counts.items():
            work_order_signal_counts[work_order][str(node_key)] += int(count)

    display_names = _load_qdrant_display_name_map()
    dimension_prefixes = sorted(
        set(TARGET_DIM_CODES)
        | {"dir_" + str(code) for code in display_names}
        | {
            node.rsplit("_", 2)[0]
            for node in node_index
            if str(node).startswith("dir_") and str(node).count("_") >= 3
        },
        key=len,
        reverse=True,
    )

    graph = GraphBuilder()
    graph.add_node(
        PHENOMENON_ID,
        "phenomenon",
        "VCB Trip",
        description="列车真空断路器跳闸/断开现象（故障代码 1757）",
        event_code="1757",
    )

    work_order_summaries: List[Dict[str, Any]] = []
    missing_structured: List[str] = []
    missing_workbook: List[str] = []
    original_equipment_names: set[str] = set()
    original_maintenance_methods: set[str] = set()

    for work_order in library_work_orders:
        case_ids = sorted(work_order_cases.get(work_order, []))
        structured = reference.get(work_order, {})
        raw_work_order = workbook_records.get(work_order, {})
        if not structured:
            missing_structured.append(work_order)
        if not raw_work_order:
            missing_workbook.append(work_order)

        metadata_rows = metadata[metadata["work_order_no"] == work_order]
        train = clean_text(metadata_rows.iloc[0].get("train")) if not metadata_rows.empty else ""
        car_no = clean_text(metadata_rows.iloc[0].get("car_no")) if not metadata_rows.empty else ""
        fault_times = unique_text(metadata_rows["fault_time_raw"].tolist()) if "fault_time_raw" in metadata_rows else []
        raw_action = clean_text(structured.get("action")) or clean_text(raw_work_order.get("Long Description"))
        equipment, action_pairs = extract_equipment_and_actions(structured)

        work_order_node = f"work_order:{work_order}"
        graph.add_node(
            work_order_node,
            "work_order",
            f"工单 {work_order}",
            work_order_no=work_order,
            train=train,
            car_no=car_no,
            fault_times=fault_times,
            fault=clean_text(structured.get("fault")) or clean_text(raw_work_order.get("Description")),
            cause=clean_text(structured.get("cause")),
            maintenance_action=raw_action,
            result=clean_text(structured.get("result")),
            replace_in_part=unique_text(structured.get("replace_in_part") or []),
            replace_out_part=unique_text(structured.get("replace_out_part") or []),
            raw_work_order=raw_work_order,
            workbook_record_available=bool(raw_work_order),
            evidence_source=(
                "工单Excel原文 + 结构化工单解析"
                if raw_work_order
                else "结构化工单解析（当前工单Excel未包含该工单）"
            ),
            structured_source=str(reference_json.relative_to(project_root)),
            workbook_source=workbook_path.name,
        )
        graph.attach_evidence(work_order_node, work_order, case_ids)

        canonical_signals: Dict[str, Dict[str, Any]] = {}
        for node_key, change_count in sorted(work_order_signal_counts.get(work_order, {}).items()):
            parsed = parse_signal_node(node_key, dimension_prefixes, display_names)
            if exclude_signal(parsed):
                continue
            canonical_key = canonical_signal_key(parsed)
            record = canonical_signals.setdefault(
                canonical_key,
                {
                    "parsed": parsed,
                    "change_count": 0,
                    "source_normalized_nodes": [],
                    "source_dimensions": [],
                    "source_codes": [],
                },
            )
            record["change_count"] += int(change_count)
            record["source_normalized_nodes"] = unique_text(record["source_normalized_nodes"] + [node_key])
            record["source_dimensions"] = unique_text(record["source_dimensions"] + [parsed["dimension"]])
            record["source_codes"] = unique_text(record["source_codes"] + [parsed["code"]])

        signal_nodes: List[str] = []
        for canonical_key, record in sorted(canonical_signals.items()):
            parsed = record["parsed"]
            signal_id = stable_id("signal", canonical_key)
            graph.add_node(
                signal_id,
                "signal",
                parsed["label"],
                normalized_signal_key=canonical_key,
                normalized_dimension=parsed["description"],
                signal_description=parsed["description"],
                previous_value=parsed["previous_value"],
                current_value=parsed["current_value"],
                carriage_unit_codes_merged=True,
            )
            signal_node = graph.nodes[signal_id]
            signal_node["source_normalized_nodes"] = unique_text(
                signal_node.get("source_normalized_nodes", []) + record["source_normalized_nodes"]
            )
            signal_node["source_dimensions"] = unique_text(
                signal_node.get("source_dimensions", []) + record["source_dimensions"]
            )
            signal_node["source_codes"] = unique_text(
                signal_node.get("source_codes", []) + record["source_codes"]
            )
            graph.nodes[signal_id]["total_change_count"] = int(
                graph.nodes[signal_id].get("total_change_count", 0)
            ) + int(record["change_count"])
            graph.attach_evidence(signal_id, work_order, case_ids)
            graph.add_edge(
                PHENOMENON_ID,
                signal_id,
                "phenomenon_to_signal",
                "表现为",
                work_order=work_order,
                case_ids=case_ids,
                semantics="历史案例中观测到的归一化信号变化",
            )
            signal_nodes.append(signal_id)

        equipment_nodes: Dict[str, str] = {}
        equipment_group_records: Dict[str, Dict[str, Any]] = {}
        cause_components = structured.get("cause_components") or []
        if isinstance(cause_components, str):
            cause_components = [cause_components]
        cause_keys = {normalize_equipment(item).casefold() for item in cause_components}
        treatment_components = structured.get("treatment_components") or []
        if isinstance(treatment_components, str):
            treatment_components = [treatment_components]
        treatment_keys = {normalize_equipment(item).casefold() for item in treatment_components}

        for equipment_name in equipment:
            original_equipment_names.add(equipment_name)
            group_key, group_label, group_basis = equipment_group(equipment_name)
            equipment_id = f"equipment_group:{group_key}"
            roles: List[str] = []
            if equipment_name.casefold() in cause_keys:
                roles.append("原因相关设备/回路")
            if equipment_name.casefold() in treatment_keys:
                roles.append("检修涉及设备")
            if not roles:
                roles.append("工单提及设备")
            group_record = equipment_group_records.setdefault(
                group_key,
                {
                    "key": group_key,
                    "id": equipment_id,
                    "label": group_label,
                    "basis": group_basis,
                    "members": [],
                    "roles": [],
                },
            )
            group_record["members"] = unique_text(group_record["members"] + [equipment_name])
            group_record["roles"] = unique_text(group_record["roles"] + roles)
            equipment_nodes[equipment_name.casefold()] = equipment_id

        for group_record in equipment_group_records.values():
            equipment_id = group_record["id"]
            graph.add_node(
                equipment_id,
                "equipment",
                group_record["label"],
                equipment_group_key=group_record["key"],
                equipment_group=group_record["label"],
                group_basis=group_record["basis"],
            )
            equipment_node = graph.nodes[equipment_id]
            equipment_node["member_equipment"] = unique_text(
                equipment_node.get("member_equipment", []) + group_record["members"]
            )
            equipment_node["roles"] = unique_text(
                equipment_node.get("roles", []) + group_record["roles"]
            )
            member_work_orders = equipment_node.setdefault("member_work_orders", {})
            for member in group_record["members"]:
                member_work_orders[member] = unique_text(
                    member_work_orders.get(member, []) + [work_order]
                )
            graph.attach_evidence(equipment_id, work_order, case_ids)
            for signal_id in signal_nodes:
                graph.add_edge(
                    signal_id,
                    equipment_id,
                    "signal_to_equipment",
                    "同工单关联",
                    work_order=work_order,
                    case_ids=case_ids,
                    semantics="信号与设备出现在同一张历史工单；表示证据关联，不单独证明因果",
                )

        for equipment_name, event in action_pairs:
            event_label = EVENT_LABELS.get(event, event)
            detailed_method = f"{event_label} {equipment_name}"
            original_maintenance_methods.add(detailed_method)
            equipment_id = equipment_nodes.get(equipment_name.casefold())
            if not equipment_id:
                original_equipment_names.add(equipment_name)
                group_key, group_label, group_basis = equipment_group(equipment_name)
                equipment_id = f"equipment_group:{group_key}"
                graph.add_node(
                    equipment_id,
                    "equipment",
                    group_label,
                    equipment_group_key=group_key,
                    equipment_group=group_label,
                    group_basis=group_basis,
                )
                equipment_node = graph.nodes[equipment_id]
                equipment_node["member_equipment"] = unique_text(
                    equipment_node.get("member_equipment", []) + [equipment_name]
                )
                equipment_node["roles"] = unique_text(
                    equipment_node.get("roles", []) + ["检修涉及设备"]
                )
                member_work_orders = equipment_node.setdefault("member_work_orders", {})
                member_work_orders[equipment_name] = unique_text(
                    member_work_orders.get(equipment_name, []) + [work_order]
                )
                graph.attach_evidence(equipment_id, work_order, case_ids)
                equipment_nodes[equipment_name.casefold()] = equipment_id
                for signal_id in signal_nodes:
                    graph.add_edge(
                        signal_id,
                        equipment_id,
                        "signal_to_equipment",
                        "同工单关联",
                        work_order=work_order,
                        case_ids=case_ids,
                        semantics="信号与设备出现在同一张历史工单；表示证据关联，不单独证明因果",
                    )
            equipment_node = graph.nodes[equipment_id]
            method_group_key, method_label, method_basis = maintenance_group(
                event,
                str(equipment_node["equipment_group_key"]),
                str(equipment_node["label"]),
            )
            method_id = f"maintenance_group:{method_group_key}"
            graph.add_node(
                method_id,
                "maintenance",
                method_label,
                maintenance_group=method_label,
                group_basis=method_basis,
                equipment_group=equipment_node["label"],
            )
            maintenance_node = graph.nodes[method_id]
            maintenance_node["member_actions"] = unique_text(
                maintenance_node.get("member_actions", []) + [event]
            )
            action_order = [
                "reset",
                "checked",
                "function tested",
                "tested",
                "monitored",
                "replaced",
                "swapped",
                "isolated",
                "repaired",
            ]
            actual_actions = sorted(
                maintenance_node["member_actions"],
                key=lambda action: action_order.index(action) if action in action_order else len(action_order),
            )
            actual_action_labels = unique_text(
                EVENT_LABELS.get(action, action) for action in actual_actions
            )
            maintenance_node["member_methods"] = unique_text(
                maintenance_node.get("member_methods", []) + [detailed_method]
            )
            method_work_orders = maintenance_node.setdefault("method_work_orders", {})
            method_work_orders[detailed_method] = unique_text(
                method_work_orders.get(detailed_method, []) + [work_order]
            )
            maintenance_node["affected_equipment_groups"] = unique_text(
                maintenance_node.get("affected_equipment_groups", [])
                + [graph.nodes[equipment_id]["label"]]
            )
            maintenance_node["affected_equipment"] = unique_text(
                maintenance_node.get("affected_equipment", []) + [equipment_name]
            )
            maintenance_node["label"] = (
                f"{' / '.join(actual_action_labels)} "
                f"{' / '.join(maintenance_node['affected_equipment'])}"
            )
            maintenance_node["maintenance_group"] = maintenance_node["label"]
            raw_actions = maintenance_node.setdefault("raw_actions", [])
            if raw_action and raw_action not in raw_actions:
                raw_actions.append(raw_action)
            graph.attach_evidence(method_id, work_order, case_ids)
            graph.add_edge(
                equipment_id,
                method_id,
                "equipment_to_maintenance",
                "采取",
                work_order=work_order,
                case_ids=case_ids,
                semantics="工单中记录的设备处置动作",
            )
            graph.add_edge(
                method_id,
                work_order_node,
                "maintenance_to_work_order",
                "来源工单",
                work_order=work_order,
                case_ids=case_ids,
                semantics="维修方法的原始证据工单",
            )

        work_order_summaries.append(
            {
                "work_order_no": work_order,
                "train": train,
                "car_no": car_no,
                "case_count": len(case_ids),
                "signal_count": len(signal_nodes),
                "equipment": equipment,
                "maintenance_methods": [
                    f"{EVENT_LABELS.get(event, event)} {component}"
                    for component, event in action_pairs
                ],
                "fault": clean_text(structured.get("fault")) or clean_text(raw_work_order.get("Description")),
                "cause": clean_text(structured.get("cause")),
                "action": raw_action,
                "result": clean_text(structured.get("result")),
                "workbook_record_available": bool(raw_work_order),
                "evidence_source": (
                    "工单Excel原文 + 结构化工单解析"
                    if raw_work_order
                    else "结构化工单解析（当前工单Excel未包含）"
                ),
            }
        )

    nodes, edges = graph.finalize()
    node_counts_by_type: Dict[str, int] = defaultdict(int)
    for node in nodes:
        node_counts_by_type[node["type"]] += 1

    overview_ids = {PHENOMENON_ID}
    for node_type, limit in (("signal", 14), ("equipment", 14), ("maintenance", 18), ("work_order", 16)):
        ranked = sorted(
            (node for node in nodes if node["type"] == node_type),
            key=lambda node: (
                int(node.get("work_order_count", 0)),
                int(node.get("total_change_count", 0)),
                node["label"],
            ),
            reverse=True,
        )
        overview_ids.update(node["id"] for node in ranked[:limit])

    return {
        "meta": {
            "title": "VCB Trip 故障知识图谱",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "schema_version": 3,
            "phenomenon": "VCB Trip",
            "event_code": "1757",
            "relationship_path": ["故障现象", "归一化信号表现", "涉及设备", "维修方法", "来源工单"],
            "workbook_source": workbook_path.name,
            "structured_reference_source": str(reference_json.relative_to(project_root)),
            "case_library_source": str(library_dir.relative_to(project_root)),
            "case_count": int(len(metadata)),
            "work_order_count": len(library_work_orders),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_counts_by_type": dict(node_counts_by_type),
            "node_compression": {
                "equipment_before": len(original_equipment_names),
                "equipment_after": int(node_counts_by_type.get("equipment", 0)),
                "maintenance_before": len(original_maintenance_methods),
                "maintenance_after": int(node_counts_by_type.get("maintenance", 0)),
                "information_retained_in": [
                    "member_equipment",
                    "member_work_orders",
                    "member_methods",
                    "method_work_orders",
                    "affected_equipment",
                    "work_orders",
                    "raw_actions",
                ],
            },
            "signal_normalization": "同一信号名称和相同前后状态跨车厢/设备单元合并",
            "excluded_signals": ["distance"],
            "missing_structured_work_orders": missing_structured,
            "missing_workbook_work_orders": missing_workbook,
            "important_note": "信号到设备为同工单证据关联，不表示仅凭该信号即可确认设备故障。",
        },
        "overview_node_ids": sorted(overview_ids),
        "nodes": nodes,
        "edges": edges,
        "work_orders": work_order_summaries,
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>VCB Trip 故障知识图谱</title>
  <style>
    :root { --ink:#14213d; --muted:#64748b; --line:#dbe3ef; --paper:#f4f7fb; }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font:14px/1.55 "Microsoft YaHei",Arial,sans-serif; }
    header { padding:28px 34px 22px; background:linear-gradient(135deg,#102a43,#1d4ed8); color:white; }
    h1 { margin:0 0 6px; font-size:27px; }
    header p { margin:0; color:#dbeafe; }
    main { padding:20px 28px 40px; }
    .stats { display:grid; grid-template-columns:repeat(6,minmax(110px,1fr)); gap:10px; margin-bottom:14px; }
    .stat,.card { background:white; border:1px solid var(--line); border-radius:12px; box-shadow:0 5px 18px rgba(15,23,42,.05); }
    .stat { padding:13px 15px; }
    .stat b { display:block; font-size:22px; color:#1d4ed8; }
    .stat span { color:var(--muted); font-size:12px; }
    .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:end; padding:14px; margin-bottom:12px; }
    label { display:flex; flex-direction:column; gap:4px; color:#475569; font-size:12px; }
    select,input { min-width:210px; padding:9px 10px; border:1px solid #cbd5e1; border-radius:8px; background:white; color:#0f172a; }
    button { padding:9px 14px; border:0; border-radius:8px; background:#1d4ed8; color:white; cursor:pointer; }
    button.secondary { background:#475569; }
    .selection-status { min-width:260px; max-width:520px; padding:8px 11px; border:1px solid #c4b5fd; border-radius:8px; background:#f5f3ff; color:#6d28d9; font-size:12px; }
    .notice { margin-left:auto; max-width:560px; padding:9px 12px; border-radius:8px; background:#fff7ed; color:#9a3412; border:1px solid #fed7aa; font-size:12px; }
    .graph-grid { display:grid; grid-template-columns:minmax(0,1fr) 330px; gap:12px; }
    .graph-card { overflow:hidden; }
    .legend { display:flex; flex-wrap:wrap; gap:14px; padding:11px 15px; border-bottom:1px solid var(--line); color:#475569; }
    .legend i { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:5px; }
    #graphViewport { height:700px; overflow:auto; background:linear-gradient(#fff,#fbfdff); }
    #graph { display:block; min-width:1650px; }
    .edge { fill:none; stroke:#94a3b8; stroke-width:1.1; opacity:.18; }
    .edge.highlight { stroke:#f97316; stroke-width:2.4; opacity:.9; }
    .node rect { stroke-width:1.4; filter:drop-shadow(0 2px 2px rgba(15,23,42,.10)); cursor:pointer; }
    .node text { pointer-events:none; font-size:11px; font-weight:600; fill:#0f172a; }
    .node.faded { opacity:.12; }
    .node.selected rect { stroke:#f97316; stroke-width:3; }
    .detail { padding:16px; height:748px; overflow:auto; }
    .detail h3 { margin:0 0 10px; }
    .detail pre { white-space:pre-wrap; word-break:break-word; font:12px/1.55 Consolas,"Microsoft YaHei",monospace; color:#334155; }
    .section { margin-top:16px; padding:16px; }
    .section h2 { margin:0 0 4px; font-size:18px; }
    .section p { margin:0 0 12px; color:var(--muted); }
    .table-wrap { overflow:auto; max-height:520px; border:1px solid var(--line); border-radius:9px; }
    table { width:100%; border-collapse:collapse; background:white; }
    th,td { padding:9px 10px; border-bottom:1px solid #e8eef6; text-align:left; vertical-align:top; }
    th { position:sticky; top:0; background:#eff6ff; color:#1e3a8a; z-index:1; }
    tr:hover td { background:#f8fafc; }
    .small { color:var(--muted); font-size:12px; }
    @media (max-width:1000px) { .stats{grid-template-columns:repeat(2,1fr)} .graph-grid{grid-template-columns:1fr} .detail{height:auto} .notice{margin-left:0} }
  </style>
</head>
<body>
<header>
  <h1>VCB Trip 故障知识图谱</h1>
  <p>故障现象 → 归一化信号表现 → 涉及设备 → 维修方法 → 来源工单</p>
</header>
<main>
  <div id="stats" class="stats"></div>
  <div class="card controls">
    <label>查看范围
      <select id="scope"><option value="overview">总体概览</option><option value="all">全部节点</option></select>
    </label>
    <label>按工单查看
      <select id="workOrder"><option value="">全部工单</option></select>
    </label>
    <label>搜索节点
      <input id="search" placeholder="输入信号、设备、维修方法或工单号">
    </label>
    <button id="reset">恢复概览</button>
    <button id="clearWorkOrderHighlights" class="secondary">清除工单点亮</button>
    <div id="selectedWorkOrders" class="selection-status">已点亮工单：无</div>
    <div class="notice">注意：信号与设备之间表示“出现在同一历史工单中的证据关联”，不能单独解释为已经证明的故障因果。</div>
  </div>
  <div class="graph-grid">
    <div class="card graph-card">
      <div class="legend">
        <span><i style="background:#ef4444"></i>故障现象</span>
        <span><i style="background:#3b82f6"></i>归一化信号</span>
        <span><i style="background:#f59e0b"></i>设备/回路（同类合并，点击看明细）</span>
        <span><i style="background:#10b981"></i>维修方法（同类合并，点击看明细）</span>
        <span><i style="background:#8b5cf6"></i>来源工单</span>
        <span class="small">在“全部节点”模式点击紫色工单，可多选并点亮完整线路；再次点击可取消。</span>
      </div>
      <div id="graphViewport"><svg id="graph"></svg></div>
    </div>
    <aside class="card detail"><h3>节点详情</h3><p class="small">点击图中的节点查看完整属性和证据工单。</p><pre id="detail">尚未选择节点</pre></aside>
  </div>
  <section class="card section">
    <h2>工单证据汇总</h2>
    <p>每一行都保留原工单的设备、维修动作、结果和对应信号数量。</p>
    <div class="table-wrap"><table><thead><tr><th>工单号</th><th>列车/车厢</th><th>案例/信号</th><th>涉及设备</th><th>维修方法</th><th>维修结果</th><th>证据来源</th></tr></thead><tbody id="workOrderRows"></tbody></table></div>
  </section>
  <section class="card section">
    <h2>归一化信号汇总</h2>
    <p>这些节点与现有相似度模型的 node_index 使用同一口径。</p>
    <div class="table-wrap"><table><thead><tr><th>归一化信号</th><th>维度</th><th>变化</th><th>工单数</th><th>总变化次数</th><th>证据工单</th></tr></thead><tbody id="signalRows"></tbody></table></div>
  </section>
</main>
<script>
const RAW_DATA = __GRAPH_DATA__;
const DATA = collapseMaintenanceDisplay(RAW_DATA);
const TYPE_ORDER = ["phenomenon","signal","equipment","maintenance","work_order"];
const COLORS = {phenomenon:"#ef4444",signal:"#3b82f6",equipment:"#f59e0b",maintenance:"#10b981",work_order:"#8b5cf6"};
const FILLS = {phenomenon:"#fee2e2",signal:"#dbeafe",equipment:"#fef3c7",maintenance:"#d1fae5",work_order:"#ede9fe"};
const nodeById = new Map(DATA.nodes.map(n => [n.id,n]));
const edgeById = new Map(DATA.edges.map(e => [e.id,e]));
const svg = document.getElementById("graph");
const scope = document.getElementById("scope");
const workOrder = document.getElementById("workOrder");
const search = document.getElementById("search");
const detail = document.getElementById("detail");
let selectedId = "";
const selectedWorkOrders = new Set();

function esc(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function short(value, n=27) { const s=String(value??""); return s.length>n ? s.slice(0,n-1)+"…" : s; }
function maintenanceActionKeys(node) {
  const order=["reset","checked","function tested","tested","monitored","replaced","swapped","isolated","repaired"];
  const actions=new Set((node.member_actions||[]).map(value=>String(value).trim().toLocaleLowerCase()));
  return order.filter(action=>actions.has(action));
}
function maintenanceDisplayLabel(node) {
  if (node.type !== "maintenance") return node.label;
  const labels={reset:"复位",checked:"检查","function tested":"功能测试",tested:"测试",monitored:"持续监测",replaced:"更换",swapped:"互换测试",isolated:"隔离",repaired:"修复"};
  return maintenanceActionKeys(node).map(action=>labels[action]).join(" / ") || "维修处置";
}
function mergeUnique(target,values) {
  (Array.isArray(values)?values:[values]).forEach(value=>{if(value!==undefined&&value!==null&&!target.includes(value))target.push(value)});
}
function collapseMaintenanceDisplay(source) {
  const data=JSON.parse(JSON.stringify(source)),groups=new Map(),idMap=new Map();
  const regular=[];
  data.nodes.forEach(node=>{
    if(node.type!=="maintenance"){regular.push(node);return}
    const actions=maintenanceActionKeys(node),key=actions.join("|")||"other",id=`maintenance_category:${key}`;
    idMap.set(node.id,id);
    let group=groups.get(id);
    if(!group){group={...node,id,label:maintenanceDisplayLabel(node),canonical_node_ids:[],work_orders:[],case_ids:[],member_actions:[],member_methods:[],affected_equipment:[],affected_equipment_groups:[],raw_actions:[],method_work_orders:{}};groups.set(id,group)}
    mergeUnique(group.canonical_node_ids,node.id);mergeUnique(group.work_orders,node.work_orders||[]);mergeUnique(group.case_ids,node.case_ids||[]);mergeUnique(group.member_actions,node.member_actions||[]);mergeUnique(group.member_methods,node.member_methods||[]);mergeUnique(group.affected_equipment,node.affected_equipment||[]);mergeUnique(group.affected_equipment_groups,node.affected_equipment_groups||[]);mergeUnique(group.raw_actions,node.raw_actions||[]);
    Object.entries(node.method_work_orders||{}).forEach(([method,orders])=>{const merged=group.method_work_orders[method]||[];mergeUnique(merged,orders);group.method_work_orders[method]=merged});
    group.work_order_count=group.work_orders.length;group.case_count=group.case_ids.length;
  });
  const edgeGroups=new Map();
  data.edges.forEach(edge=>{
    const sourceId=idMap.get(edge.source)||edge.source,targetId=idMap.get(edge.target)||edge.target,key=`${edge.type}|${sourceId}|${targetId}`;
    let merged=edgeGroups.get(key);
    if(!merged){merged={...edge,id:`display_edge:${key}`,source:sourceId,target:targetId,canonical_edge_ids:[],work_orders:[],case_ids:[]};edgeGroups.set(key,merged)}
    mergeUnique(merged.canonical_edge_ids,edge.id);mergeUnique(merged.work_orders,edge.work_orders||[]);mergeUnique(merged.case_ids,edge.case_ids||[]);merged.weight=merged.work_orders.length;merged.evidence_count=merged.work_orders.length;
  });
  data.nodes=[...regular,...groups.values()];data.edges=[...edgeGroups.values()];
  data.overview_node_ids=[...new Set((data.overview_node_ids||[]).map(id=>idMap.get(id)||id))];
  if(data.meta?.node_counts_by_type)data.meta.node_counts_by_type.maintenance=groups.size;
  if(data.meta)data.meta.node_count=data.nodes.length;
  return data;
}

function setup() {
  const m=DATA.meta, counts=m.node_counts_by_type;
  const stats=[
    [m.case_count,"案例"],[m.work_order_count,"工单"],[counts.signal||0,"信号节点"],
    [counts.equipment||0,"设备/回路"],[counts.maintenance||0,"维修方法"],[m.edge_count,"证据关系"]
  ];
  document.getElementById("stats").innerHTML=stats.map(x=>`<div class="stat"><b>${x[0]}</b><span>${x[1]}</span></div>`).join("");
  DATA.work_orders.forEach(wo => { const o=document.createElement("option"); o.value=wo.work_order_no; o.textContent=`${wo.work_order_no} · ${wo.train}/${wo.car_no}`; workOrder.appendChild(o); });
  renderTables(); render();
}

function visibleNodeIds() {
  let ids;
  const wo=workOrder.value;
  if (wo) {
    ids=new Set(DATA.nodes.filter(n => n.id==="work_order:"+wo || (n.work_orders||[]).includes(wo)).map(n=>n.id));
    ids.add("phenomenon:vcb_trip");
  } else if (scope.value==="overview") ids=new Set(DATA.overview_node_ids);
  else ids=new Set(DATA.nodes.map(n=>n.id));
  const q=search.value.trim().toLocaleLowerCase();
  if (q) {
    const matched=new Set(DATA.nodes.filter(n => JSON.stringify(n).toLocaleLowerCase().includes(q)).map(n=>n.id));
    const expanded=new Set(matched);
    DATA.edges.forEach(e => { if(matched.has(e.source)||matched.has(e.target)){expanded.add(e.source);expanded.add(e.target);} });
    ids=new Set([...ids].filter(id=>expanded.has(id)));
    if ([...ids].some(id=>nodeById.get(id)?.type==="signal")) ids.add("phenomenon:vcb_trip");
  }
  return ids;
}

function render() {
  const ids=visibleNodeIds();
  const nodes=DATA.nodes.filter(n=>ids.has(n.id));
  const edges=DATA.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));
  const columns={}; TYPE_ORDER.forEach(t=>columns[t]=[]);
  nodes.forEach(n=>columns[n.type].push(n));
  TYPE_ORDER.forEach(t=>columns[t].sort((a,b)=>(b.work_order_count||0)-(a.work_order_count||0)||a.label.localeCompare(b.label,"zh-CN")));
  const maxCount=Math.max(1,...TYPE_ORDER.map(t=>columns[t].length));
  const height=Math.max(680,maxCount*54+100), width=1650;
  svg.setAttribute("width",width); svg.setAttribute("height",height); svg.innerHTML="";
  const xMap={phenomenon:40,signal:345,equipment:660,maintenance:975,work_order:1290};
  const positions=new Map();
  TYPE_ORDER.forEach(type=>{
    const list=columns[type]; const available=height-100; const step=Math.max(48,Math.min(70,available/Math.max(1,list.length)));
    const used=(list.length-1)*step; const start=65+Math.max(0,(available-used)/2);
    list.forEach((n,i)=>positions.set(n.id,{x:xMap[type],y:start+i*step}));
  });
  const ns="http://www.w3.org/2000/svg";
  edges.forEach(e=>{ const a=positions.get(e.source),b=positions.get(e.target); if(!a||!b)return; const p=document.createElementNS(ns,"path"); const x1=a.x+220,y1=a.y+18,x2=b.x,y2=b.y+18,m=(x1+x2)/2; p.setAttribute("d",`M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}`); p.setAttribute("class","edge"); p.dataset.source=e.source;p.dataset.target=e.target;p.dataset.edge=e.id; svg.appendChild(p); });
  nodes.forEach(n=>{ const p=positions.get(n.id); const g=document.createElementNS(ns,"g");g.setAttribute("class","node");g.dataset.id=n.id;g.setAttribute("transform",`translate(${p.x},${p.y})`);
    const r=document.createElementNS(ns,"rect");r.setAttribute("width",220);r.setAttribute("height",36);r.setAttribute("rx",8);r.setAttribute("fill",FILLS[n.type]);r.setAttribute("stroke",COLORS[n.type]);g.appendChild(r);
    const displayLabel=maintenanceDisplayLabel(n);
    const title=document.createElementNS(ns,"title");title.textContent=displayLabel;g.appendChild(title);
    const t=document.createElementNS(ns,"text");t.setAttribute("x",10);t.setAttribute("y",22);t.textContent=short(displayLabel);g.appendChild(t);
    g.addEventListener("click",()=>selectNode(n.id));svg.appendChild(g);
  });
  applyHighlights();
}

function selectNode(id,scroll=true) {
  selectedId=id; const n=nodeById.get(id); if(!n)return;
  detail.textContent=JSON.stringify(n,null,2);
  if (n.type==="work_order" && scope.value==="all" && !workOrder.value) {
    const wo=n.work_order_no;
    if(selectedWorkOrders.has(wo)) selectedWorkOrders.delete(wo); else selectedWorkOrders.add(wo);
    updateWorkOrderSelectionStatus();
  }
  applyHighlights();
  if(scroll) document.querySelector(".detail").scrollTop=0;
}

function applyHighlights() {
  const nodeElements=[...svg.querySelectorAll(".node")];
  const edgeElements=[...svg.querySelectorAll(".edge")];
  if(selectedWorkOrders.size) {
    const activeEdges=new Set();
    const activeNodes=new Set(["phenomenon:vcb_trip"]);
    DATA.edges.forEach(e=>{
      if((e.work_orders||[]).some(wo=>selectedWorkOrders.has(wo))) {
        activeEdges.add(e.id);activeNodes.add(e.source);activeNodes.add(e.target);
      }
    });
    nodeElements.forEach(g=>{
      const n=nodeById.get(g.dataset.id);
      const selected=n?.type==="work_order" && selectedWorkOrders.has(n.work_order_no);
      g.classList.toggle("selected",selected);
      g.classList.toggle("faded",!activeNodes.has(g.dataset.id));
    });
    edgeElements.forEach(p=>p.classList.toggle("highlight",activeEdges.has(p.dataset.edge)));
    return;
  }
  if(selectedId) {
    const neighbours=new Set([selectedId]);
    DATA.edges.forEach(e=>{if(e.source===selectedId)neighbours.add(e.target);if(e.target===selectedId)neighbours.add(e.source);});
    nodeElements.forEach(g=>{g.classList.toggle("selected",g.dataset.id===selectedId);g.classList.toggle("faded",!neighbours.has(g.dataset.id));});
    edgeElements.forEach(p=>p.classList.toggle("highlight",p.dataset.source===selectedId||p.dataset.target===selectedId));
    return;
  }
  nodeElements.forEach(g=>{g.classList.remove("selected","faded");});
  edgeElements.forEach(p=>p.classList.remove("highlight"));
}

function updateWorkOrderSelectionStatus() {
  const values=[...selectedWorkOrders].sort();
  document.getElementById("selectedWorkOrders").textContent=`已点亮工单（${values.length}）：${values.length ? values.join("、") : "无"}`;
}

function clearWorkOrderHighlights() {
  selectedWorkOrders.clear();selectedId="";updateWorkOrderSelectionStatus();applyHighlights();
}

function renderTables() {
  document.getElementById("workOrderRows").innerHTML=DATA.work_orders.map(w=>`<tr><td>${esc(w.work_order_no)}</td><td>${esc(w.train)} / ${esc(w.car_no)}</td><td>${w.case_count} / ${w.signal_count}</td><td>${esc((w.equipment||[]).join("、"))}</td><td>${esc((w.maintenance_methods||[]).join("；"))}</td><td>${esc(w.result)}</td><td>${esc(w.evidence_source)}</td></tr>`).join("");
  const signals=DATA.nodes.filter(n=>n.type==="signal").sort((a,b)=>(b.work_order_count||0)-(a.work_order_count||0)||(b.total_change_count||0)-(a.total_change_count||0));
  document.getElementById("signalRows").innerHTML=signals.map(n=>`<tr><td>${esc(n.label)}</td><td>${esc(n.normalized_dimension)}</td><td>${esc(n.previous_value)} → ${esc(n.current_value)}</td><td>${n.work_order_count}</td><td>${n.total_change_count||0}</td><td>${esc((n.work_orders||[]).join("、"))}</td></tr>`).join("");
}

scope.addEventListener("change",()=>{workOrder.value="";selectedId="";selectedWorkOrders.clear();updateWorkOrderSelectionStatus();render();});
workOrder.addEventListener("change",()=>{selectedId="";selectedWorkOrders.clear();updateWorkOrderSelectionStatus();render();});
search.addEventListener("input",render);
document.getElementById("clearWorkOrderHighlights").addEventListener("click",clearWorkOrderHighlights);
document.getElementById("reset").addEventListener("click",()=>{scope.value="overview";workOrder.value="";search.value="";selectedId="";selectedWorkOrders.clear();updateWorkOrderSelectionStatus();detail.textContent="尚未选择节点";render();});
setup();
</script>
</body>
</html>'''


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )


def write_outputs(graph_data: Dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(graph_data, ensure_ascii=False, indent=2, default=str)
    (output_dir / "vcb_trip_knowledge_graph.json").write_text(json_text, encoding="utf-8")
    write_csv(output_dir / "vcb_trip_nodes.csv", graph_data["nodes"])
    write_csv(output_dir / "vcb_trip_edges.csv", graph_data["edges"])
    embedded = json.dumps(graph_data, ensure_ascii=False, separators=(",", ":"), default=str).replace("</", "<\\/")
    (output_dir / "vcb_trip_knowledge_graph.html").write_text(
        HTML_TEMPLATE.replace("__GRAPH_DATA__", embedded),
        encoding="utf-8",
    )
    meta = graph_data["meta"]
    readme = f"""# VCB Trip 故障知识图谱

打开 `vcb_trip_knowledge_graph.html` 即可查看交互图谱，无需启动服务器。

图谱路径：`VCB Trip → 归一化信号表现 → 涉及设备 → 维修方法 → 来源工单`

- 案例：{meta['case_count']}
- 工单：{meta['work_order_count']}
- 节点：{meta['node_count']}
- 关系：{meta['edge_count']}
- 工单源文件：`{meta['workbook_source']}`
- 结构化工单源：`{meta['structured_reference_source']}`
- 案例库源：`{meta['case_library_source']}`

## 信号归一化规则

- 同一业务信号在不同车厢/设备单元中的编码合并为一个信号节点。
- 只有“信号名称”和“前值 → 后值”都相同时才合并。
- 已排除 `distance`；`BECU Speed` 保留在节点、关系和页面中。

## 设备和维修节点

- 图谱五列结构和连线方向不变，每个具体部件独立建点。
- 不根据共同出现关系合并 controller、receiver 或其他不同部件。
- 部件与维修动作的关系只来自同一工单中的结构化部件—动作配对。
- 维修节点界面只显示复位、检查、测试、更换、互换测试、隔离、修复等动作类型，不重复显示部件名称。
- 具体部件由设备节点展示；`member_methods`、`method_work_orders`、`affected_equipment` 和 `raw_actions` 仍保留具体维修动作与工单原文。
- 工单多选与整条线路点亮继续按照原工单证据计算。

## 文件

- `vcb_trip_knowledge_graph.html`：可直接查看和筛选的知识图谱页面。
- `vcb_trip_knowledge_graph.json`：包含节点、关系、工单证据的完整数据。
- `vcb_trip_nodes.csv`：节点表，可导入 Neo4j 等图数据库。
- `vcb_trip_edges.csv`：关系表，可导入 Neo4j 等图数据库。

## 关系含义

- `phenomenon_to_signal`：历史 VCB Trip 案例中观测到该归一化信号变化。
- `signal_to_equipment`：信号和设备出现在同一工单中，是证据关联而非单独证明因果。
- `equipment_to_maintenance`：工单记录对该设备执行了相应处置。
- `maintenance_to_work_order`：维修方法来源于对应工单。

## 导入 Neo4j

在项目目录的 PowerShell 中执行：

```powershell
py -3.13 -m pip install -r requirements-knowledge-graph.txt
py -3.13 import_vcb_knowledge_graph_to_neo4j.py
```

程序默认连接 `bolt://43.199.48.66:7687`，用户名为 `neo4j`，运行时会安全地询问密码。

如果 Neo4j 中已经导入过旧版图谱，请使用下面的命令替换旧版 VCB 图谱；它只清除 `graph_scope=vcb_trip` 的节点，不影响其他数据：

```powershell
py -3.13 import_vcb_knowledge_graph_to_neo4j.py --replace-existing
```
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 VCB Trip 故障知识图谱")
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--reference-json", type=Path, default=DEFAULT_REFERENCE_JSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    library_dir = args.library_dir.expanduser().resolve()
    reference_json = args.reference_json.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    graph_data = build_graph(library_dir, reference_json, ROOT)
    write_outputs(graph_data, output_dir)
    meta = graph_data["meta"]
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"知识图谱页面：{output_dir / 'vcb_trip_knowledge_graph.html'}")


if __name__ == "__main__":
    main()
