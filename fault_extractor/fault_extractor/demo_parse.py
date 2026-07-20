from __future__ import annotations

import argparse
import json
from pathlib import Path

from work_order_reference_parser import build_reference_data_json


# Edit this one switch to True for full regeneration when launching by double-click.
REGENERATE_ALL = False

# Additional LLM attempts after the first structured-output request.
REPAIR_ATTEMPTS = 2

# Last-resort recovery is used only when the API responds but all responses are
# malformed. Network/authentication failures remain absent and retry next run.
FALLBACK_ON_INVALID_RESPONSE = True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-click demo: build or incrementally update output/reference_data.json."
    )
    parser.add_argument(
        "--regenerate-all",
        "--force",
        action="store_true",
        help="Regenerate every work order. Without this switch, existing valid entries are skipped.",
    )
    parser.add_argument("--input", help="Optional explicit .xlsx path; otherwise use the first file in input/.")
    parser.add_argument("--output", help="Optional explicit output JSON path.")
    parser.add_argument("--quiet", action="store_true", help="Disable row-level progress messages.")
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--checkpoint-every", type=int, default=1)
    parser.add_argument(
        "--repair-attempts",
        type=int,
        default=REPAIR_ATTEMPTS,
        help="Additional semantic LLM attempts after the first request (default: 2).",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable conservative recovery for malformed LLM responses.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    input_dir = root / "input"
    output_dir = root / "output"
    input_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    if args.input:
        input_xlsx = Path(args.input).expanduser().resolve()
    else:
        xlsx_files = sorted(
            path for path in input_dir.glob("*.xlsx") if not path.name.startswith("~$")
        )
        if not xlsx_files:
            raise FileNotFoundError(
                f"No .xlsx file was found in {input_dir}. Put a workbook there or use --input PATH."
            )
        input_xlsx = xlsx_files[0]

    output_json = Path(args.output).expanduser().resolve() if args.output else output_dir / "reference_data.json"

    summary = build_reference_data_json(
        xlsx_path=input_xlsx,
        output_json_path=output_json,
        config_path=root / "llm_api_config.json",
        regenerate_all=args.regenerate_all or REGENERATE_ALL,
        print_progress=not args.quiet,
        progress_every=args.progress_every,
        checkpoint_every=args.checkpoint_every,
        repair_attempts=args.repair_attempts,
        use_rule_fallback=FALLBACK_ON_INVALID_RESPONSE and not args.no_fallback,
    )

    print("Finished.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON saved to: {output_json}")
    if not (args.regenerate_all or REGENERATE_ALL):
        print("To regenerate every row, run: python demo_parse.py --regenerate-all")
    if summary.get("failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
