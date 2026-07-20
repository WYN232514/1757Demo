# -*- coding: utf-8 -*-
"""调用 VCB/1757 Fault Analysis API 的命令行测试客户端。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


DEFAULT_API_URL = "http://127.0.0.1:8000/api/v1/fault-analysis"


def prompt_if_empty(value: str | None, label: str, example: str) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return input(f"请输入{label}（例如 {example}）：").strip()


def call_fault_analysis(
    api_url: str,
    api_key: str,
    train: str,
    car_no: str,
    fault_time: str,
    fault_code: str,
    top_k: int = 5,
    similarity_threshold: float = 0.7,
    force_download: bool = False,
    timeout: int = 600,
) -> dict[str, Any]:
    payload = {
        "train": train,
        "car_no": car_no,
        "fault_time": fault_time,
        "fault_code": fault_code,
        "top_k": top_k,
        "similarity_threshold": similarity_threshold,
        "time_is_cst": True,
        "force_download": force_download,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    print(f"\n正在调用：{api_url}")
    print("请求参数：")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    try:
        response = requests.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.ConnectionError as exc:
        raise RuntimeError(
            "无法连接 API。请确认 Docker 服务已经启动，且 API 地址/公网隧道正确。"
        ) from exc
    except requests.Timeout as exc:
        raise RuntimeError(f"调用超过 {timeout} 秒仍未完成。") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"HTTP 调用失败：{exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API 返回的不是 JSON：HTTP {response.status_code}，内容={response.text[:500]}"
        ) from exc

    if not response.ok:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        raise RuntimeError(f"API 返回错误：HTTP {response.status_code}，{detail}")
    return body


def print_result(result: dict[str, Any]) -> None:
    request_info = result.get("request", {})
    download = result.get("download", {})
    analysis = result.get("analysis", {})
    work_orders = result.get("similar_work_orders", []) or []
    report = result.get("fault_report", {}) or {}

    print("\n========== 调用成功 ==========")
    print("request_id：", result.get("request_id", ""))
    print(
        "查询故障：",
        f"{request_info.get('train', '')} / {request_info.get('car_no', '')}车 / "
        f"{request_info.get('fault_time', '')} / {request_info.get('fault_code', '')}",
    )
    print(
        "下载数据：",
        f"变量 {download.get('metric_count', 0)} 个，"
        f"数据 {download.get('row_count', 0)} 行，"
        f"缓存复用={download.get('reused', False)}",
    )
    print(
        "分析结果：",
        f"节点 {analysis.get('node_count', 0)} 个，"
        f"RF状态={analysis.get('ramping_finish_status', '')}，"
        f"相似工单 {analysis.get('match_count', len(work_orders))} 个",
    )
    print("耗时：", result.get("elapsed_seconds", ""), "秒")

    if not work_orders:
        print("\n没有工单达到设置的最低相似度。")
        return

    print("\n========== 相似工单 ==========")
    for item in work_orders:
        work_order = item.get("work_order", item)
        print(
            f"\n#{item.get('rank', '')} 工单 {work_order.get('work_order_no', '')} "
            f"（最终相似度 {float(item.get('similarity', 0)):.4f}）"
        )
        print(
            f"  车号/车厢：{work_order.get('train', '')} / {work_order.get('car_no', '')}　"
            f"故障时间：{work_order.get('fault_time', '')}"
        )
        print(f"  备注：{work_order.get('remark', '')}")
        description = str(work_order.get("long_description", "") or "").strip()
        if description:
            print(f"  工单描述：{description}")
        structured = work_order.get("structured_info") or {}
        if structured:
            print(f"  故障现象：{structured.get('fault', '')}")
            print(f"  涉及部件：{structured.get('component', '')}")
            print(f"  原因判断：{structured.get('cause', '')}")
            print(f"  采取动作：{structured.get('action', '')}")
            print(f"  处理结果：{structured.get('result', '')}")

    print("\n========== 故障维修参考报告 ==========")
    if not report.get("available"):
        print(report.get("reason", "报告不可用"))
        return
    print("推荐来源：", report.get("recommendation_source", ""))
    for title, key in (
        ("情势判断", "situation_assessment"),
        ("建议排查步骤", "recommended_steps"),
        ("重点参考", "document_focus"),
        ("注意事项", "cautions"),
    ):
        print(f"\n{title}：")
        for index, text in enumerate(report.get(key, []) or [], start=1):
            print(f"  {index}. {text}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试 VCB/1757 故障分析 HTTP API")
    parser.add_argument(
        "--url",
        default=os.getenv("FAULT_API_URL", DEFAULT_API_URL),
        help="API 完整地址；也可使用 FAULT_API_URL 环境变量",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("API_KEY", ""),
        help="X-API-Key；也可使用 API_KEY 环境变量",
    )
    parser.add_argument("--train", help="车号，例如 T9")
    parser.add_argument("--car-no", help="车厢号，例如 05")
    parser.add_argument("--fault-time", help="故障时间，例如 2026-07-16 10:10:00")
    parser.add_argument("--fault-code", help="故障码，例如 P026")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Ignore a previously downloaded query cache",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="额外打印完整 JSON 响应",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    train = prompt_if_empty(args.train, "车号", "T9")
    car_no = prompt_if_empty(args.car_no, "车厢号", "05")
    fault_time = prompt_if_empty(args.fault_time, "故障时间", "2026-07-16 10:10:00")
    fault_code = prompt_if_empty(args.fault_code, "eventCode/故障码", "1757")

    try:
        result = call_fault_analysis(
            api_url=args.url,
            api_key=args.api_key,
            train=train,
            car_no=car_no,
            fault_time=fault_time,
            fault_code=fault_code,
            top_k=args.top_k,
            similarity_threshold=args.threshold,
            force_download=args.force_download,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(f"\n调用失败：{exc}", file=sys.stderr)
        return 1

    print_result(result)
    if args.raw:
        print("\n========== 完整 JSON ==========")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
