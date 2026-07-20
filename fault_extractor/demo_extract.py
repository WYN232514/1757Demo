from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape

from work_order_fault_extractor import convert_work_orders_xlsx_to_csv


def main() -> None:
    root = Path(__file__).resolve().parent
    input_dir = root / "input"
    output_dir = root / "output"
    input_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    xlsx_files = sorted(input_dir.glob("*.xlsx"))
    if xlsx_files:
        input_xlsx = xlsx_files[0]
    else:
        input_xlsx = input_dir / "demo_work_orders.xlsx"
        _create_demo_xlsx(input_xlsx)
        print(f"No .xlsx file was found in {input_dir}. Generated demo file: {input_xlsx.name}")

    output_csv = output_dir / "output.csv"
    summary = convert_work_orders_xlsx_to_csv(
        input_xlsx,
        output_csv,
        config_path=root / "llm_api_config.json",
        use_llm=True,
        fallback_to_regex=True,
    )

    print("Finished.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV saved to: {output_csv}")


def _create_demo_xlsx(path: Path) -> None:
    """Create a tiny no-dependency XLSX so the demo can run immediately.

    To test your own file, place it in ./input/ and run this script again.
    """
    rows: List[List[str]] = [
        ["工单报表"],
        ["导出人demo"],
        ["NO.", "Description", "Work Order NO.", "Trainset"],
        [
            "1",
            "(ML)P073 TMS VCB Trip Detected on 27/05/26 20:33hrs(R=4)",
            "5001544878",
            "T25",
        ],
        [
            "2",
            "(ML)P026 VCB tripped twice at LOW DT on 9/6/26 07:32 & 07:34hrs(R=2)",
            "5001614276",
            "T9",
        ],
        [
            "3",
            "(ML)P107 VCB Tripped Open at DT SHS on 05/06/26 05:50hrs(R=0)(Repeat on 6/6 05:30, 05:41hrs)(CO)",
            "5001587808",
            "T36",
        ],
        [
            "4",
            "(ML)P068 TMS VCB Trip Detected during Tcap train preparation",
            "5001449874",
            "T23",
        ],
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types_xml())
        zf.writestr("_rels/.rels", _root_rels_xml())
        zf.writestr("xl/workbook.xml", _workbook_xml())
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        zf.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(rows))


def _worksheet_xml(rows: List[List[str]]) -> str:
    row_xml_parts: List[str] = []
    for row_index, row in enumerate(rows, start=1):
        cell_parts: List[str] = []
        for col_index, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(col_index)}{row_index}"
            cell_parts.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        row_xml_parts.append(f'<row r="{row_index}">{"".join(cell_parts)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
        '</worksheet>'
    )


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )


def _workbook_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )


def _workbook_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )


if __name__ == "__main__":
    main()
