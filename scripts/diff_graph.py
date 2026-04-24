#!/usr/bin/env python3
"""
Compara dois snapshots JSON do grafo Neo4j e reporta o que mudou.

Uso:
    python scripts/diff_graph.py OLD.json NEW.json
    python scripts/diff_graph.py data/neo4j/snapshots/graph_20260101.json \\
                                  data/neo4j/graph_export.json
    python scripts/diff_graph.py OLD.json NEW.json --output diff_report.json
    python scripts/diff_graph.py OLD.json NEW.json --label Genero  # filtra label
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def load(path: Path) -> dict:
    import gzip
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def index_nodes(data: dict) -> dict[str, dict]:
    return {n["id"]: n for n in data["nodes"]}


def index_rels(data: dict) -> dict[tuple, dict]:
    return {
        (r["start_node_id"], r["end_node_id"], r["type"]): r
        for r in data["relationships"]
    }


# ---------------------------------------------------------------------------
# Diff de nós
# ---------------------------------------------------------------------------

def diff_nodes(old_nodes: dict, new_nodes: dict, label_filter: str | None) -> dict:
    old_ids = set(old_nodes)
    new_ids = set(new_nodes)

    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    common_ids = old_ids & new_ids

    added = [new_nodes[i] for i in added_ids]
    removed = [old_nodes[i] for i in removed_ids]

    modified = []
    for nid in common_ids:
        old_props = old_nodes[nid].get("properties", {})
        new_props = new_nodes[nid].get("properties", {})
        changed_keys = {
            k for k in set(old_props) | set(new_props)
            if old_props.get(k) != new_props.get(k)
        }
        if changed_keys:
            modified.append({
                "id": nid,
                "label": new_nodes[nid]["label"],
                "changed_properties": sorted(changed_keys),
                "old": {k: old_props.get(k) for k in changed_keys},
                "new": {k: new_props.get(k) for k in changed_keys},
            })

    if label_filter:
        added = [n for n in added if n["label"] == label_filter]
        removed = [n for n in removed if n["label"] == label_filter]
        modified = [n for n in modified if n["label"] == label_filter]

    return {"added": added, "removed": removed, "modified": modified}


# ---------------------------------------------------------------------------
# Diff de arestas
# ---------------------------------------------------------------------------

def diff_rels(old_rels: dict, new_rels: dict) -> dict:
    old_keys = set(old_rels)
    new_keys = set(new_rels)
    return {
        "added": [new_rels[k] for k in (new_keys - old_keys)],
        "removed": [old_rels[k] for k in (old_keys - new_keys)],
    }


# ---------------------------------------------------------------------------
# Sumário por label / tipo
# ---------------------------------------------------------------------------

def summarize_by_label(nodes: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        counts[n["label"]] += 1
    return dict(counts)


def summarize_by_reltype(rels: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rels:
        counts[r["type"]] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Impressão
# ---------------------------------------------------------------------------

def print_node_diff(node_diff: dict):
    added_by_label = summarize_by_label(node_diff["added"])
    removed_by_label = summarize_by_label(node_diff["removed"])
    all_labels = sorted(set(added_by_label) | set(removed_by_label))

    t = Table(title="Nós — diferenças por label", show_lines=True)
    t.add_column("Label", style="cyan")
    t.add_column("Adicionados", justify="right", style="green")
    t.add_column("Removidos", justify="right", style="red")
    for label in all_labels:
        t.add_row(label, str(added_by_label.get(label, 0)), str(removed_by_label.get(label, 0)))
    t.add_section()
    t.add_row("[bold]TOTAL[/]",
              f"[bold green]+{len(node_diff['added'])}[/]",
              f"[bold red]-{len(node_diff['removed'])}[/]")
    console.print(t)

    if node_diff["modified"]:
        t2 = Table(title=f"Nós modificados ({len(node_diff['modified'])})", show_lines=True)
        t2.add_column("ID", style="dim")
        t2.add_column("Label")
        t2.add_column("Propriedades alteradas")
        for n in node_diff["modified"][:50]:
            t2.add_row(n["id"][:60], n["label"], ", ".join(n["changed_properties"]))
        if len(node_diff["modified"]) > 50:
            console.print(f"  [dim]... e mais {len(node_diff['modified'])-50} modificações[/]")
        console.print(t2)


def print_rel_diff(rel_diff: dict):
    added_by_type = summarize_by_reltype(rel_diff["added"])
    removed_by_type = summarize_by_reltype(rel_diff["removed"])
    all_types = sorted(set(added_by_type) | set(removed_by_type))

    if not all_types:
        console.print("[dim]Sem alterações de arestas.[/]")
        return

    t = Table(title="Arestas — diferenças por tipo", show_lines=True)
    t.add_column("Tipo", style="green")
    t.add_column("Adicionadas", justify="right", style="green")
    t.add_column("Removidas", justify="right", style="red")
    for rt in all_types:
        t.add_row(rt, str(added_by_type.get(rt, 0)), str(removed_by_type.get(rt, 0)))
    t.add_section()
    t.add_row("[bold]TOTAL[/]",
              f"[bold green]+{len(rel_diff['added'])}[/]",
              f"[bold red]-{len(rel_diff['removed'])}[/]")
    console.print(t)


def print_metadata_diff(old_meta: dict, new_meta: dict):
    t = Table(title="Metadata", show_lines=True)
    t.add_column("Campo")
    t.add_column("Antes", style="dim")
    t.add_column("Depois", style="bold")
    for key in ("version", "exported_at", "node_count", "relationship_count"):
        old_val = str(old_meta.get(key, "—"))
        new_val = str(new_meta.get(key, "—"))
        style = "green" if old_val != new_val else ""
        t.add_row(key, old_val, f"[{style}]{new_val}[/]" if style else new_val)
    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.argument("old_file", type=click.Path(exists=True, path_type=Path))
@click.argument("new_file", type=click.Path(exists=True, path_type=Path))
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Salva relatório de diff em JSON")
@click.option("--label", "label_filter", default=None,
              help="Filtra diff de nós para um label específico")
@click.option("--quiet", is_flag=True, help="Só imprime sumário numérico")
def main(old_file: Path, new_file: Path, output: str | None, label_filter: str | None, quiet: bool):
    """Compara dois snapshots JSON do grafo e reporta as diferenças."""

    console.rule("[bold]Diff de Grafo Neo4j[/]")
    console.print(f"  [dim]OLD:[/] {old_file}")
    console.print(f"  [dim]NEW:[/] {new_file}\n")

    old_data = load(old_file)
    new_data = load(new_file)

    old_nodes = index_nodes(old_data)
    new_nodes = index_nodes(new_data)
    old_rels = index_rels(old_data)
    new_rels = index_rels(new_data)

    node_diff = diff_nodes(old_nodes, new_nodes, label_filter)
    rel_diff = diff_rels(old_rels, new_rels)

    if not quiet:
        print_metadata_diff(old_data.get("metadata", {}), new_data.get("metadata", {}))
        console.print()
        print_node_diff(node_diff)
        console.print()
        print_rel_diff(rel_diff)
    else:
        n_add = len(node_diff["added"])
        n_rem = len(node_diff["removed"])
        n_mod = len(node_diff["modified"])
        r_add = len(rel_diff["added"])
        r_rem = len(rel_diff["removed"])
        console.print(
            f"Nós: +{n_add} -{n_rem} ~{n_mod}  |  "
            f"Arestas: +{r_add} -{r_rem}"
        )

    if output:
        report = {
            "old_file": str(old_file),
            "new_file": str(new_file),
            "nodes": {
                "added_count": len(node_diff["added"]),
                "removed_count": len(node_diff["removed"]),
                "modified_count": len(node_diff["modified"]),
                "added_by_label": summarize_by_label(node_diff["added"]),
                "removed_by_label": summarize_by_label(node_diff["removed"]),
                "modified": node_diff["modified"],
            },
            "relationships": {
                "added_count": len(rel_diff["added"]),
                "removed_count": len(rel_diff["removed"]),
                "added_by_type": summarize_by_reltype(rel_diff["added"]),
                "removed_by_type": summarize_by_reltype(rel_diff["removed"]),
            },
        }
        with open(output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        console.print(f"\n[green]✓[/] Relatório salvo em {output}")

    # Código de saída: 1 se houve mudanças (útil em CI)
    changed = any([
        node_diff["added"], node_diff["removed"], node_diff["modified"],
        rel_diff["added"], rel_diff["removed"],
    ])
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
