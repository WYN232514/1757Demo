"""Import the standalone three-column VCB graph with one vcb_trip root node."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_GRAPH_JSON = ROOT / "knowledge_graph" / "vcb_work_order_action_component_graph.json"
DEFAULT_URI = "bolt://43.199.48.66:7687"
GRAPH_SCOPE = "vcb_trip"

NODE_LABELS = {
    "root": "GraphRoot",
    "work_order": "WorkOrder",
    "method": "MaintenanceMethod",
    "component": "Equipment",
}
RELATIONSHIP_TYPES = {
    "root_to_work_order": "HAS_WORK_ORDER",
    "work_order_to_method": "HAS_MAINTENANCE_ACTION",
    "method_to_component": "TARGETS_COMPONENT",
}


def neo4j_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(item is not None and isinstance(item, (str, int, float, bool)) for item in value):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def properties(record: dict[str, Any], excluded: set[str]) -> dict[str, Any]:
    return {
        **{
            key: neo4j_value(value)
            for key, value in record.items()
            if key not in excluded and value is not None
        },
        "graph_scope": GRAPH_SCOPE,
    }


def load_graph(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes, edges = data.get("nodes"), data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("图谱 JSON 必须包含 nodes 和 edges 数组")
    node_ids = [str(node.get("id") or "") for node in nodes]
    edge_ids = [str(edge.get("id") or "") for edge in edges]
    if not all(node_ids) or len(node_ids) != len(set(node_ids)):
        raise ValueError("节点 ID 为空或重复")
    if not all(edge_ids) or len(edge_ids) != len(set(edge_ids)):
        raise ValueError("关系 ID 为空或重复")
    unknown_nodes = sorted({node.get("type") for node in nodes} - set(NODE_LABELS))
    unknown_edges = sorted({edge.get("type") for edge in edges} - set(RELATIONSHIP_TYPES))
    if unknown_nodes or unknown_edges:
        raise ValueError(f"未知类型：nodes={unknown_nodes}, edges={unknown_edges}")
    if not any(node.get("id") == "vcb_trip" and node.get("type") == "root" for node in nodes):
        raise ValueError("缺少根节点 vcb_trip")
    node_id_set = set(node_ids)
    if any(edge.get("source") not in node_id_set or edge.get("target") not in node_id_set for edge in edges):
        raise ValueError("存在指向未知节点的关系")
    return data


def import_graph(data: dict[str, Any], uri: str, username: str, password: str, database: str, replace_existing: bool) -> dict[str, int]:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        kwargs = {"database": database} if database else {}
        with driver.session(**kwargs) as session:
            if replace_existing:
                deleted = session.run(
                    "MATCH (n:KnowledgeEntity {graph_scope:$scope}) DETACH DELETE n RETURN count(n) AS count",
                    scope=GRAPH_SCOPE,
                ).single()
                print(f"已删除旧 scope：{int(deleted['count'])} 个节点")

            session.run(
                "CREATE CONSTRAINT knowledge_entity_id IF NOT EXISTS FOR (n:KnowledgeEntity) REQUIRE n.id IS UNIQUE"
            ).consume()

            for node_type, label in NODE_LABELS.items():
                rows = [
                    {"id": node["id"], "properties": properties(node, {"id", "type"})}
                    for node in data["nodes"]
                    if node["type"] == node_type
                ]
                if rows:
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MERGE (n:KnowledgeEntity:{label} {{id:row.id}})
                        SET n += row.properties, n.updated_at=datetime()
                        """,
                        rows=rows,
                    ).consume()
                    print(f"节点写入 {label}: {len(rows)}")

            for edge_type, relationship in RELATIONSHIP_TYPES.items():
                rows = [
                    {
                        "id": edge["id"],
                        "source": edge["source"],
                        "target": edge["target"],
                        "properties": properties(edge, {"id", "source", "target", "type"}),
                    }
                    for edge in data["edges"]
                    if edge["type"] == edge_type
                ]
                if rows:
                    session.run(
                        f"""
                        UNWIND $rows AS row
                        MATCH (source:KnowledgeEntity {{id:row.source, graph_scope:$scope}})
                        MATCH (target:KnowledgeEntity {{id:row.target, graph_scope:$scope}})
                        MERGE (source)-[r:{relationship} {{id:row.id}}]->(target)
                        SET r += row.properties, r.updated_at=datetime()
                        """,
                        rows=rows,
                        scope=GRAPH_SCOPE,
                    ).consume()
                    print(f"关系写入 {relationship}: {len(rows)}")

            result = session.run(
                """
                MATCH (n:KnowledgeEntity {graph_scope:$scope})
                WITH count(n) AS nodes
                MATCH (:KnowledgeEntity {graph_scope:$scope})-[r {graph_scope:$scope}]->(:KnowledgeEntity {graph_scope:$scope})
                WITH nodes, count(r) AS relationships
                MATCH (root:KnowledgeEntity:GraphRoot {id:'vcb_trip', graph_scope:$scope})
                RETURN nodes, relationships, count(root) AS roots
                """,
                scope=GRAPH_SCOPE,
            ).single()
            return {key: int(result[key]) for key in ("nodes", "relationships", "roots")}
    finally:
        driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="导入工单→维修措施→相关部件三列图谱")
    parser.add_argument("--graph-json", type=Path, default=DEFAULT_GRAPH_JSON)
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", DEFAULT_URI))
    parser.add_argument("--username", default=os.getenv("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    graph_path = args.graph_json.expanduser().resolve()
    data = load_graph(graph_path)
    if args.dry_run:
        print(f"图谱：{graph_path}")
        print("节点：", dict(Counter(node["type"] for node in data["nodes"])))
        print("关系：", dict(Counter(edge["type"] for edge in data["edges"])))
        print("根节点：vcb_trip")
        return

    password = args.password or getpass.getpass("请输入 Neo4j 密码：")
    if not password:
        raise ValueError("Neo4j 密码不能为空")
    result = import_graph(data, args.uri, args.username, password, args.database, args.replace_existing)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
