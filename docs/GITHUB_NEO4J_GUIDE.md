# Guia Completo: JSON Ontológico → GitHub → Neo4j

## Visão geral do fluxo

```
 [JSON v1.1]           [GitHub]              [Neo4j]
 116 nós    ──git push──▶ repositório ──clone──▶ load_graph.py ──▶ grafo ativo
 216 arestas              remoto               (Cypher/APOC)     consultas
```

---

## MÉTODO 1: Via linha de comando (recomendado)

### Pré-requisitos

```bash
# Git
git --version          # já instalado na maioria dos sistemas

# GitHub CLI (opcional, facilita criação do repo)
# macOS:
brew install gh
# Ubuntu/Debian:
sudo apt install gh
# Windows:
winget install GitHub.cli

# Python 3.10+
python --version

# Docker (para Neo4j local)
docker --version
```

### Passo 1 — Descompactar o projeto

```bash
# Descompactar o zip que você baixou
unzip digital-taxonomist.zip
cd digital-taxonomist
```

### Passo 2 — Inicializar Git e fazer o primeiro commit

```bash
git init
git add .
git commit -m "feat: knowledge graph v1.1 - aquatic hyphomycetes ontology

- 116 nodes (15 label types) / 216 relationships (21 types)
- 41 genera, 15 species, 5 conidial morphotypes
- Brazilian distribution data (Amazon, Atlantic Forest, Caatinga, Cerrado)
- CV integration nodes (Siamese ViT, VLM, Pipeline v3)
- 0 orphan nodes, 0 broken references (audited)"
```

### Passo 3 — Criar repositório no GitHub

**Opção A: GitHub CLI (mais rápido)**
```bash
gh auth login                    # autenticar (só na primeira vez)
gh repo create digital-taxonomist --public --source . --push
```

**Opção B: Interface web**
1. Acesse https://github.com/new
2. Repository name: `digital-taxonomist`
3. Visibilidade: **Public**
4. **NÃO** marque "Add a README file" (já temos)
5. Clique **Create repository**
6. Execute os comandos que o GitHub mostra:

```bash
git remote add origin https://github.com/SEU_USUARIO/digital-taxonomist.git
git branch -M main
git push -u origin main
```

### Passo 4 — Verificar no GitHub

Acesse `https://github.com/SEU_USUARIO/digital-taxonomist` e confirme que o arquivo
`data/neo4j/aquatic_hyphomycetes_graph.json` está lá.

### Passo 5 — Subir Neo4j com Docker

```bash
# Na pasta do projeto:
docker compose up -d

# Aguardar ~30 segundos e verificar:
docker compose logs neo4j | tail -5

# Deve mostrar: "Started."
```

O Neo4j estará disponível em:
- **Browser**: http://localhost:7475
- **Bolt**: bolt://localhost:7687
- **Credenciais**: `neo4j` / `taxonomist2026`

### Passo 6 — Importar o grafo no Neo4j

```bash
# Instalar dependências Python
pip install neo4j click rich pyyaml tqdm

# Validar coerência antes de importar
python src/graph/validate.py --json data/neo4j/aquatic_hyphomycetes_graph.json

# Importar
python src/graph/load_graph.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --uri bolt://localhost:7687 \
    --user neo4j \
    --password taxonomist2026 \
    --verify
```

### Passo 7 — Verificar no Neo4j Browser

Acesse http://localhost:7475 e execute:

```cypher
// Ver todo o grafo (cuidado: muitos nós)
MATCH (n)-[r]->(m) RETURN n, r, m LIMIT 100

// Contar nós e arestas
MATCH (n) RETURN count(n) AS nodes
UNION ALL
MATCH ()-[r]->() RETURN count(r) AS relationships

// Ver a hierarquia taxonômica completa de Tetracladium
MATCH path = (s:Species)-[:BELONGS_TO_GENUS]->(g:Genus)-[:BELONGS_TO_ORDER]->(o:Order)
              -[:BELONGS_TO_CLASS]->(c:Class)-[:BELONGS_TO_PHYLUM]->(p:Phylum)
WHERE g.name = 'Tetracladium'
RETURN path

// Ver todos os gêneros estauróides
MATCH (g:Genus)-[:HAS_MORPHOTYPE]->(m:ConidialMorphotype {name: 'Estauróide (Stauroid)'})
RETURN g.name ORDER BY g.name

// Espécies por bioma brasileiro
MATCH (s:Species)-[:OCCURS_IN]->(r:GeographicRegion)
RETURN r.name AS bioma, collect(s.name) AS especies, count(s) AS total
ORDER BY total DESC
```

---

## MÉTODO 2: Importação direta via Cypher (sem Python)

Se você já tem Neo4j rodando e quer importar sem Python, copie o JSON para o diretório
de importação do Neo4j e use APOC:

```bash
# Copiar JSON para o volume de importação do Docker
docker cp data/neo4j/aquatic_hyphomycetes_graph.json \
    digital-taxonomist-neo4j:/var/lib/neo4j/import/
```

Depois, no Neo4j Browser:

```cypher
// Criar constraints
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Phylum) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Class) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Order) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Genus) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Species) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:ConidialMorphotype) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:MorphologicalAttribute) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Conidiogenesis) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:EcologicalGroup) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Habitat) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Substrate) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:GeographicRegion) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:EnzymaticActivity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:CVArchitecture) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (n:CVPipeline) REQUIRE n.id IS UNIQUE;

// Importar nós
CALL apoc.load.json('file:///aquatic_hyphomycetes_graph.json') YIELD value
UNWIND value.nodes AS node
CALL apoc.create.node([node.label], apoc.map.merge({id: node.id}, node.properties))
YIELD node AS n
RETURN count(n) AS nodes_created;

// Importar relacionamentos
CALL apoc.load.json('file:///aquatic_hyphomycetes_graph.json') YIELD value
UNWIND value.relationships AS rel
MATCH (a {id: rel.start_node_id}), (b {id: rel.end_node_id})
CALL apoc.create.relationship(a, rel.type, COALESCE(rel.properties, {}), b)
YIELD rel AS r
RETURN count(r) AS relationships_created;
```

---

## MÉTODO 3: Neo4j Aura (cloud gratuito)

Se não quiser instalar Docker:

1. Acesse https://neo4j.com/cloud/aura-free/
2. Crie uma instância gratuita
3. Anote o **Connection URI** (ex: `neo4j+s://abc123.databases.neo4j.io`)
4. Anote a **senha** gerada

```bash
# Importar no Aura (ajustar URI e senha)
python src/graph/load_graph.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --uri "neo4j+s://abc123.databases.neo4j.io" \
    --user neo4j \
    --password SUA_SENHA_AURA \
    --verify
```

Para Aura, o APOC não está disponível por padrão. Use o método Python (load_graph.py)
que usa Cypher puro via driver oficial.

---

## Fluxo de atualização (após modificações)

Quando você adicionar novos táxons, espécies ou corrigir dados:

```bash
# 1. Editar o JSON
# (ou usar scripts Python para gerar automaticamente)

# 2. Validar
python src/graph/validate.py --json data/neo4j/aquatic_hyphomycetes_graph.json

# 3. Commit + push
git add data/neo4j/aquatic_hyphomycetes_graph.json
git commit -m "data: add 5 new species from Fiuza et al. 2022"
git push

# 4. Re-importar no Neo4j (--no-clear para incremental, sem --no-clear para limpar e reimportar)
python src/graph/load_graph.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --uri bolt://localhost:7687 \
    --user neo4j \
    --password taxonomist2026
```

---

## Troubleshooting

| Problema | Solução |
|----------|---------|
| `git push` pede senha | Use `gh auth login` ou configure SSH: `ssh-keygen -t ed25519` → adicionar em GitHub Settings → SSH Keys |
| Neo4j não inicia | `docker compose logs neo4j` para ver erro. Comum: porta 7475/7687 já em uso |
| APOC não disponível | Verificar `NEO4J_PLUGINS: '["apoc"]'` no docker-compose.yml |
| `ModuleNotFoundError: neo4j` | `pip install neo4j` |
| JSON muito grande para APOC | Use `load_graph.py` que faz batch por nó/aresta |
| Aura recusa conexão | Verificar se URI usa `neo4j+s://` (com TLS) |

---

## Estrutura do JSON para referência

O arquivo `aquatic_hyphomycetes_graph.json` segue este schema:

```json
{
  "metadata": {
    "version": "1.1.0",
    "date": "2026-03-17",
    "sources": ["..."],
    "changelog": ["..."]
  },
  "nodes": [
    {
      "id": "genus_tetracladium",       // ID único (usado nas arestas)
      "label": "Genus",                  // Label do Neo4j
      "properties": {                    // Propriedades do nó
        "name": "Tetracladium",
        "authority": "De Wild. 1893",
        "conidial_shape": "tetraradiate",
        "description": "...",
        "phylogenetic_placement": "Leotiomycetes, Helotiales",
        "cv_morphotype": "stauroid_tetraradiate_robust"
      }
    }
  ],
  "relationships": [
    {
      "type": "BELONGS_TO_ORDER",        // Tipo do relacionamento
      "start_node_id": "genus_tetracladium",  // → referencia node.id
      "end_node_id": "order_helotiales",      // → referencia node.id
      "properties": {}                         // Propriedades opcionais
    }
  ]
}
```

Para adicionar um novo gênero, basta adicionar um objeto em `nodes[]` e as arestas
correspondentes em `relationships[]`, depois rodar a validação.
