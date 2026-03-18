"""
Graph coherence validator: audits the knowledge graph JSON for structural
and taxonomic consistency before Neo4j import.

Usage:
    python src/graph/validate.py --json data/neo4j/aquatic_hyphomycetes_graph.json
"""

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass
class AuditResult:
    category: str
    severity: str  # "ERROR", "WARNING", "INFO"
    message: str
    items: list = field(default_factory=list)


class GraphValidator:
    """Validates structural and taxonomic coherence of the knowledge graph."""

    def __init__(self, json_path: str):
        with open(json_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.nodes = {n["id"]: n for n in self.data["nodes"]}
        self.rels = self.data["relationships"]
        self.results: list[AuditResult] = []

    def _add(self, category: str, severity: str, message: str, items: list = None):
        self.results.append(AuditResult(category, severity, message, items or []))

    def check_orphan_nodes(self):
        connected = set()
        for r in self.rels:
            connected.add(r["start_node_id"])
            connected.add(r["end_node_id"])
        orphans = [
            f"{self.nodes[n]['label']}: {self.nodes[n]['properties'].get('name', n)}"
            for n in self.nodes
            if n not in connected
        ]
        self._add("Orphan Nodes", "ERROR" if orphans else "INFO",
                   f"{len(orphans)} nodes without any connections", orphans)

    def check_broken_references(self):
        broken = []
        for r in self.rels:
            if r["start_node_id"] not in self.nodes:
                broken.append(f"Missing start: {r['start_node_id']}")
            if r["end_node_id"] not in self.nodes:
                broken.append(f"Missing end: {r['end_node_id']}")
        self._add("Broken References", "ERROR" if broken else "INFO",
                   f"{len(broken)} broken edge references", broken)

    def check_duplicate_edges(self):
        seen = set()
        dupes = []
        for r in self.rels:
            key = (r["start_node_id"], r["end_node_id"], r["type"])
            if key in seen:
                dupes.append(f"{r['type']}: {r['start_node_id']} -> {r['end_node_id']}")
            seen.add(key)
        self._add("Duplicate Edges", "WARNING" if dupes else "INFO",
                   f"{len(dupes)} duplicate relationships", dupes)

    def check_taxonomic_hierarchy(self):
        """Ensure: Class->Phylum, Order->Class, Genus->Order, Species->Genus."""
        checks = [
            ("Class", "BELONGS_TO_PHYLUM", "Classes without phylum"),
            ("Order", "BELONGS_TO_CLASS", "Orders without class"),
            ("Genus", "BELONGS_TO_ORDER", "Genera without order"),
            ("Species", "BELONGS_TO_GENUS", "Species without genus"),
        ]
        for label, rel_type, desc in checks:
            entities = [n for n in self.nodes.values() if n["label"] == label]
            linked = set(r["start_node_id"] for r in self.rels if r["type"] == rel_type)
            missing = [
                f"{n['properties'].get('name', n['id'])}"
                for n in entities
                if n["id"] not in linked
            ]
            sev = "WARNING" if missing else "INFO"
            # Anguillospora is expected to have no order (polyphyletic)
            if label == "Genus" and len(missing) == 1 and "Anguillospora" in missing[0]:
                sev = "INFO"
            self._add("Taxonomic Hierarchy", sev, f"{desc}: {len(missing)}", missing)

    def check_genus_morphotypes(self):
        genera = [n for n in self.nodes.values() if n["label"] == "Genus"]
        linked = set(r["start_node_id"] for r in self.rels if r["type"] == "HAS_MORPHOTYPE")
        missing = [g["properties"]["name"] for g in genera if g["id"] not in linked]
        self._add("Morphotype Coverage", "WARNING" if missing else "INFO",
                   f"{len(missing)} genera without morphotype", missing)

    def check_species_geography(self):
        species = [n for n in self.nodes.values() if n["label"] == "Species"]
        linked = set(r["start_node_id"] for r in self.rels if r["type"] == "OCCURS_IN")
        missing = [s["properties"]["name"] for s in species if s["id"] not in linked]
        self._add("Geographic Distribution", "WARNING" if missing else "INFO",
                   f"{len(missing)} species without distribution", missing)

    def compute_connectivity(self):
        degree = Counter()
        for r in self.rels:
            degree[r["start_node_id"]] += 1
            degree[r["end_node_id"]] += 1
        avg_deg = sum(degree.values()) / max(len(degree), 1)
        max_node = max(degree, key=degree.get) if degree else None
        max_name = self.nodes[max_node]["properties"]["name"] if max_node else "N/A"
        self._add("Connectivity", "INFO",
                   f"Avg degree: {avg_deg:.1f}, Max: {degree.get(max_node, 0)} ({max_name})",
                   [f"{self.nodes[n]['properties']['name']} ({self.nodes[n]['label']}): {d}"
                    for n, d in degree.most_common(10)])

    def run_all(self) -> list[AuditResult]:
        self.check_orphan_nodes()
        self.check_broken_references()
        self.check_duplicate_edges()
        self.check_taxonomic_hierarchy()
        self.check_genus_morphotypes()
        self.check_species_geography()
        self.compute_connectivity()
        return self.results

    def print_report(self):
        table = Table(title="Graph Coherence Audit", show_lines=True)
        table.add_column("Category", style="bold")
        table.add_column("Severity")
        table.add_column("Summary")
        table.add_column("Details", max_width=50)

        sev_style = {"ERROR": "red bold", "WARNING": "yellow", "INFO": "green"}
        errors = 0
        warnings = 0

        for r in self.results:
            style = sev_style.get(r.severity, "")
            details = "\n".join(r.items[:5]) if r.items else "—"
            if len(r.items) > 5:
                details += f"\n... +{len(r.items)-5} more"
            table.add_row(r.category, f"[{style}]{r.severity}[/]", r.message, details)
            if r.severity == "ERROR":
                errors += sum(1 for _ in r.items) if r.items else 1
            elif r.severity == "WARNING":
                warnings += sum(1 for _ in r.items) if r.items else 1

        console.print(table)
        console.print(f"\n[bold]Summary:[/] {len(self.data['nodes'])} nodes, "
                       f"{len(self.rels)} relationships, "
                       f"{len(set(n['label'] for n in self.data['nodes']))} label types")
        if errors:
            console.print(f"[red bold]✗ {errors} errors require fixing[/]")
        elif warnings:
            console.print(f"[yellow]⚠ {warnings} warnings (review recommended)[/]")
        else:
            console.print("[green bold]✓ Graph is fully coherent[/]")

        return errors == 0


@click.command()
@click.option("--json", "json_path", required=True, help="Path to graph JSON")
@click.option("--strict", is_flag=True, help="Exit with code 1 on any warning")
def main(json_path: str, strict: bool):
    validator = GraphValidator(json_path)
    validator.run_all()
    passed = validator.print_report()

    if not passed:
        sys.exit(1)
    if strict:
        has_warnings = any(r.severity == "WARNING" for r in validator.results)
        if has_warnings:
            sys.exit(1)


if __name__ == "__main__":
    main()
