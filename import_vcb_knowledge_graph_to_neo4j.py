#!/usr/bin/env python3
"""Import the generated VCB Trip knowledge graph into Neo4j.

This importer is intentionally non-destructive.  It uses MERGE with stable
node and relationship IDs, so running it again updates the same VCB graph
instead of duplicating it.  Existing unrelated Neo4j data is never deleted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_GRAPH_JSON = ROOT / "knowledge_graph" / "vcb_trip_knowledge_graph.json"
DEFAULT_URI = "bolt://43.199.48.66:7687"

NODE_LABELS = {
    "phenomenon": "FaultPhenomenon",
    "signal": "SignalManifestation",
    "equipment": "Equipment",
    "maintenance": "MaintenanceMethod",
    "work_order": "WorkOrder",
}

RELATIONSHIP_TYPES = {
    "phenomenon_to_signal": "MANIFESTS_AS",
    "signal_to_equipment": "ASSOCIATED_WITH",
    "equipment_to_maintenance": "TREATED_BY",
    "maintenance_to_work_order": "EVIDENCED_BY",
}


def chunks(items: Sequence[Dict[str, Any]], size: int) -> Iterable[Sequence[Dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def neo4j_property(value: Any) -> Any:
    """Convert JSON values to legal Neo4j property values.

    Neo4j supports scalars and homogeneous scalar arrays as properties.  Rich
    dictionaries, or arrays containing dictionaries, are retained as JSON
    strings instead of being discarded.
    """
    if is_scalar(value):
        return value
    if isinstance(value, list) and all(is_scalar(item) and item is not None for item in value):
        scalar_types = {type(item) for item in value}
        if len(scalar_types) <= 1 or scalar_types <= {int, float}:
            return value
        return [str(item) for item in value]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def prepare_properties(record: Mapping[str, Any], excluded: set[str]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {"graph_scope": "vcb_trip"}
    for key, value in record.items():
        if key in excluded or value is None:
            continue
        properties[str(key)] = neo4j_property(value)
    return properties


def load_and_validate(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("图谱 JSON 必须同时包含 nodes 和 edges 数组")

    node_ids = [str(node.get("id", "")) for node in nodes]
    edge_ids = [str(edge.get("id", "")) for edge in edges]
    if not all(node_ids) or len(node_ids) != len(set(node_ids)):
        raise ValueError("节点 ID 存在空值或重复")
    if not all(edge_ids) or len(edge_ids) != len(set(edge_ids)):
        raise ValueError("关系 ID 存在空值或重复")

    unknown_node_types = sorted({str(node.get("type")) for node in nodes} - set(NODE_LABELS))
    unknown_edge_types = sorted({str(edge.get("type")) for edge in edges} - set(RELATIONSHIP_TYPES))
    if unknown_node_types:
        raise ValueError(f"存在不能导入的节点类型：{unknown_node_types}")
    if unknown_edge_types:
        raise ValueError(f"存在不能导入的关系类型：{unknown_edge_types}")

    node_id_set = set(node_ids)
    broken = [
        edge.get("id")
        for edge in edges
        if str(edge.get("source")) not in node_id_set or str(edge.get("target")) not in node_id_set
    ]
    if broken:
        raise ValueError(f"存在找不到起点或终点的关系：{broken[:10]}")
    return data


def prepare_nodes(data: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {node_type: [] for node_type in NODE_LABELS}
    for node in data["nodes"]:
        node_type = str(node["type"])
        grouped[node_type].append(
            {
                "id": str(node["id"]),
                "properties": prepare_properties(node, {"id", "type"}),
            }
        )
    return grouped


def prepare_edges(data: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {edge_type: [] for edge_type in RELATIONSHIP_TYPES}
    for edge in data["edges"]:
        edge_type = str(edge["type"])
        grouped[edge_type].append(
            {
                "id": str(edge["id"]),
                "source": str(edge["source"]),
                "target": str(edge["target"]),
                "properties": prepare_properties(edge, {"id", "type", "source", "target"}),
            }
        )
    return grouped


def create_constraints(session: Any) -> None:
    session.run(
        "CREATE CONSTRAINT vcb_knowledge_entity_id IF NOT EXISTS "
        "FOR (n:KnowledgeEntity) REQUIRE n.id IS UNIQUE"
    ).consume()
    session.run(
        "CREATE INDEX vcb_knowledge_entity_scope IF NOT EXISTS "
        "FOR (n:KnowledgeEntity) ON (n.graph_scope)"
    ).consume()


def upsert_node_batch(tx: Any, label: str, rows: Sequence[Dict[str, Any]]) -> None:
    query = f"""
    UNWIND $rows AS row
    MERGE (n:KnowledgeEntity:{label} {{id: row.id}})
    SET n += row.properties,
        n.id = row.id,
        n.updated_at = datetime()
    """
    tx.run(query, rows=list(rows)).consume()


def upsert_edge_batch(tx: Any, relationship_type: str, rows: Sequence[Dict[str, Any]]) -> None:
    query = f"""
    UNWIND $rows AS row
    MATCH (source:KnowledgeEntity {{id: row.source}})
    MATCH (target:KnowledgeEntity {{id: row.target}})
    MERGE (source)-[r:{relationship_type} {{id: row.id}}]->(target)
    SET r += row.properties,
        r.id = row.id,
        r.updated_at = datetime()
    """
    tx.run(query, rows=list(rows)).consume()


def import_graph(
    data: Mapping[str, Any],
    uri: str,
    username: str,
    password: str,
    database: str,
    batch_size: int,
    replace_existing: bool,
) -> Dict[str, Any]:
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError(
            "尚未安装 Neo4j 驱动，请先运行："
            "py -3.13 -m pip install -r requirements-knowledge-graph.txt"
        ) from exc

    node_groups = prepare_nodes(data)
    edge_groups = prepare_edges(data)
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        session_kwargs = {"database": database} if database else {}
        with driver.session(**session_kwargs) as session:
            if replace_existing:
                deleted = session.run(
                    """
                    MATCH (n:KnowledgeEntity {graph_scope: 'vcb_trip'})
                    DETACH DELETE n
                    RETURN count(*) AS deleted_count
                    """
                ).single()
                print(f"已清除旧版 VCB 图谱节点：{int(deleted['deleted_count'])}")
            create_constraints(session)
            for node_type, rows in node_groups.items():
                label = NODE_LABELS[node_type]
                for batch in chunks(rows, batch_size):
                    session.execute_write(upsert_node_batch, label, batch)
                print(f"节点已写入：{label} = {len(rows)}")

            for edge_type, rows in edge_groups.items():
                relationship_type = RELATIONSHIP_TYPES[edge_type]
                for batch in chunks(rows, batch_size):
                    session.execute_write(upsert_edge_batch, relationship_type, batch)
                print(f"关系已写入：{relationship_type} = {len(rows)}")

            verification = session.run(
                """
                MATCH (n:KnowledgeEntity {graph_scope: 'vcb_trip'})
                WITH count(n) AS node_count
                MATCH (:KnowledgeEntity {graph_scope: 'vcb_trip'})
                      -[r {graph_scope: 'vcb_trip'}]->
                      (:KnowledgeEntity {graph_scope: 'vcb_trip'})
                RETURN node_count, count(r) AS relationship_count
                """
            ).single()
            return {
                "node_count": int(verification["node_count"]),
                "relationship_count": int(verification["relationship_count"]),
            }
    finally:
        driver.close()


def print_dry_run(data: Mapping[str, Any], path: Path, uri: str, database: str) -> None:
    node_counts = Counter(str(node["type"]) for node in data["nodes"])
    edge_counts = Counter(str(edge["type"]) for edge in data["edges"])
    print("仅检查，不连接 Neo4j，也不写入数据。")
    print(f"图谱文件：{path}")
    print(f"目标地址：{uri}")
    print(f"目标数据库：{database or '(服务器默认数据库)'}")
    print("节点：")
    for node_type, count in node_counts.items():
        print(f"  {NODE_LABELS[node_type]}: {count}")
    print("关系：")
    for edge_type, count in edge_counts.items():
        print(f"  {RELATIONSHIP_TYPES[edge_type]}: {count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 VCB Trip 知识图谱导入 Neo4j")
    parser.add_argument("--graph-json", type=Path, default=DEFAULT_GRAPH_JSON)
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", DEFAULT_URI))
    parser.add_argument("--username", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="导入前仅清除 graph_scope=vcb_trip 的旧图谱；不会删除其他 Neo4j 数据",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查文件，不连接或写入 Neo4j")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    graph_path = args.graph_json.expanduser().resolve()
    if args.batch_size < 1:
        raise ValueError("batch-size 必须大于 0")
    data = load_and_validate(graph_path)

    if args.dry_run:
        print_dry_run(data, graph_path, args.uri, args.database)
        return

    password = args.password or getpass.getpass("请输入 Neo4j 密码（输入时不会显示）：")
    if not password:
        raise ValueError("Neo4j 密码不能为空")
    print(f"正在连接 {args.uri} ...")
    result = import_graph(
        data=data,
        uri=args.uri,
        username=args.username,
        password=password,
        database=args.database,
        batch_size=args.batch_size,
        replace_existing=args.replace_existing,
    )
    print("\n导入完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("浏览器地址：http://43.199.48.66:7474/browser/")
    print("查看命令：MATCH (n:KnowledgeEntity {graph_scope:'vcb_trip'}) RETURN n LIMIT 100")


if __name__ == "__main__":
    main()
