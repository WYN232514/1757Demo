from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from llm_api_tool import call_llm

OUTPUT_COLUMNS = ["work_order_no", "train", "fault_time", "time_order", "remark"]

_XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "office_rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}|\d{3,4})(?:\s*hrs?\b)?", re.IGNORECASE)
_TIME_THEN_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2}:\d{2}|\d{3,4})(?:\s*hrs?\b)?\s+"
    r"(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)(?!\d)",
    re.IGNORECASE,
)
_REPEAT_CLAUSE_RE = re.compile(
    r"repeat(?:ed)?\s+(?:at\s+[^()]{0,40}?\s+)?(?:on\s+)?([^()\n\r.;]+)",
    re.IGNORECASE,
)


def convert_work_orders_xlsx_to_csv(
    xlsx_path: str | Path,
    output_csv_path: str | Path,
    config_path: str | Path | None = None,
    use_llm: bool = True,
    fallback_to_regex: bool = True,
    print_progress: bool = True,
    progress_every: int = 1,
) -> Dict[str, Any]:
    """Convert a work-order XLSX workbook into a fault-time CSV.

    The function reads every sheet in *xlsx_path*, finds the header row whose
    first non-empty cell is ``NO.``, ignores all rows above it, then writes one
    CSV row for each input row. If a Description contains repeated fault
    instances, extra output rows are written with incremented ``time_order``.

    Args:
        xlsx_path: Input .xlsx file path.
        output_csv_path: Output .csv file path.
        config_path: Optional path to the LLM JSON config. Defaults to the
            config beside ``llm_api_tool.py``.
        use_llm: Whether to try the configured LLM parser first.
        fallback_to_regex: Whether to use the built-in parser when the LLM call
            fails or returns no usable event.
        print_progress: Whether to print progress messages to stdout.
        progress_every: Print one row-progress message every N valid input rows.
            Use 1 to print every parsed row.

    Returns:
        A summary dict with input/output row counts and parser usage counts.
    """
    xlsx_path = Path(xlsx_path).expanduser().resolve()
    output_csv_path = Path(output_csv_path).expanduser().resolve()
    config_path = Path(config_path).expanduser().resolve() if config_path else None
    progress_every = max(1, int(progress_every or 1))

    _progress(print_progress, f"Input XLSX: {xlsx_path}")
    _progress(print_progress, f"Output CSV: {output_csv_path}")
    _progress(print_progress, "Loading workbook...")
    sheets = _read_xlsx_sheets(xlsx_path)
    _progress(print_progress, f"Workbook loaded. Found {len(sheets)} sheet(s).")

    output_rows: List[Dict[str, Any]] = []
    input_rows = 0
    sheets_processed = 0
    llm_success_rows = 0
    fallback_rows = 0
    no_fault_time_rows = 0

    for sheet_no, (sheet_name, rows) in enumerate(sheets, start=1):
        _progress(print_progress, f"Scanning sheet {sheet_no}/{len(sheets)}: {sheet_name!r}")
        header_index = _find_header_row_index(rows)
        if header_index is None:
            _progress(print_progress, f"Skipped sheet {sheet_name!r}: no header row starting with 'NO.' was found.")
            continue

        headers = [_clean_header(x) for x in rows[header_index]]
        required = _build_required_column_map(headers)
        sheets_processed += 1
        data_row_candidates = max(0, len(rows) - header_index - 1)
        _progress(
            print_progress,
            f"Header found at row {header_index + 1}; omitted {header_index} top row(s); "
            f"scanning {data_row_candidates} possible data row(s).",
        )

        sheet_valid_rows = 0
        sheet_output_before = len(output_rows)
        for row_offset, raw_row in enumerate(rows[header_index + 1 :], start=1):
            row = _row_to_dict(headers, raw_row)
            if _is_blank_work_order_row(row, required):
                continue

            input_rows += 1
            sheet_valid_rows += 1
            sheet_row_no = header_index + 1 + row_offset
            work_order_no = _clean_cell(row.get(required["work_order_no"], ""))
            train = _clean_cell(row.get(required["train"], ""))
            description = _clean_cell(row.get(required["description"], ""))
            report_date = _clean_cell(row.get("Report Date", ""))
            change_date = _clean_cell(row.get("Change Date", ""))

            parser_name = "none"
            events: List[Dict[str, str]] = []
            show_row_progress = input_rows == 1 or progress_every <= 1 or input_rows % progress_every == 0
            if show_row_progress:
                _progress(
                    print_progress,
                    f"Parsing row {input_rows} "
                    f"(sheet={sheet_name!r}, sheet_row={sheet_row_no}, "
                    f"work_order_no={work_order_no or 'N/A'}, train={train or 'N/A'})...",
                )

            if use_llm and description:
                events = _extract_events_with_llm(
                    description=description,
                    report_date=report_date,
                    change_date=change_date,
                    config_path=config_path,
                )
                if events:
                    parser_name = "llm"
                    llm_success_rows += 1

            if (not events) and fallback_to_regex:
                events = _extract_events_with_regex(description, report_date or change_date)
                parser_name = "regex"
                fallback_rows += 1

            if not events:
                no_fault_time_rows += 1
                events = [{"fault_time": "", "remark": _make_remark(description, 0)}]

            if show_row_progress:
                _progress(
                    print_progress,
                    f"Row {input_rows} parsed by {parser_name}; extracted {len(events)} event row(s).",
                )

            for time_order, event in enumerate(events):
                fault_time = _normalize_fault_time(event.get("fault_time", ""))
                remark = _clean_cell(event.get("remark", "")) or _make_remark(description, time_order)
                output_rows.append(
                    {
                        "work_order_no": work_order_no,
                        "train": train,
                        "fault_time": fault_time,
                        "time_order": str(time_order),
                        "remark": remark,
                    }
                )

        _progress(
            print_progress,
            f"Finished sheet {sheet_name!r}: {sheet_valid_rows} valid input row(s), "
            f"{len(output_rows) - sheet_output_before} output row(s).",
        )

    _progress(print_progress, f"Writing CSV with {len(output_rows)} row(s)...")
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with output_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(output_rows)

    _progress(print_progress, f"CSV saved: {output_csv_path}")
    _progress(
        print_progress,
        "Summary: "
        f"sheets_processed={sheets_processed}, input_rows={input_rows}, "
        f"output_rows={len(output_rows)}, llm_success_rows={llm_success_rows}, "
        f"fallback_rows={fallback_rows}, no_fault_time_rows={no_fault_time_rows}",
    )

    return {
        "input_xlsx": str(xlsx_path),
        "output_csv": str(output_csv_path),
        "sheets_processed": sheets_processed,
        "input_rows": input_rows,
        "output_rows": len(output_rows),
        "llm_success_rows": llm_success_rows,
        "fallback_rows": fallback_rows,
        "no_fault_time_rows": no_fault_time_rows,
    }


def _read_xlsx_sheets(xlsx_path: Path) -> List[Tuple[str, List[List[str]]]]:
    """Read visible cell values from an XLSX file with only the Python stdlib."""
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Input XLSX not found: {xlsx_path}")

    with zipfile.ZipFile(xlsx_path) as zf:
        names = set(zf.namelist())
        shared_strings = _read_shared_strings(zf, names)
        sheet_refs = _read_sheet_refs(zf)
        sheets: List[Tuple[str, List[List[str]]]] = []

        for sheet_name, sheet_path in sheet_refs:
            if sheet_path not in names:
                continue
            rows = _read_sheet_rows(zf, sheet_path, shared_strings)
            sheets.append((sheet_name, rows))
        return sheets


def _read_shared_strings(zf: zipfile.ZipFile, names: set[str]) -> List[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: List[str] = []
    for si in root.findall("main:si", _XLSX_NS):
        text_parts = [node.text or "" for node in si.findall(".//main:t", _XLSX_NS)]
        strings.append("".join(text_parts))
    return strings


def _read_sheet_refs(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

    rid_to_target: Dict[str, str] = {}
    for rel in rels.findall("rel:Relationship", _XLSX_NS):
        rid = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if not rid or not target:
            continue
        if target.startswith("/"):
            normalized = target.lstrip("/")
        else:
            normalized = "xl/" + target.lstrip("/")
        rid_to_target[rid] = normalized.replace("//", "/")

    sheet_refs: List[Tuple[str, str]] = []
    for sheet in workbook.findall("main:sheets/main:sheet", _XLSX_NS):
        name = sheet.attrib.get("name", "Sheet")
        rid = sheet.attrib.get(f"{{{_XLSX_NS['office_rel']}}}id", "")
        target = rid_to_target.get(rid)
        if target:
            sheet_refs.append((name, target))
    return sheet_refs


def _read_sheet_rows(
    zf: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: Sequence[str],
) -> List[List[str]]:
    root = ET.fromstring(zf.read(sheet_path))
    rows: List[List[str]] = []

    for row_elem in root.findall(".//main:sheetData/main:row", _XLSX_NS):
        values_by_col: Dict[int, str] = {}
        for cell in row_elem.findall("main:c", _XLSX_NS):
            ref = cell.attrib.get("r", "")
            col_index = _column_index_from_cell_ref(ref)
            if col_index is None:
                continue
            values_by_col[col_index] = _read_cell_value(cell, shared_strings)

        if values_by_col:
            max_col = max(values_by_col)
            rows.append([values_by_col.get(i, "") for i in range(max_col + 1)])
        else:
            rows.append([])
    return rows


def _read_cell_value(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t", "")

    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", _XLSX_NS))

    value_node = cell.find("main:v", _XLSX_NS)
    if value_node is None:
        return ""

    value = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError):
            return value
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def _column_index_from_cell_ref(cell_ref: str) -> Optional[int]:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    if not letters:
        return None
    number = 0
    for char in letters.upper():
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number - 1


def _find_header_row_index(rows: Sequence[Sequence[str]]) -> Optional[int]:
    for i, row in enumerate(rows):
        first_non_empty = next((_clean_cell(cell) for cell in row if _clean_cell(cell)), "")
        if first_non_empty.upper() == "NO.":
            return i
    return None


def _clean_header(value: Any) -> str:
    return _clean_cell(value).replace("\ufeff", "")


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).replace("\xa0", " ").strip()


def _build_required_column_map(headers: Sequence[str]) -> Dict[str, str]:
    normalized = {header.lower(): header for header in headers if header}

    aliases = {
        "description": ["description"],
        "work_order_no": ["work order no.", "work order no", "work_order_no", "work order number"],
        "train": ["trainset", "train", "train set"],
    }

    result: Dict[str, str] = {}
    for key, candidates in aliases.items():
        found = ""
        for candidate in candidates:
            if candidate in normalized:
                found = normalized[candidate]
                break
        if not found:
            raise ValueError(
                f"Required column for {key!r} was not found. "
                f"Accepted headers: {candidates}. Actual headers: {list(headers)}"
            )
        result[key] = found
    return result


def _row_to_dict(headers: Sequence[str], row: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for index, header in enumerate(headers):
        if not header:
            continue
        result[header] = _clean_cell(row[index]) if index < len(row) else ""
    return result


def _is_blank_work_order_row(row: Dict[str, str], required: Dict[str, str]) -> bool:
    return not (_clean_cell(row.get(required["work_order_no"], "")) or _clean_cell(row.get(required["description"], "")))


def _extract_events_with_llm(
    description: str,
    report_date: str = "",
    change_date: str = "",
    config_path: Path | None = None,
) -> List[Dict[str, str]]:
    prompt = f"""
Extract all fault occurrence times from this train work-order Description.

Return only a JSON object with this exact shape:
{{
  "events": [
    {{"fault_time": "yyyy-mm-dd hh:mm:ss", "remark": "short fault summary"}}
  ]
}}

Rules:
1. Include the primary fault time and all repeat/repeated instances.
2. Convert all times to yyyy-mm-dd hh:mm:ss.
3. Treat dates as day/month/year unless clearly impossible.
4. Convert two-digit years as 20YY.
5. If a repeat date lacks a year, use the year from the primary fault date.
6. If a repeat contains only a time such as "repeat on 22:53", use the primary fault date.
7. If one date has multiple times like "07:32 & 07:34hrs", output one event per time.
8. If no clear fault occurrence time exists, return {{"events": []}}.
9. Do not include repair/report/finish/update times unless they are explicitly the fault occurrence in Description.

Reference Report Date, only for missing year fallback: {report_date or "N/A"}
Reference Change Date, only for missing year fallback: {change_date or "N/A"}
Description:
{description}
""".strip()

    result = call_llm(
        {
            "system_prompt": "You are a precise information extraction tool. Output valid JSON only.",
            "prompt": prompt,
            "temperature": 0,
            "top_p": 0.1,
            "max_tokens": 512,
        },
        config_path=config_path,
    )
    if not result.get("success"):
        return []

    return _parse_llm_events(str(result.get("content", "")))


def _parse_llm_events(content: str) -> List[Dict[str, str]]:
    data = _json_from_text(content)
    if data is None:
        return []

    if isinstance(data, dict):
        events_raw = data.get("events", [])
    elif isinstance(data, list):
        events_raw = data
    else:
        return []

    events: List[Dict[str, str]] = []
    seen: set[str] = set()
    for event in events_raw:
        if not isinstance(event, dict):
            continue
        fault_time = _normalize_fault_time(str(event.get("fault_time", "")))
        if not fault_time or fault_time in seen:
            continue
        seen.add(fault_time)
        events.append(
            {
                "fault_time": fault_time,
                "remark": _clean_cell(event.get("remark", "")),
            }
        )
    return events


def _json_from_text(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last != -1 and first < last:
        try:
            return json.loads(stripped[first : last + 1])
        except json.JSONDecodeError:
            return None
    return None


def _extract_events_with_regex(description: str, reference_date: str = "") -> List[Dict[str, str]]:
    text = _clean_cell(description)
    if not text:
        return []

    reference_year = _infer_reference_year(text, reference_date)
    found: List[Tuple[int, str]] = []

    # Pattern: time before date, e.g. "on 05:18hrs 18/05/2026".
    time_before_spans: List[Tuple[int, int]] = []
    for match in _TIME_THEN_DATE_RE.finditer(text):
        time_raw, date_raw = match.group(1), match.group(2)
        fault_time = _combine_date_time(date_raw, time_raw, reference_year)
        if fault_time:
            found.append((match.start(), fault_time))
            time_before_spans.append(match.span())

    # Pattern: date before one or more times, e.g. "9/6/26 07:32 & 07:34hrs".
    date_matches = list(_DATE_RE.finditer(text))
    for index, date_match in enumerate(date_matches):
        date_raw = date_match.group(1)
        segment_start = date_match.end()
        segment_end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(text)
        segment = text[segment_start:segment_end]

        # Keep the local search window compact so unrelated later numbers are not treated as times.
        hard_stop = re.search(r"(?:\(R\s*=|\)\s*\(|//|\.\s|;|\n)", segment, flags=re.IGNORECASE)
        if hard_stop:
            segment = segment[: hard_stop.start()]

        for time_match in _TIME_RE.finditer(segment):
            absolute_pos = segment_start + time_match.start()
            if _inside_any_span(absolute_pos, time_before_spans):
                continue
            time_raw = time_match.group(1)
            fault_time = _combine_date_time(date_raw, time_raw, reference_year)
            if fault_time:
                found.append((absolute_pos, fault_time))

    # Pattern: repeat clause with only a time, e.g. "repeat on 22:53".
    primary_date = _date_part(found[0][1]) if found else None
    if primary_date:
        for repeat_match in _REPEAT_CLAUSE_RE.finditer(text):
            clause = repeat_match.group(1)
            if _DATE_RE.search(clause):
                continue
            for time_match in _TIME_RE.finditer(clause):
                time_raw = time_match.group(1)
                fault_time = _combine_iso_date_time(primary_date, time_raw)
                if fault_time:
                    found.append((repeat_match.start(1) + time_match.start(), fault_time))

    found.sort(key=lambda item: item[0])
    events: List[Dict[str, str]] = []
    seen: set[str] = set()
    for _, fault_time in found:
        if fault_time in seen:
            continue
        seen.add(fault_time)
        events.append({"fault_time": fault_time, "remark": _make_remark(text, len(events))})
    return events


def _inside_any_span(position: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _infer_reference_year(text: str, reference_date: str = "") -> int:
    for candidate in [text, reference_date]:
        if not candidate:
            continue
        for match in _DATE_RE.finditer(candidate):
            parts = re.split(r"[./-]", match.group(1))
            if len(parts) >= 3 and parts[2]:
                year = _parse_year(parts[2])
                if year:
                    return year
        iso = re.search(r"(20\d{2}|19\d{2})", candidate)
        if iso:
            return int(iso.group(1))
    return _dt.datetime.now().year


def _parse_year(raw_year: str) -> Optional[int]:
    raw_year = raw_year.strip()
    if not raw_year:
        return None
    year = int(raw_year)
    if year < 100:
        return 2000 + year
    return year


def _combine_date_time(date_raw: str, time_raw: str, reference_year: int) -> str:
    parts = re.split(r"[./-]", date_raw.strip())
    if len(parts) < 2:
        return ""
    try:
        day = int(parts[0])
        month = int(parts[1])
        year = _parse_year(parts[2]) if len(parts) >= 3 else reference_year
        if year is None:
            return ""
        hour, minute = _parse_time(time_raw)
        return _dt.datetime(year, month, day, hour, minute, 0).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _combine_iso_date_time(date_iso: str, time_raw: str) -> str:
    try:
        year, month, day = (int(part) for part in date_iso.split("-"))
        hour, minute = _parse_time(time_raw)
        return _dt.datetime(year, month, day, hour, minute, 0).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _parse_time(time_raw: str) -> Tuple[int, int]:
    text = time_raw.strip()
    if ":" in text:
        hour_raw, minute_raw = text.split(":", 1)
        hour, minute = int(hour_raw), int(minute_raw[:2])
    else:
        digits = re.sub(r"\D", "", text)
        if len(digits) == 3:
            hour, minute = int(digits[0]), int(digits[1:])
        elif len(digits) == 4:
            hour, minute = int(digits[:2]), int(digits[2:])
        else:
            raise ValueError(f"Unsupported time: {time_raw}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {time_raw}")
    return hour, minute


def _date_part(fault_time: str) -> str:
    return fault_time.split(" ", 1)[0]


def _normalize_fault_time(value: str) -> str:
    text = _clean_cell(value)
    if not text:
        return ""
    text = text.replace("T", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?:Z|[+-]\d{2}:?\d{2})$", "", text).strip()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
    ]
    for fmt in formats:
        try:
            return _dt.datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ""


def _progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[work_order_fault_extractor] {message}", flush=True)


def _make_remark(description: str, time_order: int) -> str:
    text = _clean_cell(description)
    text = re.sub(r"^\(ML\)\s*", "", text, flags=re.IGNORECASE)

    cut_positions = []
    for pattern in [r"\s+on\s+\d", r"\s+at\s+\d", r"\s+during\s+", r"\(Repeat", r"\(R\s*="]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            cut_positions.append(match.start())
    if cut_positions:
        text = text[: min(cut_positions)]

    text = re.sub(r"\s+", " ", text).strip(" -;,.()")
    if not text:
        text = "Fault event"
    if len(text) > 120:
        text = text[:117].rstrip() + "..."
    if time_order > 0 and not re.search(r"\brepeat\b", text, flags=re.IGNORECASE):
        return f"Repeat {time_order}: {text}"
    return text


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract train work-order fault times from XLSX to CSV.")
    parser.add_argument("xlsx_path", help="Input .xlsx path")
    parser.add_argument("output_csv_path", help="Output .csv path")
    parser.add_argument("--config", default=None, help="Optional LLM config path")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM and use the built-in regex parser only")
    parser.add_argument("--no-fallback", action="store_true", help="Disable regex fallback")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress printing")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1,
        help="Print row progress every N valid input rows. Default: 1",
    )
    args = parser.parse_args()

    summary = convert_work_orders_xlsx_to_csv(
        args.xlsx_path,
        args.output_csv_path,
        config_path=args.config,
        use_llm=not args.no_llm,
        fallback_to_regex=not args.no_fallback,
        print_progress=not args.quiet,
        progress_every=args.progress_every,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
