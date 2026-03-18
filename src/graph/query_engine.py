"""
Cypher query templates for the aquatic hyphomycetes knowledge graph.

Provides pre-built queries for taxonomy lookups, morphotype validation,
and graph-guided classification support.
"""

from dataclasses import dataclass
from typing import Optional

from neo4j import GraphDatabase


@dataclass
class TaxonomicPath:
    species: Optional[str] = None
    genus: Optional[str] = None
    order: Optional[str] = None
    class_name: Optional[str] = None
    phylum: Optional[str] = None
    morphotype: Optional[str] = None
    conidiogenesis: Optional[str] = None


class QueryEngine:
    """Pre-built Cypher queries for the knowledge graph."""

    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def get_full_taxonomy(self, genus_name: str) -> TaxonomicPath:
        """Get complete taxonomic path for a genus."""
        query = """
        MATCH (g:Genus {name: $name})
        OPTIONAL MATCH (g)-[:BELONGS_TO_ORDER]->(o:Order)
        OPTIONAL MATCH (o)-[:BELONGS_TO_CLASS]->(c:Class)
        OPTIONAL MATCH (c)-[:BELONGS_TO_PHYLUM]->(p:Phylum)
        OPTIONAL MATCH (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
        OPTIONAL MATCH (g)-[:USES_CONIDIOGENESIS]->(cg:Conidiogenesis)
        RETURN g.name AS genus, o.name AS order_name, c.name AS class_name,
               p.name AS phylum, m.name AS morphotype, cg.name AS conidiogenesis
        """
        with self.driver.session() as session:
            result = session.run(query, name=genus_name).single()
            if result:
                return TaxonomicPath(
                    genus=result["genus"],
                    order=result["order_name"],
                    class_name=result["class_name"],
                    phylum=result["phylum"],
                    morphotype=result["morphotype"],
                    conidiogenesis=result["conidiogenesis"],
                )
        return TaxonomicPath()

    def get_genera_by_morphotype(self, morphotype_name: str) -> list[str]:
        """Get all genera with a given morphotype (for pair sampling)."""
        query = """
        MATCH (g:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
        WHERE toLower(m.name) CONTAINS toLower($name)
        RETURN g.name AS genus ORDER BY genus
        """
        with self.driver.session() as session:
            return [r["genus"] for r in session.run(query, name=morphotype_name)]

    def validate_prediction(
        self, predicted_genus: str, observed_morphotype: str
    ) -> dict:
        """
        Validate a CV prediction against the knowledge graph.
        Returns consistency score and reasoning.
        """
        query = """
        MATCH (g:Genus {name: $genus})-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
        RETURN m.name AS expected_morphotype
        """
        with self.driver.session() as session:
            result = session.run(query, genus=predicted_genus).single()
            if not result:
                return {"valid": False, "reason": f"Genus '{predicted_genus}' not found in graph"}

            expected = result["expected_morphotype"].lower()
            observed_lower = observed_morphotype.lower()
            is_consistent = observed_lower in expected or expected in observed_lower

            return {
                "valid": is_consistent,
                "predicted_genus": predicted_genus,
                "expected_morphotype": result["expected_morphotype"],
                "observed_morphotype": observed_morphotype,
                "reason": "Consistent" if is_consistent else
                          f"Mismatch: {predicted_genus} expects {result['expected_morphotype']}, got {observed_morphotype}",
            }

    def get_polyphyletic_genera(self) -> list[dict]:
        """Get genera with POLYPHYLETIC_IN relationships (classification challenges)."""
        query = """
        MATCH (g:Genus)-[:POLYPHYLETIC_IN]->(c:Class)
        RETURN g.name AS genus, collect(c.name) AS classes
        """
        with self.driver.session() as session:
            return [dict(r) for r in session.run(query)]

    def get_species_distribution(self, region_name: str) -> list[dict]:
        """Get all species occurring in a geographic region."""
        query = """
        MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
        WHERE toLower(r.name) CONTAINS toLower($region)
        MATCH (s)-[:BELONGS_TO_GENUS]->(g:Genus)
        RETURN s.name AS species, g.name AS genus
        ORDER BY genus, species
        """
        with self.driver.session() as session:
            return [dict(r) for r in session.run(query, region=region_name)]

    def get_morphological_attributes(self, morphotype_name: str) -> list[dict]:
        """Get all morphological attributes measured for a morphotype."""
        query = """
        MATCH (m:ConidialMorphotype)-[:MEASURED_BY]->(a:MorphologicalAttribute)
        WHERE toLower(m.name) CONTAINS toLower($name)
        RETURN a.name AS attribute, a.properties AS props
        """
        with self.driver.session() as session:
            return [dict(r) for r in session.run(query, name=morphotype_name)]

    def graph_stats(self) -> dict:
        """Return graph-wide statistics."""
        queries = {
            "nodes": "MATCH (n) RETURN count(n) AS c",
            "relationships": "MATCH ()-[r]->() RETURN count(r) AS c",
            "genera": "MATCH (g:Genus) RETURN count(g) AS c",
            "species": "MATCH (s:Species) RETURN count(s) AS c",
            "morphotypes": "MATCH (m:ConidialMorphotype) RETURN count(m) AS c",
        }
        stats = {}
        with self.driver.session() as session:
            for key, query in queries.items():
                stats[key] = session.run(query).single()["c"]
        return stats
