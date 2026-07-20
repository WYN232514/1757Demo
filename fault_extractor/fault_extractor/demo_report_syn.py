from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict

from report_syn import report_synthesis

# Default example: a new VCB-trip-like time series is most similar to these
# documented historical work orders in the bundled reference_data file.
DEFAULT_SIMILAR_FAULT_INFO: Dict[str, float] = {
    "5001614276": 0.94,
    "5001587808": 0.90,
    "5001505712": 0.86,
    "5001544878": 0.81,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-click demo: synthesize a Markdown report from similar historical faults."
    )
    parser.add_argument(
        "--reference",
        help="Optional reference_data JSON path. Default: output/reference_data_with_cause_inferenced.json.",
    )
    parser.add_argument(
        "--similar",
        help="Optional JSON object string or JSON file path, e.g. '{\"5001614276\": 0.94}'.",
    )
    parser.add_argument(
        "--output",
        help="Optional output .md path. Default: output/synthesized_fault_reference_report.md.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM synthesis and use the deterministic report generator.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)

    reference_path = (
        Path(args.reference).expanduser().resolve()
        if args.reference
        else output_dir / "reference_data_with_cause_inferenced.json"
    )
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else output_dir / "synthesized_fault_reference_report.md"
    )
    similar_fault_info = _load_similar_fault_info(args.similar) if args.similar else DEFAULT_SIMILAR_FAULT_INFO

    previous_disable = os.environ.get("REPORT_SYN_DISABLE_LLM")
    if args.no_llm:
        os.environ["REPORT_SYN_DISABLE_LLM"] = "1"
    try:
        report = report_synthesis(similar_fault_info, reference_path)
    finally:
        if args.no_llm:
            if previous_disable is None:
                os.environ.pop("REPORT_SYN_DISABLE_LLM", None)
            else:
                os.environ["REPORT_SYN_DISABLE_LLM"] = previous_disable

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")

    print("Finished.")
    print("similar_fault_info:")
    print(json.dumps(similar_fault_info, ensure_ascii=False, indent=2))
    print(f"Markdown report saved to: {output_path}")


def _load_similar_fault_info(value: str) -> Dict[str, float]:
    path = Path(value).expanduser()
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("--similar must be a JSON object mapping work_order_no to similarity.")
    return {str(key): float(score) for key, score in raw.items()}


if __name__ == "__main__":
    main()
