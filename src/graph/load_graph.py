"""
Graph loader: imports the aquatic hyphomycetes knowledge graph JSON into Neo4j.

Usage:
    python src/graph/load_graph.py --json data/neo4j/aquatic_hyphomycetes_graph.json
    python src/graph/load_graph.py --json data/neo4j/aquatic_hyphomycetes_graph.json --uri bolt://localhost:7687
"""

import json
import logging
from pathlib import Path
from typing import Optional

import click
from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class GraphLoader:
    """Loads the aquatic hyphomycetes knowledge graph into Neo4j."""

    def __init__(self, uri: str = "bolt://localhost:7687", user: str = "neo4j", password: str = "password"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Connected to Neo4j at {uri}")

    def close(self):
        self.driver.close()

    def clear_database(self):
        """Remove all nodes and relationships."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            logger.info("Database cleared")

    def create_constraints(self, label_types: list[str]):
        """Create uniqueness constraints for all node labels."""
        with self.driver.session() as session:
            for label in label_types:
                try:
                    session.run(
                        f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                    )
                    logger.info(f"Constraint created for :{label}")
                except Exception as e:
                    logger.warning(f"Constraint for {label}: {e}")

    def load_nodes(self, nodes: list[dict]) -> int:
        """Load all nodes into Neo4j."""
        count = 0
        with self.driver.session() as session:
            for node in nodes:
                label = node["label"]
                node_id = node["id"]
                props = {**node["properties"], "id": node_id}

                # Handle list properties (Neo4j doesn't support nested objects in MERGE)
                clean_props = {}
                for k, v in props.items():
                    if isinstance(v, (str, int, float, bool)):
                        clean_props[k] = v
                    elif isinstance(v, list) and all(isinstance(i, str) for i in v):
                        clean_props[k] = v
                    else:
                        clean_props[k] = json.dumps(v, ensure_ascii=False)

                session.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                    id=node_id,
                    props=clean_props,
                )
                count += 1

        logger.info(f"Loaded {count} nodes")
        return count

    def load_relationships(self, relationships: list[dict]) -> int:
        """Load all relationships into Neo4j."""
        count = 0
        with self.driver.session() as session:
            for rel in relationships:
                rel_type = rel["type"]
                start_id = rel["start_node_id"]
                end_id = rel["end_node_id"]
                props = rel.get("properties", {})

                # Clean properties
                clean_props = {}
                for k, v in props.items():
                    if isinstance(v, (str, int, float, bool)):
                        clean_props[k] = v
                    else:
                        clean_props[k] = json.dumps(v, ensure_ascii=False)

                query = (
                    f"MATCH (a {{id: $start_id}}), (b {{id: $end_id}}) "
                    f"MERGE (a)-[r:{rel_type}]->(b) "
                    f"SET r += $props"
                )
                result = session.run(query, start_id=start_id, end_id=end_id, props=clean_props)
                summary = result.consume()
                if summary.counters.relationships_created > 0:
                    count += 1

        logger.info(f"Loaded {count} relationships")
        return count

    def load_from_json(self, json_path: str, clear: bool = True) -> dict:
        """Full pipeline: load JSON graph into Neo4j."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        logger.info(f"Loaded JSON: {len(data['nodes'])} nodes, {len(data['relationships'])} relationships")

        if clear:
            self.clear_database()

        # Extract unique labels
        labels = list(set(n["label"] for n in data["nodes"]))
        self.create_constraints(labels)

        n_nodes = self.load_nodes(data["nodes"])
        n_rels = self.load_relationships(data["relationships"])

        stats = {
            "nodes_loaded": n_nodes,
            "relationships_loaded": n_rels,
            "labels": labels,
            "version": data.get("metadata", {}).get("version", "unknown"),
        }
        logger.info(f"Import complete: {stats}")
        return stats

    def verify_import(self) -> dict:
        """Run verification queries after import."""
        with self.driver.session() as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            orphans = session.run(
                "MATCH (n) WHERE NOT (n)--() RETURN n.id AS id, labels(n) AS labels"
            ).data()
            genera_no_order = session.run(
                "MATCH (g:Genus) WHERE NOT (g)-[:BELONGS_TO_ORDER]->() "
                "RETURN g.name AS name, g.id AS id"
            ).data()

        return {
            "total_nodes": node_count,
            "total_relationships": rel_count,
            "orphan_nodes": orphans,
            "genera_without_order": genera_no_order,
        }


@click.command()
@click.option("--json", "json_path", required=True, help="Path to the graph JSON file")
@click.option("--uri", default="bolt://localhost:7687", help="Neo4j URI")
@click.option("--user", default="neo4j", help="Neo4j username")
@click.option("--password", default="password", help="Neo4j password")
@click.option("--no-clear", is_flag=True, help="Don't clear database before import")
@click.option("--verify", is_flag=True, help="Run verification after import")
def main(json_path: str, uri: str, user: str, password: str, no_clear: bool, verify: bool):
    loader = GraphLoader(uri=uri, user=user, password=password)
    try:
        stats = loader.load_from_json(json_path, clear=not no_clear)
        click.echo(f"\n✓ Import complete: {stats['nodes_loaded']} nodes, {stats['relationships_loaded']} relationships")

        if verify:
            verification = loader.verify_import()
            click.echo(f"\n--- Verification ---")
            click.echo(f"Nodes in DB: {verification['total_nodes']}")
            click.echo(f"Relationships in DB: {verification['total_relationships']}")
            click.echo(f"Orphan nodes: {len(verification['orphan_nodes'])}")
            click.echo(f"Genera without order: {len(verification['genera_without_order'])}")
            for g in verification["genera_without_order"]:
                click.echo(f"  ⚠ {g['name']} (expected for polyphyletic genera)")
    finally:
        loader.close()


if __name__ == "__main__":
    main()
