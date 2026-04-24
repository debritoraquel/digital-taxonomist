#!/usr/bin/env bash
# =============================================================================
# setup_github_neo4j.sh
# Configura o repositório GitHub e importa o grafo ontológico no Neo4j
#
# Uso:
#   chmod +x setup_github_neo4j.sh
#   ./setup_github_neo4j.sh
# =============================================================================

set -e

echo "============================================"
echo " Digital Taxonomist — Setup GitHub + Neo4j"
echo "============================================"
echo ""

# ─────────────────────────────────────────────
# ETAPA 1: Inicializar repositório Git local
# ─────────────────────────────────────────────
echo "[1/6] Inicializando repositório Git..."

git init
git add .
git commit -m "feat: initial commit — graph v1.1 (116 nodes, 216 edges, 0 orphans)

- Knowledge graph JSON with full taxonomy of aquatic hyphomycetes
- Siamese ViT architecture for few-shot classification
- Preprocessing pipeline v3 (percentile-based)
- Graph loader, validator, and query engine for Neo4j
- CI/CD with graph coherence checks
- 42 coherence issues from v1.0 fixed in v1.1"

echo "   ✓ Commit inicial criado"

# ─────────────────────────────────────────────
# ETAPA 2: Criar repositório no GitHub via CLI
# ─────────────────────────────────────────────
echo ""
echo "[2/6] Criando repositório no GitHub..."
echo "   (requer GitHub CLI instalado: brew install gh / sudo apt install gh)"
echo ""

# Verifica se gh está instalado
if command -v gh &> /dev/null; then
    echo "   GitHub CLI detectado. Criando repositório..."
    gh repo create digital-taxonomist \
        --public \
        --description "Automated classification of aquatic hyphomycetes using CV, knowledge graphs, and multimodal AI" \
        --source . \
        --push
    echo "   ✓ Repositório criado e código enviado"
else
    echo "   ⚠ GitHub CLI não encontrado."
    echo "   Opção manual:"
    echo ""
    echo "   1. Vá em https://github.com/new"
    echo "   2. Nome: digital-taxonomist"
    echo "   3. Visibilidade: Public"
    echo "   4. NÃO marque 'Add README' (já temos)"
    echo "   5. Clique 'Create repository'"
    echo "   6. Execute:"
    echo ""
    echo "      git remote add origin https://github.com/SEU_USUARIO/digital-taxonomist.git"
    echo "      git branch -M main"
    echo "      git push -u origin main"
    echo ""
    read -p "   Pressione ENTER quando tiver criado o repositório no GitHub... "

    read -p "   Seu username do GitHub: " GH_USER
    git remote add origin "https://github.com/${GH_USER}/digital-taxonomist.git"
    git branch -M main
    git push -u origin main
    echo "   ✓ Código enviado para GitHub"
fi

# ─────────────────────────────────────────────
# ETAPA 3: Subir Neo4j via Docker
# ─────────────────────────────────────────────
echo ""
echo "[3/6] Iniciando Neo4j via Docker Compose..."

if command -v docker &> /dev/null; then
    docker compose up -d
    echo "   Aguardando Neo4j inicializar (30s)..."
    sleep 30

    # Verificar se está rodando
    if docker compose ps | grep -q "running"; then
        echo "   ✓ Neo4j rodando em http://localhost:7475"
        echo "   ✓ Bolt em bolt://localhost:7687"
        echo "   ✓ Credenciais: neo4j / taxonomist2026"
    else
        echo "   ⚠ Neo4j pode ainda estar iniciando. Verifique com: docker compose logs neo4j"
    fi
else
    echo "   ⚠ Docker não encontrado."
    echo "   Alternativas:"
    echo "   a) Instalar Docker: https://docs.docker.com/get-docker/"
    echo "   b) Usar Neo4j Desktop: https://neo4j.com/download/"
    echo "   c) Usar Neo4j Aura (cloud gratuito): https://neo4j.com/cloud/aura-free/"
    echo ""
    echo "   Para Neo4j Desktop ou Aura, ajuste URI/credenciais nos próximos passos."
fi

# ─────────────────────────────────────────────
# ETAPA 4: Instalar dependências Python
# ─────────────────────────────────────────────
echo ""
echo "[4/6] Instalando dependências Python..."

if command -v pip &> /dev/null; then
    pip install neo4j click rich pyyaml tqdm 2>/dev/null || \
    pip install neo4j click rich pyyaml tqdm --break-system-packages 2>/dev/null
    echo "   ✓ Dependências instaladas"
else
    echo "   ⚠ pip não encontrado. Execute manualmente:"
    echo "      pip install neo4j click rich pyyaml tqdm"
fi

# ─────────────────────────────────────────────
# ETAPA 5: Validar o grafo antes de importar
# ─────────────────────────────────────────────
echo ""
echo "[5/6] Validando coerência do grafo..."
python src/graph/validate.py --json data/neo4j/aquatic_hyphomycetes_graph.json

# ─────────────────────────────────────────────
# ETAPA 6: Importar no Neo4j
# ─────────────────────────────────────────────
echo ""
echo "[6/6] Importando grafo no Neo4j..."
python src/graph/load_graph.py \
    --json data/neo4j/aquatic_hyphomycetes_graph.json \
    --uri bolt://localhost:7687 \
    --user neo4j \
    --password taxonomist2026 \
    --verify

echo ""
echo "============================================"
echo " ✓ SETUP COMPLETO!"
echo "============================================"
echo ""
echo " GitHub:  https://github.com/SEU_USUARIO/digital-taxonomist"
echo " Neo4j:   http://localhost:7475  (neo4j/taxonomist2026)"
echo ""
echo " Próximos passos:"
echo "   1. Abrir Neo4j Browser: http://localhost:7475"
echo "   2. Executar: MATCH (n) RETURN n LIMIT 50"
echo "   3. Explorar o grafo visualmente"
echo ""
