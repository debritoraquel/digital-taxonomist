# Graph Schema & Methodology

## Ontological Model (v1.2 — com pesos ecológicos)

The knowledge graph encodes the complete taxonomic, morphological, and ecological
knowledge about aquatic hyphomycetes (Ingoldian fungi) needed for computer vision
classification and biomonitoring decision support.

**v1.2 additions:** ecological weights on all relationships, `BiomonitoringIndex`
node type, `INDICATES_WATER_QUALITY` relationships, and Bayesian decision priors
on `EcologicalGroup` nodes.

### Node Types (16 labels, 119 nodes)

| Label | Count | Role in Pipeline |
|-------|-------|-----------------|
| **Phylum** | 2 | Root of taxonomic hierarchy |
| **Class** | 6 | Molecular phylogenetic placement |
| **Order** | 5 | Dominant: Helotiales (37 connections) |
| **Genus** | 41 | Primary classification target for CV |
| **Species** | 15 | Key species with Brazilian distribution |
| **ConidialMorphotype** | 5 | Core visual categories for CV |
| **MorphologicalAttribute** | 12 | Measurable features extracted by CV |
| **Conidiogenesis** | 4 | Spore formation mode |
| **EcologicalGroup** | 4 | Ecological classification + Bayesian priors |
| **Habitat** | 4 | Physical environment types |
| **Substrate** | 4 | Growth substrates |
| **GeographicRegion** | 6 | Distribution data (4 Brazilian biomes) |
| **EnzymaticActivity** | 5 | Functional ecology |
| **BiomonitoringIndex** | 3 | Water quality indices (IQRH, Shannon, TEI) |
| **CVArchitecture** | 2 | ML model components |
| **CVPipeline** | 1 | Preprocessing pipeline |

### Relationship Types (23 types, 222 edges — com pesos)

**Taxonomic hierarchy:**
- `BELONGS_TO_PHYLUM` — Class → Phylum
- `BELONGS_TO_CLASS` — Order → Class
- `BELONGS_TO_ORDER` — Genus → Order (with `placement_status` property for incertae sedis)
- `BELONGS_TO_GENUS` — Species → Genus

**Morphological:**
- `HAS_MORPHOTYPE` — Genus → ConidialMorphotype
- `MEASURED_BY` — ConidialMorphotype → MorphologicalAttribute
- `USES_CONIDIOGENESIS` — Genus → Conidiogenesis

**Ecological (com pesos para inferência):**
- `CLASSIFIED_AS` — Genus → EcologicalGroup · props: `affinity_weight` [0-1], `decision_weight`
- `INHABITS` — EcologicalGroup → Habitat · props: `frequency_weight` [0-1], `seasonality`
- `FOUND_ON` — EcologicalGroup → Substrate · props: `preference_score` [0-1], `decomposition_stage_preference`
- `EXTENDS_TO` — EcologicalGroup → Habitat (rare/marginal)
- `PRODUCES_ENZYME` — EcologicalGroup → EnzymaticActivity · props: `activity_level`, `activity_score` [0-1]

**Biogeographic:**
- `OCCURS_IN` — Species → GeographicRegion · props: `occurrence_probability` [0-1], `collection_records`
- `CHARACTERISTIC_OF` — EcologicalGroup → GeographicRegion

**Biomonitoring (novos em v1.2):**
- `INDICATES_WATER_QUALITY` — EcologicalGroup → BiomonitoringIndex · props: `indicator_weight` [0-1], `direction`

**Phylogenetic:**
- `POLYPHYLETIC_IN` — Genus → Class (for polyphyletic genera)
- `INCLUDES_AQUATIC_IN` — Phylum → EcologicalGroup

**CV Integration:**
- `TARGETS_CLASSIFICATION_OF` — CVArchitecture → ConidialMorphotype
- `INTEGRATES_WITH` — CVArchitecture → CVArchitecture
- `FEEDS_INTO` — CVPipeline → CVArchitecture

## Ecological Weight Schema

Weights enable probabilistic inference at every step of the identification pipeline:

| Relationship | Weight Property | Inference Use |
|---|---|---|
| `CLASSIFIED_AS` | `affinity_weight` | P(eco_group \| genus) — prior ecológico |
| `INHABITS` | `frequency_weight` | P(habitat \| eco_group) — verossimilhança de habitat |
| `FOUND_ON` | `preference_score` | P(substrate \| eco_group) — verossimilhança de substrato |
| `PRODUCES_ENZYME` | `activity_score` | P(enzyme \| eco_group) — capacidade funcional |
| `OCCURS_IN` | `occurrence_probability` | P(region \| species) — prior geográfico |
| `INDICATES_WATER_QUALITY` | `indicator_weight` | contribuição ao índice de qualidade |

### EcologicalGroup Decision Priors

Each `EcologicalGroup` carries a `decision_prior` — the prior probability that a
randomly collected aquatic hyphomycete from a subtropical Brazilian stream belongs
to this group:

| Group | Prior | Rationale |
|---|---|---|
| Ingoldian | 0.60 | Dominant group in lotic environments |
| Aero-aquatic | 0.20 | Common in lentic/mixed habitats |
| Submerged-aquatic | 0.10 | Specialist on woody debris |
| Terrestrial-aquatic | 0.10 | Rare in pure aquatic samples |

### BiomonitoringIndex Nodes

Three indices are encoded for water quality decision support:

| ID | Name | Scale | decision_weight |
|---|---|---|---|
| `idx_iqrh` | IQRH — Índice de Qualidade por Hifomicetos | 0-10 | 0.90 |
| `idx_shannon_hifi` | H' Shannon-Wiener (Hifomicetos) | 0-4 bits | 0.75 |
| `idx_sporulation` | Taxa de Esporulação Ingoldiana (TEI) | esporos/L/dia | 0.65 |

## Key Design Decisions

### 1. Polyphyly Encoding

Genera like *Anguillospora* are distributed across 3 phylogenetic classes
(Leotiomycetes, Dothideomycetes, Orbiliomycetes). Rather than forcing a
single taxonomic path, the graph uses:

- `POLYPHYLETIC_IN` edges to mark each class affiliation
- *Anguillospora* intentionally has NO `BELONGS_TO_ORDER` edge
- The `GraphAwarePairSampler` uses these edges to generate hard negatives

### 2. Incertae Sedis Placement

16 genera lack confirmed molecular phylogenetic placement. These receive
`BELONGS_TO_ORDER` edges to Helotiales (the dominant order) with a
`placement_status: "incertae_sedis"` property, enabling:

- Graph traversal without dead ends
- Filtering by placement confidence in queries
- Progressive refinement as molecular data becomes available

### 3. Morphotype as CV Bridge

The 5 `ConidialMorphotype` nodes (Stauroid, Scolecoid, Appendaged, Helicoid,
Clavate) serve as the bridge between taxonomy and computer vision:

- Each genus maps to exactly one morphotype
- Each morphotype links to measurable `MorphologicalAttribute` nodes
- The Siamese ViT first classifies morphotype, then refines to genus
- The `MEASURED_BY` edges define which features the CV pipeline must extract

## Coherence Audit Results (v1.1)

```
✓ Orphan nodes:            0
✓ Broken references:       0
✓ Duplicate edges:         0
✓ Genera without order:    1 (Anguillospora — expected, polyphyletic)
✓ Genera without morpho:   0
✓ Species without genus:   0
✓ Species without distrib: 0
```

## Hub Analysis

| Hub Node | Type | Degree | Role |
|----------|------|--------|------|
| Helotiales | Order | 37 | Dominant order — attracts most genera |
| Stauroid | Morphotype | 33 | Most common conidial shape — 15 genera |
| Ingoldian | EcoGroup | 24 | Core ecological classification |
| Scolecoid | Morphotype | 20 | Second most common shape — 9 genera |
| Mata Atlântica | Region | 12 | Highest species richness in Brazil |

## Cypher Query Examples

```cypher
// Get complete taxonomy for a genus
MATCH (g:Genus {name: 'Tetracladium'})
  -[:BELONGS_TO_ORDER]->(o:Order)
  -[:BELONGS_TO_CLASS]->(c:Class)
  -[:BELONGS_TO_PHYLUM]->(p:Phylum)
MATCH (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN g.name, o.name, c.name, p.name, m.name

// Find hard negatives for training (same morphotype, different genus)
MATCH (g1:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)<-[:HAS_MORPHOTYPE]-(g2:Genus)
WHERE g1.name = 'Tetracladium' AND g1 <> g2
RETURN g2.name AS hard_negative_genus, m.name AS shared_morphotype

// Validate a CV prediction
MATCH (g:Genus {name: $predicted_genus})-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN m.name AS expected_morphotype

// Species richness per Brazilian biome
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
WHERE r.name CONTAINS 'Brasil' OR r.name IN ['Amazônia Brasileira','Mata Atlântica','Caatinga','Cerrado']
RETURN r.name, count(s) AS species_count ORDER BY species_count DESC
```
