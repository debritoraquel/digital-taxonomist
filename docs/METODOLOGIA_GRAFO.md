# Metodologia: Extração de Dados, Construção do Grafo e Consultas Cypher

**Projeto:** Taxonomista Digital — Hifomicetos Aquáticos  
**Grafo:** v1.1.0 · 116 nós · 216 arestas · 15 tipos de rótulo · 21 tipos de relação

---

## 1. Fontes de Dados

O grafo integra literatura taxonômica primária e bancos de dados globais:

| Fonte | Contribuição |
|-------|-------------|
| Ingold 1942 | Descrição seminal dos hifomicetos aquáticos; morfologia estauróide/escolecoide |
| Webster & Descals 1981 | Chaves morfológicas e ecologia de fungos conidiais de água doce |
| Fiuza et al. 2017 (Phytotaxa 306) | Checklist e chave de fungos ingoldianos do Brasil (base dos biomas brasileiros) |
| Fiuza et al. 2022 | Assembleias por bioma: Amazônia, Mata Atlântica, Caatinga, Cerrado, Pampa |
| Baschien et al. 2013 | Filogenia molecular de Leotiomycetes; posicionamento de gêneros polifiléticos |
| Belliveau & Bärlocher 2005 | Evidência molecular para origens múltiplas de morfotipos convergentes |
| Gulis et al. 2022 | Barcodes ITS rDNA; critério de espécie molecular |
| Duarte et al. 2016 | Biogeografia global; regiões neotropicais |
| Luo et al. 2019 | Hifomicetos do Greater Mekong; referência comparativa para biomas tropicais |
| Fiuza et al. 2015 | Primeiros registros da Amazônia brasileira |

---

## 2. Pipeline de Extração de Dados

### 2.1 Extração de PDFs Científicos

```
PDF de artigo científico
        │
        ▼
 PyMuPDF (fitz)                   ← extrai páginas como imagens (150–300 dpi)
        │
        ▼
 Detecção de pranchas (plates)    ← heurística: páginas com densidade alta de
        │                            figuras e baixa densidade de texto
        ▼
 OCR + parsing de legendas        ← pytesseract; regex para binômios "Genus species"
        │
        ▼
 Segmentação de conídios          ← OpenCV: limiarização Otsu, detecção de
        │                            contornos, filtragem por área (100–50 000 px²)
        ▼
 Crop normalizado 224×224 px      ← saída: imagem grayscale, 1 conídio por arquivo
        │
        ▼
 Metadados: genus_species, fonte, página, bbox
```

Scripts relevantes:
- `src/preprocessing/extract_conidia_drawings.py` — `ConidiaDrawingExtractor`
- `extract_training_pdfs_v8.py` — pipeline completo com versão 8
- `executar_extracao.py` — entrada única para execução local

### 2.2 Extração de Metadados Taxonômicos

Para cada gênero foram extraídos manualmente das fontes primárias (Ingold 1942, Fiuza et al. 2017):

```
Para cada gênero G:
  ├── authority (autor, ano)
  ├── type_species (espécie-tipo nomenclatural)
  ├── conidial_shape (descrição morfológica textual)
  ├── conidiogenesis (modo de formação do conídio)
  ├── morphotype (categoria visual: stauroid, scolecoid, appendaged, helicoid, clavate)
  ├── order → class → phylum (hierarquia APG/MycoBank)
  ├── polyphyletic_in (classes filogenéticas múltiplas, se aplicável)
  └── brazil_biomes (via Fiuza et al. 2017 e 2022)
```

### 2.3 Controle de Qualidade

Antes da importação no Neo4j, o validador audita o JSON:

```bash
python src/graph/validate.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --strict
```

Verificações realizadas:

| Check | Critério |
|-------|---------|
| Broken references | Toda aresta aponta para nós existentes |
| Duplicate edges | `(start, end, type)` único |
| Orphan nodes | Todo nó tem ≥ 1 relação |
| Hierarquia taxonômica | Classe→Filo, Ordem→Classe, Gênero→Ordem, Espécie→Gênero |
| Cobertura morfotipo | Todo gênero tem `HAS_MORPHOTYPE` |
| Distribuição geográfica | ≥ 90 % das espécies com `OCCURS_IN` |

A v1.1.0 corrigiu 42 problemas identificados na auditoria da v1.0 (ver `docs/AUDIT_REPORT.md`).

---

## 3. Modelo do Grafo

### 3.1 Esquema de nós

```
Phylum ──────── Ascomycota, Basidiomycota
   └── Class ── Leotiomycetes, Dothideomycetes, Sordariomycetes,
   │            Orbiliomycetes, Basidiomycota_cl, Agaricomycetes
   └── Order ── Helotiales (dominante, 40 conn.), Pleosporales,
                Hypocreales, Xylariales, Agaricales
                   └── Genus (41) ──── Species (15)
                          │
                    ┌─────┴──────┬──────────────────┐
                    ▼            ▼                   ▼
           ConidialMorphotype  Conidiogenesis   EcologicalGroup
           (5 tipos)           (4 tipos)         (4 tipos)
                    │
             MorphologicalAttribute (12)
```

### 3.2 Tipos de relação por camada

**Taxonomia**
```
(Class)-[:BELONGS_TO_PHYLUM]->(Phylum)
(Order)-[:BELONGS_TO_CLASS]->(Class)
(Genus)-[:BELONGS_TO_ORDER]->(Order)
(Species)-[:BELONGS_TO_GENUS]->(Genus)
(Genus)-[:POLYPHYLETIC_IN]->(Class)          // gêneros polifiléticos
(Species)-[:TELEOMORPH_OF]->(Species)        // fase sexual ↔ assexual
```

**Morfologia e classificação**
```
(Genus)-[:HAS_MORPHOTYPE]->(ConidialMorphotype)
(Genus)-[:USES_CONIDIOGENESIS]->(Conidiogenesis)
(ConidialMorphotype)-[:MEASURED_BY]->(MorphologicalAttribute)
(Genus)-[:CLASSIFIED_AS]->(EcologicalGroup)
```

**Ecologia e distribuição**
```
(Species)-[:OCCURS_IN]->(GeographicRegion)
(EcologicalGroup)-[:INHABITS]->(Habitat)
(Genus)-[:FOUND_ON]->(Substrate)
(Genus)-[:PRODUCES_ENZYME]->(EnzymaticActivity)
```

**Pipeline de visão computacional**
```
(CVPipeline)-[:FEEDS_INTO]->(CVArchitecture)
(CVArchitecture)-[:TARGETS_CLASSIFICATION_OF]->(ConidialMorphotype)
(CVArchitecture)-[:INTEGRATES_WITH]->(ConidialMorphotype)
(CVArchitecture)-[:QUERIES_GRAPH]->(Genus)
```

---

## 4. Importação no Neo4j

```bash
# 1. Subir Neo4j via Docker
docker compose up -d

# 2. Aguardar disponibilidade (variáveis via .env ou defaults do docker-compose)
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=taxonomist2026

# 3. Importar grafo
python src/graph/load_graph.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --verify

# 4. Verificar conectividade via Python
python -c "
from graph.connection import check_connectivity
print('Neo4j OK:', check_connectivity())
"
```

---

## 5. Scripts Cypher — Investigando o Grafo

Abra o Neo4j Browser em `http://localhost:7474` (ou use `cypher-shell`) e execute as
consultas abaixo.

---

### 5.1 Visão geral do grafo

```cypher
// Contagem de nós por rótulo
MATCH (n)
RETURN labels(n)[0] AS label, count(n) AS total
ORDER BY total DESC;
```

```cypher
// Contagem de arestas por tipo de relação
MATCH ()-[r]->()
RETURN type(r) AS relacao, count(r) AS total
ORDER BY total DESC;
```

```cypher
// Visualizar o grafo completo (limitado a 150 nós para performance)
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 150;
```

---

### 5.2 Hierarquia taxonômica completa

```cypher
// Caminho completo: Espécie → Gênero → Ordem → Classe → Filo
MATCH path = (s:Species)-[:BELONGS_TO_GENUS]->(g:Genus)
             -[:BELONGS_TO_ORDER]->(o:Order)
             -[:BELONGS_TO_CLASS]->(c:Class)
             -[:BELONGS_TO_PHYLUM]->(p:Phylum)
RETURN s.name   AS especie,
       g.name   AS genero,
       o.name   AS ordem,
       c.name   AS classe,
       p.name   AS filo
ORDER BY filo, classe, ordem, genero, especie;
```

```cypher
// Quantos gêneros por ordem?
MATCH (g:Genus)-[:BELONGS_TO_ORDER]->(o:Order)
RETURN o.name AS ordem, count(g) AS n_generos
ORDER BY n_generos DESC;
```

```cypher
// Qual ordem domina qual morfotipo?
MATCH (g:Genus)-[:BELONGS_TO_ORDER]->(o:Order),
      (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN o.name AS ordem, m.name AS morfotipo, count(g) AS n_generos
ORDER BY ordem, n_generos DESC;
```

---

### 5.3 Morfotipos conidiais

```cypher
// Todos os gêneros por morfotipo (base do pipeline de CV)
MATCH (g:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN m.name AS morfotipo,
       collect(g.name) AS generos,
       count(g) AS total
ORDER BY total DESC;
```

```cypher
// Atributos morfológicos medidos por morfotipo
MATCH (m:ConidialMorphotype)-[:MEASURED_BY]->(a:MorphologicalAttribute)
RETURN m.name AS morfotipo,
       collect(a.name) AS atributos;
```

```cypher
// Gêneros do morfotipo estauróide com suas ordens e conidiogênese
MATCH (g:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype {name: "Estauróide (Stauroid)"}),
      (g)-[:BELONGS_TO_ORDER]->(o:Order)
OPTIONAL MATCH (g)-[:USES_CONIDIOGENESIS]->(c:Conidiogenesis)
RETURN g.name AS genero, o.name AS ordem, c.name AS conidiogenese
ORDER BY ordem, genero;
```

---

### 5.4 Polifiletismo e desafios de classificação

```cypher
// Gêneros polifiléticos: em quantas classes distintas aparecem?
MATCH (g:Genus)-[:POLYPHYLETIC_IN]->(c:Class)
RETURN g.name AS genero,
       count(c) AS n_classes,
       collect(c.name) AS classes
ORDER BY n_classes DESC;
```

```cypher
// Por que Anguillospora é o caso mais complexo?
// Todas as relações de Anguillospora no grafo
MATCH (g:Genus {name: "Anguillospora"})-[r]->(target)
RETURN type(r) AS relacao, labels(target)[0] AS tipo_alvo, target.name AS alvo
UNION
MATCH (source)-[r]->(g:Genus {name: "Anguillospora"})
RETURN type(r) AS relacao, labels(source)[0] AS tipo_fonte, source.name AS fonte;
```

```cypher
// Comparar morfotipo de Tricladium com sua posição filogenética
MATCH (g:Genus {name: "Tricladium"})
OPTIONAL MATCH (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
OPTIONAL MATCH (g)-[:BELONGS_TO_ORDER]->(o:Order)-[:BELONGS_TO_CLASS]->(c:Class)
OPTIONAL MATCH (g)-[:POLYPHYLETIC_IN]->(cp:Class)
RETURN g.name, m.name AS morfotipo, o.name AS ordem,
       c.name AS classe_hierarquica,
       collect(DISTINCT cp.name) AS classes_polifileticas;
```

---

### 5.5 Distribuição geográfica (biomas brasileiros)

```cypher
// Quais espécies ocorrem em cada bioma?
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
RETURN r.name AS bioma,
       count(s) AS n_especies,
       collect(s.name) AS especies
ORDER BY n_especies DESC;
```

```cypher
// Espécies cosmopolitas presentes em todos os 4 biomas principais
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
WHERE r.name IN ["Amazônia", "Mata Atlântica", "Caatinga", "Cerrado"]
WITH s, collect(r.name) AS biomas
WHERE size(biomas) = 4
RETURN s.name AS especie, biomas;
```

```cypher
// Espécies endêmicas de um único bioma
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
WITH s, collect(r.name) AS biomas
WHERE size(biomas) = 1
RETURN s.name AS especie, biomas[0] AS bioma_exclusivo;
```

```cypher
// Gêneros por bioma (via espécies filhas)
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion),
      (s)-[:BELONGS_TO_GENUS]->(g:Genus)
RETURN r.name AS bioma, count(DISTINCT g) AS n_generos,
       collect(DISTINCT g.name) AS generos
ORDER BY n_generos DESC;
```

---

### 5.6 Ecologia funcional

```cypher
// Qual grupo ecológico habita qual tipo de ambiente?
MATCH (eg:EcologicalGroup)-[:INHABITS]->(h:Habitat)
RETURN eg.name AS grupo_ecologico, collect(h.name) AS habitats;
```

```cypher
// Quais gêneros produzem enzimas ligninolíticas ou celulolíticas?
MATCH (g:Genus)-[:PRODUCES_ENZYME]->(e:EnzymaticActivity)
RETURN e.name AS enzima, collect(g.name) AS generos
ORDER BY size(collect(g.name)) DESC;
```

```cypher
// Gêneros encontrados em espuma de riacho (método de coleta por foam trapping)
MATCH (g:Genus)-[:FOUND_ON]->(s:Substrate)
WHERE toLower(s.name) CONTAINS "espuma"
RETURN g.name AS genero, s.name AS substrato;
```

```cypher
// Substrato → gênero → morfotipo (cadeia ecologia-morfologia)
MATCH (g:Genus)-[:FOUND_ON]->(sub:Substrate),
      (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN sub.name AS substrato, m.name AS morfotipo,
       count(g) AS n_generos, collect(g.name) AS generos
ORDER BY substrato, n_generos DESC;
```

---

### 5.7 Validação de predições do modelo de CV

```cypher
// Verificar se um gênero predito é consistente com o morfotipo observado
// (substitua "Alatospora" e "stauroid" pelos valores reais da predição)
MATCH (g:Genus {name: "Alatospora"})-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN g.name AS genero_predito,
       m.name AS morfotipo_esperado,
       toLower(m.name) CONTAINS "stauroid" AS consistente;
```

```cypher
// Obter caminho taxonômico completo para um gênero predito
// (usado no módulo QueryEngine.get_full_taxonomy)
MATCH (g:Genus {name: "Tetracladium"})
OPTIONAL MATCH (g)-[:BELONGS_TO_ORDER]->(o:Order)
OPTIONAL MATCH (o)-[:BELONGS_TO_CLASS]->(c:Class)
OPTIONAL MATCH (c)-[:BELONGS_TO_PHYLUM]->(p:Phylum)
OPTIONAL MATCH (g)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
OPTIONAL MATCH (g)-[:USES_CONIDIOGENESIS]->(cg:Conidiogenesis)
RETURN g.name   AS genero,
       o.name   AS ordem,
       c.name   AS classe,
       p.name   AS filo,
       m.name   AS morfotipo,
       cg.name  AS conidiogenese;
```

```cypher
// Listar todos os gêneros do mesmo morfotipo que o predito
// (candidatos alternativos para classificação ambígua)
MATCH (g_pred:Genus {name: "Alatospora"})-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype),
      (g_alt:Genus)-[:HAS_MORPHOTYPE]->(m)
WHERE g_alt <> g_pred
RETURN m.name AS morfotipo, collect(g_alt.name) AS generos_alternativos;
```

---

### 5.8 Análise estrutural do grafo

```cypher
// Grau de cada nó (in + out): quem é mais central?
MATCH (n)
OPTIONAL MATCH (n)-[out]->()
OPTIONAL MATCH ()-[in]->(n)
RETURN labels(n)[0] AS tipo, n.name AS nome,
       count(DISTINCT out) AS saida,
       count(DISTINCT in)  AS entrada,
       count(DISTINCT out) + count(DISTINCT in) AS grau_total
ORDER BY grau_total DESC
LIMIT 20;
```

```cypher
// Nós com grau zero (órfãos — não deve existir na v1.1.0)
MATCH (n)
WHERE NOT (n)--()
RETURN labels(n)[0] AS tipo, n.name AS nome;
```

```cypher
// Caminhos mais curtos entre dois gêneros morfologicamente distantes
MATCH (a:Genus {name: "Alatospora"}),
      (b:Genus {name: "Lunulospora"}),
      path = shortestPath((a)-[*]-(b))
RETURN [n IN nodes(path) | coalesce(n.name, labels(n)[0])] AS caminho,
       length(path) AS distancia;
```

```cypher
// Centralidade de morfotipo: qual morfotipo conecta mais gêneros a mais biomas?
MATCH (m:ConidialMorphotype)<-[:HAS_MORPHOTYPE]-(g:Genus),
      (s:Species)-[:BELONGS_TO_GENUS]->(g),
      (s)-[:OCCURS_IN]->(r:GeographicRegion)
RETURN m.name AS morfotipo,
       count(DISTINCT g) AS n_generos,
       count(DISTINCT r) AS n_regioes,
       count(DISTINCT g) * count(DISTINCT r) AS score_centralidade
ORDER BY score_centralidade DESC;
```

---

### 5.9 Consultas para exportação / treinamento do modelo

```cypher
// Exportar pares (imagem_label, morfotipo) para treino supervisionado
// (retorna apenas gêneros com morfotipo definido)
MATCH (g:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)
RETURN g.name AS genus_label,
       m.name AS morphotype_label
ORDER BY m.name, g.name;
```

```cypher
// Identificar pares difíceis para treino contrastivo
// (mesmo morfotipo, ordens diferentes → negativos difíceis)
MATCH (g1:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype)<-[:HAS_MORPHOTYPE]-(g2:Genus),
      (g1)-[:BELONGS_TO_ORDER]->(o1:Order),
      (g2)-[:BELONGS_TO_ORDER]->(o2:Order)
WHERE g1.name < g2.name AND o1 <> o2
RETURN g1.name AS genero_a, g2.name AS genero_b,
       m.name AS morfotipo_compartilhado,
       o1.name AS ordem_a, o2.name AS ordem_b
ORDER BY morfotipo_compartilhado;
```

---

## 6. Fluxo de Validação Integrado (Grafo + Modelo)

```
Imagem TIFF
    │
    ▼
SpecimenSegmenter          → crops de conídios individuais
    │
    ▼
SiameseViT.predict()       → genus_pred, confidence, embedding
    │
    ▼
QueryEngine.validate_prediction(genus_pred, observed_morphotype)
    │          └── Cypher: MATCH (g)-[:HAS_MORPHOTYPE]->(m)
    │                      WHERE g.name = genus_pred
    │
    ├─ valid=True  → aceitar predição, buscar caminho taxonômico
    └─ valid=False → rejeitar ou sinalizar revisão manual
              │
              ▼
    QueryEngine.get_full_taxonomy(genus_pred)
              │
              ▼
    Relatório: espécie candidata, distribuição no Brasil,
               morfotipo esperado, enzimas, habitats
```

---

## 7. Extensões Futuras do Grafo

| Extensão | Nós / Arestas Adicionais | Fonte |
|----------|--------------------------|-------|
| Sequências ITS rDNA | `Sequence` → `BARCODE_OF` → Genus | GenBank / Gulis et al. 2022 |
| Imagens de referência | `ReferenceImage` → `EXEMPLAR_OF` → Species | coleção local |
| Métricas morfométricas | propriedades numéricas em `MorphologicalAttribute` | medições via CV |
| Novos biomas | `GeographicRegion`: Pampa, Pantanal | Fiuza et al. 2022 |
| Hifomicetos marinhos | novos `Genus` + `Habitat: marine` | literatura especializada |
| Co-ocorrências | `(Genus)-[:CO_OCCURS_WITH]->(Genus)` | dados de campo |
