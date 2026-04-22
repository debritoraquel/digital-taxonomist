"""
Genus-to-morphotype resolver using the knowledge graph JSON.

Loads the aquatic hyphomycetes graph and builds a lookup from genus
names (e.g. "Alatospora") to conidial morphotype classes (e.g. "stauroid").

Designed for standalone use without a running Neo4j instance — reads
the JSON file directly, following the same pattern as
``GraphAwarePairSampler`` in ``src/cv/siamese_vit.py``.

Usage:
    from preprocessing.morphotype_lookup import MorphotypeLookup

    lookup = MorphotypeLookup()
    lookup.get_morphotype("Alatospora")   # "stauroid"
    lookup.get_morphotype("Unknown")      # "unknown"
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_GRAPH_JSON = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "neo4j" / "aquatic_hyphomycetes_graph.json"
)


class MorphotypeLookup:
    """Maps genus names to conidial morphotype categories."""

    MORPHOTYPE_CLASSES: list[str] = [
        "stauroid", "scolecoid", "appendaged", "helicoid", "clavate",
    ]
    UNKNOWN: str = "unknown"

    def __init__(self, graph_json_path: Optional[Path] = None):
        path = Path(graph_json_path) if graph_json_path else _DEFAULT_GRAPH_JSON

        with open(path) as f:
            data = json.load(f)

        # Build node_id -> node name lookup
        id_to_name: dict[str, str] = {}
        for node in data["nodes"]:
            id_to_name[node["id"]] = node["properties"]["name"]

        # Build genus_name(lower) -> morphotype and reverse mapping
        self._genus_to_morphotype: dict[str, str] = {}
        self._morphotype_to_genera: dict[str, list[str]] = {
            m: [] for m in self.MORPHOTYPE_CLASSES
        }

        for rel in data["relationships"]:
            if rel["type"] != "HAS_MORPHOTYPE":
                continue

            genus_id = rel["start_node_id"]
            morphotype_id = rel["end_node_id"]

            genus_name = id_to_name.get(genus_id, "")
            morphotype = morphotype_id.replace("morphotype_", "")

            if genus_name and morphotype in self.MORPHOTYPE_CLASSES:
                self._genus_to_morphotype[genus_name.lower()] = morphotype
                self._morphotype_to_genera[morphotype].append(genus_name)

        logger.debug(
            f"MorphotypeLookup: {len(self._genus_to_morphotype)} genera "
            f"mapped to {len(self.MORPHOTYPE_CLASSES)} morphotypes"
        )

    def get_morphotype(self, genus_name: str) -> str:
        """Return morphotype for *genus_name* (case-insensitive), or ``'unknown'``."""
        return self._genus_to_morphotype.get(genus_name.lower(), self.UNKNOWN)

    def get_all_genera_for_morphotype(self, morphotype: str) -> list[str]:
        """Return all genus names belonging to *morphotype*."""
        return list(self._morphotype_to_genera.get(morphotype, []))

    @property
    def all_morphotypes(self) -> list[str]:
        """The 5 morphotype class names plus ``'unknown'``."""
        return self.MORPHOTYPE_CLASSES + [self.UNKNOWN]

    @property
    def genus_count(self) -> int:
        """Number of genera with a known morphotype mapping."""
        return len(self._genus_to_morphotype)
