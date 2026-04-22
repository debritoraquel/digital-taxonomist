"""Tests for the genus-to-morphotype lookup module."""

from pathlib import Path

import pytest

from preprocessing.morphotype_lookup import MorphotypeLookup

GRAPH_JSON = Path("data/neo4j/aquatic_hyphomycetes_graph.json")


@pytest.fixture
def lookup():
    return MorphotypeLookup(GRAPH_JSON)


class TestMorphotypeLookup:
    def test_loads_from_json(self, lookup):
        assert lookup.genus_count > 0

    def test_all_41_genera_mapped(self, lookup):
        assert lookup.genus_count >= 41

    def test_alatospora_is_stauroid(self, lookup):
        assert lookup.get_morphotype("Alatospora") == "stauroid"

    def test_tetracladium_is_stauroid(self, lookup):
        assert lookup.get_morphotype("Tetracladium") == "stauroid"

    def test_anguillospora_is_scolecoid(self, lookup):
        assert lookup.get_morphotype("Anguillospora") == "scolecoid"

    def test_lunulospora_is_scolecoid(self, lookup):
        assert lookup.get_morphotype("Lunulospora") == "scolecoid"

    def test_campylospora_is_appendaged(self, lookup):
        assert lookup.get_morphotype("Campylospora") == "appendaged"

    def test_heliscella_is_helicoid(self, lookup):
        assert lookup.get_morphotype("Heliscella") == "helicoid"

    def test_clavariopsis_is_clavate(self, lookup):
        assert lookup.get_morphotype("Clavariopsis") == "clavate"

    def test_unknown_genus(self, lookup):
        assert lookup.get_morphotype("NonexistentGenus") == "unknown"

    def test_case_insensitive(self, lookup):
        assert lookup.get_morphotype("alatospora") == "stauroid"
        assert lookup.get_morphotype("ALATOSPORA") == "stauroid"
        assert lookup.get_morphotype("Alatospora") == "stauroid"

    def test_morphotype_classes_constant(self, lookup):
        assert "stauroid" in lookup.MORPHOTYPE_CLASSES
        assert "scolecoid" in lookup.MORPHOTYPE_CLASSES
        assert "appendaged" in lookup.MORPHOTYPE_CLASSES
        assert "helicoid" in lookup.MORPHOTYPE_CLASSES
        assert "clavate" in lookup.MORPHOTYPE_CLASSES
        assert len(lookup.MORPHOTYPE_CLASSES) == 5

    def test_all_morphotypes_includes_unknown(self, lookup):
        assert "unknown" in lookup.all_morphotypes
        assert len(lookup.all_morphotypes) == 6

    def test_get_genera_for_stauroid(self, lookup):
        genera = lookup.get_all_genera_for_morphotype("stauroid")
        assert len(genera) >= 7
        assert "Alatospora" in genera

    def test_get_genera_for_nonexistent(self, lookup):
        assert lookup.get_all_genera_for_morphotype("fake") == []
