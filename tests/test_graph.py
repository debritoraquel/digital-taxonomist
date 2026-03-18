"""Tests for graph coherence and data integrity."""

import json
from pathlib import Path

import pytest

GRAPH_JSON = Path("data/neo4j/aquatic_hyphomycetes_graph.json")


@pytest.fixture
def graph_data():
    with open(GRAPH_JSON) as f:
        return json.load(f)


@pytest.fixture
def nodes(graph_data):
    return {n["id"]: n for n in graph_data["nodes"]}


@pytest.fixture
def rels(graph_data):
    return graph_data["relationships"]


class TestGraphStructure:
    def test_json_loads(self, graph_data):
        assert "nodes" in graph_data
        assert "relationships" in graph_data
        assert "metadata" in graph_data

    def test_node_count(self, graph_data):
        assert len(graph_data["nodes"]) >= 100

    def test_relationship_count(self, graph_data):
        assert len(graph_data["relationships"]) >= 200

    def test_no_broken_references(self, nodes, rels):
        for r in rels:
            assert r["start_node_id"] in nodes, f"Missing start: {r['start_node_id']}"
            assert r["end_node_id"] in nodes, f"Missing end: {r['end_node_id']}"

    def test_no_duplicate_edges(self, rels):
        seen = set()
        for r in rels:
            key = (r["start_node_id"], r["end_node_id"], r["type"])
            assert key not in seen, f"Duplicate: {key}"
            seen.add(key)

    def test_all_nodes_have_required_fields(self, graph_data):
        for n in graph_data["nodes"]:
            assert "id" in n
            assert "label" in n
            assert "properties" in n
            assert "name" in n["properties"]


class TestTaxonomicCoherence:
    def test_classes_have_phylum(self, nodes, rels):
        classes = [n for n in nodes.values() if n["label"] == "Class"]
        linked = {r["start_node_id"] for r in rels if r["type"] == "BELONGS_TO_PHYLUM"}
        for c in classes:
            assert c["id"] in linked, f"Class {c['properties']['name']} has no phylum"

    def test_orders_have_class(self, nodes, rels):
        orders = [n for n in nodes.values() if n["label"] == "Order"]
        linked = {r["start_node_id"] for r in rels if r["type"] == "BELONGS_TO_CLASS"}
        for o in orders:
            assert o["id"] in linked, f"Order {o['properties']['name']} has no class"

    def test_species_have_genus(self, nodes, rels):
        species = [n for n in nodes.values() if n["label"] == "Species"]
        linked = {r["start_node_id"] for r in rels if r["type"] == "BELONGS_TO_GENUS"}
        for s in species:
            assert s["id"] in linked, f"Species {s['properties']['name']} has no genus"

    def test_genera_have_morphotype(self, nodes, rels):
        genera = [n for n in nodes.values() if n["label"] == "Genus"]
        linked = {r["start_node_id"] for r in rels if r["type"] == "HAS_MORPHOTYPE"}
        for g in genera:
            assert g["id"] in linked, f"Genus {g['properties']['name']} has no morphotype"

    def test_at_least_5_morphotypes(self, nodes):
        morphotypes = [n for n in nodes.values() if n["label"] == "ConidialMorphotype"]
        assert len(morphotypes) >= 5

    def test_anguillospora_is_polyphyletic(self, rels):
        polyphyletic = [
            r for r in rels
            if r["type"] == "POLYPHYLETIC_IN" and "anguillospora" in r["start_node_id"]
        ]
        assert len(polyphyletic) >= 2, "Anguillospora should be in at least 2 classes"


class TestBrazilianDistribution:
    def test_brazil_regions_exist(self, nodes):
        regions = [n["properties"]["name"] for n in nodes.values() if n["label"] == "GeographicRegion"]
        for expected in ["Amazônia", "Mata Atlântica", "Caatinga", "Cerrado"]:
            assert any(expected in r for r in regions), f"Missing region: {expected}"

    def test_species_have_distribution(self, nodes, rels):
        species = [n for n in nodes.values() if n["label"] == "Species"]
        linked = {r["start_node_id"] for r in rels if r["type"] == "OCCURS_IN"}
        coverage = sum(1 for s in species if s["id"] in linked) / len(species)
        assert coverage >= 0.9, f"Only {coverage:.0%} of species have distribution data"


class TestCVIntegration:
    def test_cv_architecture_nodes_exist(self, nodes):
        cv_nodes = [n for n in nodes.values() if n["label"] in ("CVArchitecture", "CVPipeline")]
        assert len(cv_nodes) >= 2

    def test_cv_connects_to_morphotypes(self, rels):
        cv_morpho = [r for r in rels if r["type"] == "TARGETS_CLASSIFICATION_OF"]
        assert len(cv_morpho) >= 1
