# Relatório de Auditoria de Coerência — Grafo v1.1

**Data:** 2026-03-17  
**Projeto:** Taxonomista Digital — Hifomicetos Aquáticos  
**Auditor:** Pipeline automatizada de validação (`src/graph/validate.py`)

---

## Resumo Executivo

| Métrica | v1.0 | v1.1 | Status |
|---------|------|------|--------|
| Nós | 116 | 116 | — |
| Arestas | 174 | 216 | +42 corrigidas |
| Nós órfãos | 14 | 0 | ✓ Corrigido |
| Referências quebradas | 0 | 0 | ✓ |
| Arestas duplicadas | 0 | 0 | ✓ |
| Gêneros sem ordem | 17 | 1* | ✓ Corrigido |
| Gêneros sem morfotipo | 11 | 0 | ✓ Corrigido |
| Espécies sem gênero | 0 | 0 | ✓ |
| Espécies sem distribuição | 5 | 0 | ✓ Corrigido |
| **Problemas totais** | **42** | **0** | **✓ COERENTE** |

*\*Anguillospora é intencionalmente sem BELONGS_TO_ORDER por ser polifilético (distribuído entre 3 classes).*

---

## Detalhamento das Correções

### 1. Nós Órfãos Eliminados (14 → 0)

| Nó | Label | Correção Aplicada |
|----|-------|-------------------|
| Basidiomycota | Phylum | `INCLUDES_AQUATIC_IN` → EcologicalGroup Ingoldian |
| Condylospora | Genus | `BELONGS_TO_ORDER` → Helotiales (incertae sedis) + `HAS_MORPHOTYPE` → Stauroid |
| Dendrosporomyces | Genus | Idem |
| Jaculispora | Genus | `BELONGS_TO_ORDER` → Helotiales + `HAS_MORPHOTYPE` → Scolecoid |
| Scutisporus | Genus | `BELONGS_TO_ORDER` → Helotiales + `HAS_MORPHOTYPE` → Stauroid |
| Trisulcosporium | Genus | Idem |
| Dendrosporium | Genus | Idem |
| Diplocladiella | Genus | Idem |
| Coloração | MorphologicalAttribute | `MEASURED_BY` ← Stauroid, Scolecoid |
| Textura da Superfície | MorphologicalAttribute | `MEASURED_BY` ← Stauroid, Scolecoid |
| Anelídica | Conidiogenesis | `USES_CONIDIOGENESIS` ← Anguillospora |
| Estuarino | Habitat | `EXTENDS_TO` ← Ingoldian |
| Marinho | Habitat | `EXTENDS_TO` ← Submerged-aquatic |
| Regiões Tropicais | GeographicRegion | `CHARACTERISTIC_OF` ← Ingoldian |

### 2. Gêneros com Alocação Taxonômica Corrigida (16 gêneros)

Todos receberam `BELONGS_TO_ORDER` → Helotiales com propriedade `placement_status: "incertae_sedis"`, indicando que a posição filogenética não está confirmada molecularmente.

Gêneros afetados: *Condylospora, Dendrosporomyces, Flabellospora, Ingoldiella, Jaculispora, Naiadella, Pyramidospora, Scutisporus, Trinacrium, Trisulcosporium, Flabelloscladia, Dendrosporium, Diplocladiella, Brachiosphaera, Volucrispora, Actinospora*

### 3. Gêneros com Morfotipo Atribuído (11 gêneros)

| Gênero | Morfotipo Atribuído | Justificativa |
|--------|-------------------|---------------|
| Condylospora | Stauroid | Conídios ramificados complexos |
| Dendrosporomyces | Stauroid | Conídios holoblásticos ramificados dendríticos |
| Dwayaangam | Stauroid | Conídios ramificados corniformes |
| Filosporella | Scolecoid | Conídios filiformes variáveis |
| Jaculispora | Scolecoid | Conídios alongados javelin-like |
| Margaritispora | Stauroid | Conídios globosos catenados |
| Scutisporus | Stauroid | Conídios escutelados |
| Trisulcosporium | Stauroid | Conídios trisulcados |
| Dendrosporium | Stauroid | Conídios dendríticos lobados |
| Aquanectria | Scolecoid | Conídios fusiformes curvados |
| Diplocladiella | Stauroid | Conídios ramificados escalaroides |

### 4. Espécies com Distribuição Geográfica Adicionada (5 espécies)

| Espécie | Região | Referência |
|---------|--------|-----------|
| A. crassa | Mata Atlântica | Schoenlein-Crusius & Milanez |
| C. aquatica | Regiões Temperadas | Cosmopolita temperada |
| T. elegans | Regiões Temperadas | Predominante em temperados |
| T. splendens | Mata Atlântica + Temperadas | Fiuza et al. 2017 |
| V. elodeae | Regiões Temperadas | Cosmopolita temperada |

---

## Análise de Conectividade

### Hubs Principais (grau ≥ 7)

```
Helotiales              (Order)              grau=37
Estauróide              (ConidialMorphotype) grau=33
Ingoldiano              (EcologicalGroup)    grau=24
Escolecoide             (ConidialMorphotype) grau=20
Mata Atlântica          (GeographicRegion)   grau=12
Anguillospora           (Genus)              grau=8
Amazônia Brasileira     (GeographicRegion)   grau=8
Caatinga                (GeographicRegion)   grau=8
Cerrado                 (GeographicRegion)   grau=8
Leotiomycetes           (Class)              grau=7
Tricladium              (Genus)              grau=7
```

### Cobertura Brasileira

| Bioma | Espécies Registradas |
|-------|---------------------|
| Mata Atlântica | 12 |
| Amazônia | 8 |
| Caatinga | 8 |
| Cerrado | 8 |

### Gêneros Polifiléticos Modelados

| Gênero | Classes | Implicação para CV |
|--------|---------|-------------------|
| Anguillospora | Leotiomycetes, Dothideomycetes, Orbiliomycetes | Classificação por morfologia é insuficiente; necessita features adicionais |
| Tricladium | Leotiomycetes (múltiplos clados) | Espécies morfologicamente similares são filogeneticamente distantes |
| Lemonniera | Leotiomycetes (múltiplos clados) | Idem |
| Flagellospora | Leotiomycetes (múltiplos clados) | Idem |
| Varicosporium | Leotiomycetes (múltiplos clados) | Idem |

---

## Recomendações para v1.2

1. **Expandir espécies**: Incluir as 85 espécies do checklist brasileiro (Fiuza et al. 2017)
2. **Adicionar teleomorfos**: Conectar anamorfos aos seus estados sexuados conhecidos (10% das espécies)
3. **Dados moleculares**: Incluir nós com accession numbers ITS/LSU do GenBank
4. **Atributos quantitativos**: Adicionar ranges de dimensões conidiais (comprimento × largura em μm) por espécie
5. **Resolver incertae sedis**: À medida que dados moleculares estiverem disponíveis, atualizar `placement_status`
