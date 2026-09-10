"""Test the GET fault-analysis page endpoint and open it in a browser."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://phm.railiverse.com/mtr-1757/api/v1/fault-analysis"


def main() -> int:
    parser = argparse.ArgumentParser(description="测试故障分析页面 GET 接口")
    parser.add_argument("--url", default=DEFAULT_URL, help="GET 接口地址")
    parser.add_argument("--no-browser", action="store_true", help="只检查响应，不打开浏览器")
    args = parser.parse_args()

    params = {
        "equipInstance": "T25",
        "equipUnit": "09",
        "eventTime": "2026-05-18 21:25:52",
        "eventCode": "1757",
    }
    page_url = f"{args.url}?{urlencode(params)}"

    print(f"请求地址：{page_url}")
    try:
        request = Request(
            page_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 fault-analysis-interface-test/1.0",
            },
        )
        with urlopen(request, timeout=30) as response:
            status_code = response.status
            content_type = response.headers.get("Content-Type", "").lower()
            response_body = response.read()
        print(f"HTTP 状态：{status_code}")
        print(f"Content-Type：{content_type}")
        if "text/html" not in content_type:
            print("接口没有返回 HTML 页面。")
            print(response_body[:1000].decode("utf-8", errors="replace"))
            return 1
        print("页面接口响应正常。")
    except HTTPError as exc:
        print(f"HTTP 检查失败：{exc.code} {exc.reason}", file=sys.stderr)
        print("响应内容：" + exc.read(1000).decode("utf-8", errors="replace"), file=sys.stderr)
        if args.no_browser:
            return 1
    except URLError as exc:
        print(f"网络检查失败：{exc.reason}", file=sys.stderr)
        print("如果接口只能从内网访问，请在能访问该内网的电脑上运行本脚本。", file=sys.stderr)
        if args.no_browser:
            return 1

    if not args.no_browser:
        opened = webbrowser.open(page_url, new=2)
        print("已交给默认浏览器打开。" if opened else "浏览器未自动打开，请复制上面的请求地址访问。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
