#!/usr/bin/env python3
"""
Exporta o grafo Neo4j completo para JSON e/ou CSV versionável.

Uso:
    python scripts/export_neo4j.py                         # JSON → data/neo4j/graph_export.json
    python scripts/export_neo4j.py --snapshot              # JSON timestamped em snapshots/
    python scripts/export_neo4j.py --format csv            # CSV por rótulo em data/neo4j/csv/
    python scripts/export_neo4j.py --format all            # JSON + CSV
    python scripts/export_neo4j.py --gzip                  # comprime a saída JSON
    python scripts/export_neo4j.py --output custom.json    # caminho personalizado
    python scripts/export_neo4j.py --batch-size 2000       # lotes maiores (mais RAM)

Variáveis de ambiente (opcional):
    NEO4J_URI       bolt://localhost:7687
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  taxonomist2026
"""

import csv
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from neo4j import Driver
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# Adiciona src/ ao path para importar graph.connection
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from graph.connection import get_driver_with_retry

console = Console()
log = logging.getLogger(__name__)

DEFAULT_OUTPUT = Path("data/neo4j/graph_export.json")
SNAPSHOTS_DIR = Path("data/neo4j/snapshots")
CSV_DIR = Path("data/neo4j/csv")
BATCH = 1000


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

def discover_schema(driver: Driver) -> dict:
    """Descobre labels, tipos de relação e property keys do banco."""
    with driver.session() as s:
        labels = [r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label ORDER BY label")]
        rel_types = [r["relationshipType"] for r in s.run(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType"
        )]
        prop_keys = [r["propertyKey"] for r in s.run(
            "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey ORDER BY propertyKey"
        )]
        counts = s.run(
            "MATCH (n) WITH count(n) AS nodes "
            "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
        ).single()

    return {
        "labels": labels,
        "relationship_types": rel_types,
        "property_keys": prop_keys,
        "node_count": counts["nodes"],
        "relationship_count": counts["rels"],
    }


def count_by_label(driver: Driver, labels: list[str]) -> dict[str, int]:
    with driver.session() as s:
        result = {}
        for label in labels:
            row = s.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()
            result[label] = row["c"]
    return result


def count_by_reltype(driver: Driver, rel_types: list[str]) -> dict[str, int]:
    with driver.session() as s:
        result = {}
        for rt in rel_types:
            row = s.run(f"MATCH ()-[r:`{rt}`]->() RETURN count(r) AS c").single()
            result[rt] = row["c"]
    return result


# ---------------------------------------------------------------------------
# ID estável por nó
# ---------------------------------------------------------------------------

_ID_QUERY = """
MATCH (n)
RETURN
  CASE WHEN n.id IS NOT NULL THEN toString(n.id)
       WHEN n.binomial IS NOT NULL THEN toLower(replace(n.binomial,' ','_'))
       ELSE elementId(n)
  END AS node_id,
  labels(n)[0] AS label,
  properties(n) AS props,
  elementId(n) AS element_id
SKIP $skip LIMIT $limit
"""

_REL_QUERY = """
MATCH (a)-[r]->(b)
RETURN
  CASE WHEN a.id IS NOT NULL THEN toString(a.id)
       WHEN a.binomial IS NOT NULL THEN toLower(replace(a.binomial,' ','_'))
       ELSE elementId(a)
  END AS start_id,
  CASE WHEN b.id IS NOT NULL THEN toString(b.id)
       WHEN b.binomial IS NOT NULL THEN toLower(replace(b.binomial,' ','_'))
       ELSE elementId(b)
  END AS end_id,
  type(r) AS rel_type,
  properties(r) AS props
SKIP $skip LIMIT $limit
"""


# ---------------------------------------------------------------------------
# Exportação — nós
# ---------------------------------------------------------------------------

def export_nodes(driver: Driver, total: int, batch: int) -> list[dict]:
    nodes = []
    seen_ids: dict[str, int] = {}  # detecta colisões de ID

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Nós[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("exportando nós", total=total)

        skip = 0
        while True:
            with driver.session() as s:
                batch_rows = s.run(_ID_QUERY, skip=skip, limit=batch).data()
            if not batch_rows:
                break
            for row in batch_rows:
                node_id = row["node_id"]
                # Resolve colisão de ID acrescentando sufixo numérico
                if node_id in seen_ids:
                    seen_ids[node_id] += 1
                    node_id = f"{node_id}_{seen_ids[node_id]}"
                else:
                    seen_ids[node_id] = 0

                nodes.append({
                    "id": node_id,
                    "label": row["label"] or "Unknown",
                    "properties": _clean_props(row["props"]),
                    "_element_id": row["element_id"],  # guardado para resolução de arestas
                })
            progress.advance(task, len(batch_rows))
            skip += batch
            if len(batch_rows) < batch:
                break

    return nodes


# ---------------------------------------------------------------------------
# Exportação — relacionamentos
# ---------------------------------------------------------------------------

def export_relationships(driver: Driver, total: int, batch: int) -> list[dict]:
    rels = []
    seen: set[tuple] = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Arestas[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("exportando arestas", total=total)

        skip = 0
        while True:
            with driver.session() as s:
                batch_rows = s.run(_REL_QUERY, skip=skip, limit=batch).data()
            if not batch_rows:
                break
            for row in batch_rows:
                key = (row["start_id"], row["end_id"], row["rel_type"])
                if key in seen:
                    continue
                seen.add(key)
                rels.append({
                    "type": row["rel_type"],
                    "start_node_id": row["start_id"],
                    "end_node_id": row["end_id"],
                    "properties": _clean_props(row["props"]),
                })
            progress.advance(task, len(batch_rows))
            skip += batch
            if len(batch_rows) < batch:
                break

    return rels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_props(props: dict) -> dict:
    """Serializa propriedades não suportadas diretamente em JSON."""
    if not props:
        return {}
    clean = {}
    for k, v in props.items():
        if k == "_element_id":
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[k] = v
        elif isinstance(v, list):
            clean[k] = [str(i) if not isinstance(i, (str, int, float, bool)) else i for i in v]
        else:
            clean[k] = str(v)
    return clean


def build_metadata(schema: dict, label_counts: dict, rel_counts: dict, version: str) -> dict:
    return {
        "project": "Taxonomista Digital — Hifomicetos Aquáticos",
        "version": version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "node_count": schema["node_count"],
        "relationship_count": schema["relationship_count"],
        "labels": schema["labels"],
        "relationship_types": schema["relationship_types"],
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
        "property_keys": schema["property_keys"],
    }


# ---------------------------------------------------------------------------
# Saída — JSON
# ---------------------------------------------------------------------------

def write_json(data: dict, output_path: Path, use_gzip: bool) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if use_gzip:
        gz_path = output_path.with_suffix(".json.gz")
        with gzip.open(gz_path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return gz_path
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_path


# ---------------------------------------------------------------------------
# Saída — CSV (um arquivo por label + um para arestas)
# ---------------------------------------------------------------------------

def write_csv(nodes: list[dict], rels: list[dict], csv_dir: Path):
    csv_dir.mkdir(parents=True, exist_ok=True)

    # Agrupa nós por label
    by_label: dict[str, list] = {}
    for n in nodes:
        by_label.setdefault(n["label"], []).append(n)

    written = []
    for label, label_nodes in by_label.items():
        safe_label = label.replace(" ", "_").replace("/", "_")
        path = csv_dir / f"nodes_{safe_label}.csv"
        all_keys = sorted({k for n in label_nodes for k in n["properties"]})
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id"] + all_keys)
            writer.writeheader()
            for n in label_nodes:
                row = {"id": n["id"]}
                row.update({k: n["properties"].get(k, "") for k in all_keys})
                writer.writerow(row)
        written.append(path)

    # Arestas
    rel_path = csv_dir / "relationships.csv"
    all_props = sorted({k for r in rels for k in r["properties"]})
    with open(rel_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["start_node_id", "end_node_id", "type"] + all_props)
        writer.writeheader()
        for r in rels:
            row = {"start_node_id": r["start_node_id"], "end_node_id": r["end_node_id"], "type": r["type"]}
            row.update({k: r["properties"].get(k, "") for k in all_props})
            writer.writerow(row)
    written.append(rel_path)

    return written


# ---------------------------------------------------------------------------
# Relatório final
# ---------------------------------------------------------------------------

def print_report(metadata: dict, output_paths: list[Path]):
    table = Table(title="[bold]Exportação Neo4j concluída[/]", show_lines=True)
    table.add_column("Label", style="cyan")
    table.add_column("Nós", justify="right")
    for label, count in sorted(metadata["label_counts"].items()):
        table.add_row(label, str(count))
    console.print(table)

    table2 = Table(title="Relacionamentos", show_lines=True)
    table2.add_column("Tipo", style="green")
    table2.add_column("Total", justify="right")
    for rt, count in sorted(metadata["relationship_type_counts"].items()):
        table2.add_row(rt, str(count))
    console.print(table2)

    console.print(f"\n[bold]Total:[/] {metadata['node_count']} nós · "
                  f"{metadata['relationship_count']} arestas\n")
    for p in output_paths:
        size_mb = p.stat().st_size / 1_048_576
        console.print(f"  [green]✓[/] {p}  ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--uri", default=None, envvar="NEO4J_URI",
              help="URI Bolt (padrão: NEO4J_URI ou bolt://localhost:7687)")
@click.option("--user", default=None, envvar="NEO4J_USER", help="Usuário Neo4j")
@click.option("--password", default=None, envvar="NEO4J_PASSWORD", help="Senha Neo4j")
@click.option("--output", "-o", default=None, type=click.Path(),
              help=f"Arquivo de saída JSON (padrão: {DEFAULT_OUTPUT})")
@click.option("--snapshot", is_flag=True,
              help="Salva snapshot timestamped em data/neo4j/snapshots/")
@click.option("--format", "fmt", default="json",
              type=click.Choice(["json", "csv", "all"]), show_default=True,
              help="Formato de exportação")
@click.option("--gzip", "use_gzip", is_flag=True, help="Comprime saída JSON com gzip")
@click.option("--batch-size", default=BATCH, show_default=True,
              help="Número de registros por lote de consulta")
@click.option("--version", "graph_version", default=None,
              help="Versão do grafo para o metadata (ex: 2.0.0)")
@click.option("--retries", default=3, show_default=True,
              help="Tentativas de conexão com back-off exponencial")
def main(uri, user, password, output, snapshot, fmt, use_gzip, batch_size, graph_version, retries):
    """Exporta o grafo Neo4j completo para JSON/CSV versionável."""

    console.rule("[bold cyan]Export Neo4j → Arquivo[/]")

    # Conexão
    console.print("Conectando ao Neo4j...", end=" ")
    driver = get_driver_with_retry(uri=uri, user=user, password=password, retries=retries)
    console.print("[green]OK[/]")

    # Schema
    console.print("Descobrindo schema...")
    schema = discover_schema(driver)
    console.print(f"  {len(schema['labels'])} labels · "
                  f"{len(schema['relationship_types'])} tipos de relação · "
                  f"{schema['node_count']:,} nós · "
                  f"{schema['relationship_count']:,} arestas")

    label_counts = count_by_label(driver, schema["labels"])
    rel_counts = count_by_reltype(driver, schema["relationship_types"])

    # Versão automática se não fornecida
    if not graph_version:
        graph_version = datetime.now(timezone.utc).strftime("%Y.%m.%d")

    metadata = build_metadata(schema, label_counts, rel_counts, graph_version)

    # Exportar nós
    console.print("\nExportando nós...")
    nodes = export_nodes(driver, schema["node_count"], batch_size)

    # Exportar arestas
    console.print("\nExportando arestas...")
    rels = export_relationships(driver, schema["relationship_count"], batch_size)

    driver.close()

    graph_data = {"metadata": metadata, "nodes": nodes, "relationships": rels}

    # Determinar caminhos de saída
    output_paths: list[Path] = []

    if fmt in ("json", "all"):
        if snapshot:
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            json_path = SNAPSHOTS_DIR / f"graph_{ts}.json"
        elif output:
            json_path = Path(output)
        else:
            json_path = DEFAULT_OUTPUT

        written = write_json(graph_data, json_path, use_gzip)
        output_paths.append(written)

        # Sempre mantém uma cópia como "current" se não for snapshot
        if not snapshot and not output:
            pass  # o arquivo já é o current
        elif snapshot:
            # Atualiza também o arquivo "current"
            current = write_json(graph_data, DEFAULT_OUTPUT, False)
            console.print(f"  [dim]current atualizado: {current}[/]")

    if fmt in ("csv", "all"):
        written_csvs = write_csv(nodes, rels, CSV_DIR)
        output_paths.extend(written_csvs)

    # Relatório
    console.print()
    print_report(metadata, output_paths)


if __name__ == "__main__":
    main()
