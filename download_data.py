# -*- coding: utf-8 -*-
"""Download a fault data window using credentials from the local .env file."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from gateway_download import download_fault_window


ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = ROOT / ".env"
REQUIRED_ENV_KEYS = (
    "RAILIVERSE_ENDPOINT",
    "RAILIVERSE_CLIENT_ID",
    "RAILIVERSE_CLIENT_SECRET",
)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from *path* into the current process."""
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            raise ValueError(f"Invalid .env line {line_number}: empty key")
        os.environ[key] = value

    missing = [key for key in REQUIRED_ENV_KEYS if not os.getenv(key, "").strip()]
    if missing:
        raise RuntimeError(f"Missing required .env values: {', '.join(missing)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use the current project's .env to download a fault data window."
    )
    parser.add_argument("--train", required=True, help="Train number, for example T9")
    parser.add_argument(
        "--fault-time",
        required=True,
        help='Fault time in Beijing time, for example "2026-07-16 10:10:00"',
    )
    parser.add_argument("--fault-code", default="", help="Fault code, for example P026")
    parser.add_argument("--before-min", type=int, default=30)
    parser.add_argument("--after-min", type=int, default=30)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Metrics per request; default 1 to match the known working example",
    )
    parser.add_argument(
        "--time-is-utc",
        action="store_true",
        help="Interpret --fault-time as UTC; by default it is Beijing time",
    )
    parser.add_argument(
        "--metrics-json",
        default=str(ROOT / "gateway_metrics.json"),
        help="Metric list JSON file",
    )
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "gateway_downloads" / "queries"),
        help="Directory in which downloaded query folders are created",
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--force", action="store_true", help="Ignore an existing cache")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        load_env_file(Path(args.env_file).resolve())
        result = download_fault_window(
            train=args.train,
            fault_time=args.fault_time,
            fault_code=args.fault_code,
            time_is_cst=not args.time_is_utc,
            before_min=args.before_min,
            after_min=args.after_min,
            metrics_json=args.metrics_json,
            output_root=args.output_root,
            batch_size=args.batch_size,
            reuse=not args.force,
        )
    except Exception as exc:
        message = str(exc)
        if "HTTP 403" in message and "cloudfront" in message.lower():
            print(
                "Download failed: HTTP 403 (blocked by CloudFront).\n"
                "The endpoint is complete, but this computer/network is not allowed "
                "to access it. Run the same command on the permitted intranet computer "
                "or ask the administrator to allow this computer's source IP.",
                file=sys.stderr,
            )
        else:
            print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    print("Download completed")
    print(f"Output directory: {result.data_dir.resolve()}")
    print(f"Rows: {result.row_count}")
    print(f"Metrics: {result.metric_count}")
    print(f"Reused cache: {result.reused}")
    for path in result.files:
        print(f"File: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
