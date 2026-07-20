from __future__ import annotations

import argparse
import json
from pathlib import Path

from work_order_reference_parser import _load_work_order_records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that every work order in an XLSX workbook has a structured JSON record."
    )
    parser.add_argument("xlsx_path")
    parser.add_argument("reference_json_path")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx_path).expanduser().resolve()
    json_path = Path(args.reference_json_path).expanduser().resolve()
    records, _ = _load_work_order_records(xlsx_path, print_progress=False)
    database = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(database, dict):
        raise ValueError(f"Structured reference file must contain a JSON object: {json_path}")

    workbook_orders = set(records)
    structured_orders = {str(key).strip() for key in database}
    missing = sorted(workbook_orders - structured_orders)
    stale = sorted(structured_orders - workbook_orders)

    print(f"Workbook work orders: {len(workbook_orders)}")
    print(f"Structured records:    {len(structured_orders)}")
    if stale:
        print(f"Stale structured records (kept): {', '.join(stale)}")
    if missing:
        print(f"ERROR: missing structured work orders: {', '.join(missing)}")
        return 1

    print("Structured work-order coverage: 100%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
