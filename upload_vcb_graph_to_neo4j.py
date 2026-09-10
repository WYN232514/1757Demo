"""一键上传 VCB 工单三列知识图谱到 Neo4j。

直接运行本文件，在终端输入 Neo4j 密码即可。密码输入时不会回显。
程序只替换 graph_scope='vcb_trip' 的数据，不会删除其他图谱。
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from import_vcb_work_order_action_component_to_neo4j import (
    DEFAULT_GRAPH_JSON,
    DEFAULT_URI,
    GRAPH_SCOPE,
    import_graph,
    load_graph,
)


ROOT = Path(__file__).resolve().parent
GRAPH_HTML = ROOT / "knowledge_graph" / "vcb_work_order_action_component_graph.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="上传 vcb_trip：根节点 → 工单 → 维修措施 → 相关部件"
    )
    parser.add_argument("--uri", default=DEFAULT_URI, help="Neo4j Bolt 地址")
    parser.add_argument("--username", default="neo4j", help="Neo4j 用户名")
    parser.add_argument("--database", default="neo4j", help="Neo4j 数据库名")
    parser.add_argument(
        "--graph-json",
        type=Path,
        default=DEFAULT_GRAPH_JSON,
        help="待上传的图谱 JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    graph_json = args.graph_json.expanduser().resolve()

    if not GRAPH_HTML.exists():
        print(f"错误：找不到知识图谱文件：{GRAPH_HTML}", file=sys.stderr)
        return 1
    if not graph_json.exists():
        print(f"错误：找不到 Neo4j 导入数据：{graph_json}", file=sys.stderr)
        print("请先运行：python build_vcb_work_order_action_component_graph.py", file=sys.stderr)
        return 1

    try:
        data = load_graph(graph_json)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"图谱数据校验失败：{exc}", file=sys.stderr)
        return 1

    expected_nodes = len(data["nodes"])
    expected_relationships = len(data["edges"])
    print("准备上传 VCB 三列知识图谱")
    print(f"  Neo4j：{args.uri}")
    print(f"  数据库：{args.database}")
    print(f"  用户名：{args.username}")
    print(f"  根节点：{GRAPH_SCOPE}")
    print(f"  节点数：{expected_nodes}")
    print(f"  关系数：{expected_relationships}")
    print("  范围：仅替换 graph_scope='vcb_trip'，其他图谱不受影响")
    print()

    password = getpass.getpass("请输入 Neo4j 密码（输入内容不会显示）：")
    if not password:
        print("密码不能为空，已取消上传。", file=sys.stderr)
        return 1

    try:
        result = import_graph(
            data=data,
            uri=args.uri,
            username=args.username,
            password=password,
            database=args.database,
            replace_existing=True,
        )
    except Exception as exc:
        print(f"上传失败：{exc}", file=sys.stderr)
        return 1

    if result != {
        "nodes": expected_nodes,
        "relationships": expected_relationships,
        "roots": 1,
    }:
        print("上传完成，但数据库校验结果与导入文件不一致：", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print()
    print("上传成功并已通过数据库校验。")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Neo4j Browser 查看完整图谱：")
    print(
        "MATCH p=(root:GraphRoot:KnowledgeEntity "
        "{id:'vcb_trip', graph_scope:'vcb_trip'})-[*0..3]->() RETURN p;"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
