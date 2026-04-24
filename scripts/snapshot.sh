#!/usr/bin/env bash
# =============================================================================
# snapshot.sh — Exporta o grafo Neo4j e cria um commit versionado no Git
#
# Uso:
#   ./scripts/snapshot.sh                       # exporta e commita
#   ./scripts/snapshot.sh --tag v2.1.0          # adiciona tag git
#   ./scripts/snapshot.sh --no-commit           # só exporta, não commita
#   ./scripts/snapshot.sh --diff                # mostra diff com export anterior
#   ./scripts/snapshot.sh --gzip                # comprime com gzip antes de commitar
#
# Variáveis de ambiente:
#   NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD       (lidos de .env se existir)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# Carrega .env se existir
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo "  [env] Credenciais carregadas de .env"
fi

# Defaults
TAG=""
NO_COMMIT=false
SHOW_DIFF=false
USE_GZIP=false
EXTRA_ARGS=()

# Parse de argumentos
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)      TAG="$2"; shift 2 ;;
        --no-commit) NO_COMMIT=true; shift ;;
        --diff)     SHOW_DIFF=true; shift ;;
        --gzip)     USE_GZIP=true; EXTRA_ARGS+=("--gzip"); shift ;;
        *)          EXTRA_ARGS+=("$1"); shift ;;
    esac
done

TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
EXPORT_CURRENT="data/neo4j/graph_export.json"
SNAPSHOT_FILE="data/neo4j/snapshots/graph_${TIMESTAMP}.json"

echo "============================================"
echo " Digital Taxonomist — Snapshot Neo4j"
echo " $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================"
echo ""

# ── 1. Verificar dependências ──────────────────────────────────────────────
echo "[1/5] Verificando dependências Python..."
python -c "import neo4j, rich, click" 2>/dev/null || {
    echo "  Instalando dependências mínimas..."
    pip install neo4j rich click -q
}
echo "  ✓ OK"
echo ""

# ── 2. Guardar export anterior para diff ──────────────────────────────────
PREV_EXPORT=""
if $SHOW_DIFF && [ -f "$EXPORT_CURRENT" ]; then
    PREV_EXPORT="data/neo4j/snapshots/graph_prev_${TIMESTAMP}.json"
    cp "$EXPORT_CURRENT" "$PREV_EXPORT"
    echo "[2/5] Export anterior salvo em: $PREV_EXPORT"
else
    echo "[2/5] (sem export anterior para diff)"
fi
echo ""

# ── 3. Exportar grafo ─────────────────────────────────────────────────────
echo "[3/5] Exportando grafo do Neo4j..."
python scripts/export_neo4j.py \
    --snapshot \
    "${EXTRA_ARGS[@]}" \
    "$@" 2>/dev/null || python scripts/export_neo4j.py --snapshot "${EXTRA_ARGS[@]}"

# Encontra o snapshot mais recente criado
LATEST_SNAPSHOT=$(ls -t data/neo4j/snapshots/graph_*.json 2>/dev/null | head -1 || true)

if [ -z "$LATEST_SNAPSHOT" ]; then
    echo "  ✗ Nenhum snapshot encontrado após exportação."
    exit 1
fi
echo ""
echo "  Snapshot: $LATEST_SNAPSHOT"
echo "  Current:  $EXPORT_CURRENT"

# ── 4. Diff (opcional) ────────────────────────────────────────────────────
echo ""
echo "[4/5] Diff..."
if $SHOW_DIFF && [ -n "$PREV_EXPORT" ]; then
    python scripts/diff_graph.py "$PREV_EXPORT" "$EXPORT_CURRENT" --quiet || true
    rm -f "$PREV_EXPORT"
else
    echo "  (use --diff para comparar com a versão anterior)"
fi

# ── 5. Git commit ─────────────────────────────────────────────────────────
echo ""
echo "[5/5] Versionando no Git..."

if $NO_COMMIT; then
    echo "  --no-commit ativo: pulando commit."
    echo ""
    echo "  Para commitar manualmente:"
    echo "    git add $EXPORT_CURRENT"
    echo "    git add $LATEST_SNAPSHOT"
    echo "    git commit -m \"data: snapshot Neo4j $TIMESTAMP\""
    exit 0
fi

# Verifica se estamos em um repositório git
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "  ✗ Não é um repositório git. Exportação salva mas não commitada."
    exit 0
fi

# Conta nós e arestas do export para a mensagem de commit
NODE_COUNT=$(python -c "
import json
with open('$EXPORT_CURRENT') as f:
    d = json.load(f)
print(d['metadata'].get('node_count', len(d['nodes'])))
" 2>/dev/null || echo "?")

REL_COUNT=$(python -c "
import json
with open('$EXPORT_CURRENT') as f:
    d = json.load(f)
print(d['metadata'].get('relationship_count', len(d['relationships'])))
" 2>/dev/null || echo "?")

GRAPH_VERSION=$(python -c "
import json
with open('$EXPORT_CURRENT') as f:
    d = json.load(f)
print(d['metadata'].get('version', 'unknown'))
" 2>/dev/null || echo "unknown")

git add "$EXPORT_CURRENT" "$LATEST_SNAPSHOT" 2>/dev/null || true
# Adiciona CSVs se existirem
if [ -d "data/neo4j/csv" ]; then
    git add data/neo4j/csv/ 2>/dev/null || true
fi

COMMIT_MSG="data: snapshot Neo4j v${GRAPH_VERSION} — ${NODE_COUNT} nós, ${REL_COUNT} arestas

Exportado em: ${TIMESTAMP} UTC
Snapshot: ${LATEST_SNAPSHOT}

https://claude.ai/code/session_01JQaXdPg9yhtN666T1hbPGL"

git commit -m "$COMMIT_MSG" || {
    echo "  (nada a commitar — grafo sem alterações desde o último snapshot)"
}

# Tag opcional
if [ -n "$TAG" ]; then
    git tag -a "$TAG" -m "Grafo Neo4j $TAG — ${NODE_COUNT} nós, ${REL_COUNT} arestas"
    echo "  ✓ Tag criada: $TAG"
    echo "  Para publicar: git push origin $TAG"
fi

echo ""
echo "============================================"
echo " ✓ Snapshot concluído!"
echo "============================================"
echo ""
echo "  Arquivo atual:   $EXPORT_CURRENT"
echo "  Snapshot datado: $LATEST_SNAPSHOT"
echo ""
echo " Próximos passos:"
echo "   git push origin $(git branch --show-current)"
echo "   python scripts/diff_graph.py data/neo4j/snapshots/<old> $EXPORT_CURRENT"
echo ""
