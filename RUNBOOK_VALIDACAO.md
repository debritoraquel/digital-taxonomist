# RUNBOOK — Validação com dados reais (20-04-2026)

Este runbook cobre as duas fases solicitadas:

- **Fase 1 — Treinamento:** extrair imagens e epítetos dos 24 PDFs de chaves taxonômicas em `D:\MBA\tcc\REFERENCIAS\Chaves com imagem`, separando automaticamente desenhos de lâminas.
- **Fase 2 — Validação:** rodar inferência DINOv2 + FAISS sobre `C:\Users\Usuário\Desktop\Fotos_Organizadas_20260128_184422` e gerar `C:\Users\Usuário\Desktop\Validação_e_teste_20_04_26` preservando a hierarquia dos pontos de amostragem.

---

## Pré-requisitos

Ative o venv do projeto e confirme as dependências:

```powershell
cd D:\MBA\tcc\taxonomista-digital
venv\Scripts\activate
pip install PyMuPDF Pillow numpy opencv-python tqdm
# Se ainda não tiver o stack DL:
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install transformers faiss-cpu
```

Copie a pasta `validacao_2026_04_20\` do seu workspace (`C:\Users\Usuário\Desktop\claude\taxonomista\`) para `D:\MBA\tcc\taxonomista-digital\validacao_2026_04_20\`, ou rode direto do workspace se o venv estiver acessível.

---

## Fase 1 — Extração dos 24 PDFs

### 1.1. Teste rápido do extrator de epíteto

```powershell
cd D:\MBA\tcc\taxonomista-digital\validacao_2026_04_20
python genus_species_extractor.py --selftest
```

Esperado: `Self-test: 8/8 passed`. Se algum teste falhar, o vocabulário controlado precisa de ajuste antes de rodar a extração completa.

### 1.2. Dry-run (só lista o que seria extraído)

```powershell
python extract_training_pdfs.py ^
  --src "D:\MBA\tcc\REFERENCIAS\Chaves com imagem" ^
  --drawings-out   "D:\MBA\tcc\treinamento_desenho_taxonomico" ^
  --microscopy-out "D:\MBA\tcc\treinamento_lamina_taxonomico" ^
  --ambiguous-out  "D:\MBA\tcc\treinamento_ambiguo" ^
  --genera-db "D:\MBA\tcc\taxonomista-digital\data\taxonomista_digital.db" ^
  --dry-run
```

Use isso para validar o vocabulário e a detecção de legendas antes de gravar qualquer arquivo.

### 1.3. Extração completa

Remova `--dry-run`:

```powershell
python extract_training_pdfs.py ^
  --src "D:\MBA\tcc\REFERENCIAS\Chaves com imagem" ^
  --drawings-out   "D:\MBA\tcc\treinamento_desenho_taxonomico" ^
  --microscopy-out "D:\MBA\tcc\treinamento_lamina_taxonomico" ^
  --ambiguous-out  "D:\MBA\tcc\treinamento_ambiguo" ^
  --genera-db "D:\MBA\tcc\taxonomista-digital\data\taxonomista_digital.db" ^
  --min-width 120 --min-height 120
```

**Saída esperada:**

Três pastas em `D:\MBA\tcc\`:

- `treinamento_desenho_taxonomico\` — line drawings
- `treinamento_lamina_taxonomico\` — imagens de microscopia
- `treinamento_ambiguo\` — casos para revisão manual

Cada imagem segue o padrão de nome canônico:

```
{paper_slug}__p{page:03d}__fig{idx:02d}__{hash6}__{Genus_species}.png
```

Exemplo:

```
santos2019_chave_tetracladium__p005__fig02__a1b2c3__Tetracladium_marchalianum.png
```

E um JSON sidecar de mesmo `base_id` com legenda bruta, bbox no PDF, classificador (features + regra disparada) e epíteto (genus, species, confidence, regra, `all_candidates`).

Um arquivo `training_index.jsonl` consolidado é gravado em `D:\MBA\tcc\`, servindo de trilha de auditoria única para todas as imagens extraídas.

### 1.4. Revisão manual da pasta `_ambiguo`

Abra a pasta, inspecione as imagens caso a caso e mova manualmente para `desenho` ou `lamina`. Essa é a única etapa que exige olho humano — o classificador é intencionalmente conservador e só trabalha com regras determinísticas rápidas (saturação, densidade de bordas, fundo branco, dominância azul da coloração por azul de algodão).

### 1.5. Rigor do epíteto

Todas as extrações passam por:

1. **Regex estrito de binomial** (`[A-Z][a-z]{2,}\s+[a-z]{4,}`).
2. **Validação cruzada contra os 81 gêneros** do SQLite do pipeline (ou lista fallback embutida com os mesmos 81).
3. **Blacklist de espúrios** (`figure`, `plate`, `scale`, `water`, `conidia`, etc.).
4. **Bloqueio de seções não confiáveis** (`material examined`, `etymology`, `holotype`…).
5. **Fallback controlado para `genus-only`** quando o binomial não aparece (marcado `uncertain=True`, arquivo termina em `_sp`).
6. **Casos sem match** ficam como `UNKNOWN_UNKNOWN` com legenda completa no JSON para revisão.

---

## Fase 2 — Validação com dados reais

Rode a inferência sobre as fotos de ponto de amostragem. A hierarquia de subpastas é preservada.

```powershell
python validate_real_samples.py ^
  --target "C:\Users\Usuário\Desktop\Fotos_Organizadas_20260128_184422" ^
  --out    "C:\Users\Usuário\Desktop\Validação_e_teste_20_04_26" ^
  --training-dirs "D:\MBA\tcc\treinamento_desenho_taxonomico" ^
                  "D:\MBA\tcc\treinamento_lamina_taxonomico" ^
  --model dinov2-small ^
  --topk 5 ^
  --min-confidence 0.35
```

**O que acontece:**

1. Carrega DINOv2-small (mesmo modelo do pipeline principal).
2. Gera embeddings de todas as imagens de treino rotuladas (desenho + lâmina) e monta um índice FAISS (cosseno via produto interno normalizado).
3. Para cada imagem real, faz k-NN (`topk=5`) e agrega votos ponderados pela similaridade.
4. Salva a imagem na pasta `Validação_e_teste_20_04_26\<ponto>\` com nome:

```
<nome_original>__<Genero>_<especie>__conf<NN>.<ext>
```

Imagens com confiança abaixo de `--min-confidence` recebem o prefixo `UNCERTAIN_` e precisam de revisão humana.

**Artefatos gerados:**

- `Validação_e_teste_20_04_26\<ponto>\...` — imagens identificadas
- `Validação_e_teste_20_04_26\predictions.csv` — tabela mestre com `sampling_point, target_path, best_genus, best_species, confidence, top3_json, output_path`
- `Validação_e_teste_20_04_26\summary_by_point.md` — contagem de gêneros por ponto de amostragem

### 2.1. Nota sobre a escala

Você mencionou que algumas imagens não têm barra de escala mas usam a mesma escala das outras. Para esta fase, o DINOv2 é **invariante a escala em nível de pixel** (o processador redimensiona tudo para 224×224), então a ausência da barra não afeta o embedding visual. A escala importa na fase de **morfometria** (comprimento e largura em µm) — nessa fase, use apenas as imagens que **tenham** a barra de escala detectada como referência fiducial para todas as demais do mesmo conjunto. O módulo de detecção de escala (`scale_bar_detector.py`) do pipeline principal cuida disso a jusante.

### 2.2. Comparação com a pasta revisada por especialista

Para avaliar acurácia, cruze `predictions.csv` com a pasta revisada:

```python
import pandas as pd
pred = pd.read_csv("predictions.csv")
# junte com anotações do especialista e calcule accuracy por gênero e por ponto
```

---

## Neo4j — endereços corretos

Você escreveu `http://localhost:7688/` duas vezes na última mensagem — presumo que a segunda deveria ser outro endereço. A configuração confirmada em 2026-04-18 é:

- `bolt://localhost:7688` — **porta Bolt** (protocolo binário do driver). **Não abre no navegador** — fica em branco porque o endpoint não é HTTP.

Se o container `neo4j-hifomicetos` estiver expondo portas diferentes, confirme no Docker Desktop na aba "Ports" do container. Se a porta HTTP estiver mapeada para algo diferente de 7475 (por exemplo, 7474 default do Neo4j ou outra), ajuste o URL no navegador — o Bolt sempre fica no 7688 conforme seu `config.py`.

Para reiterar o mapeamento canônico do projeto:

| Protocolo | Porta | Endereço | Uso |
|-----------|-------|----------|-----|
| Bolt | 7688 | `bolt://localhost:7688` | Drivers Python (fix_neo4j_graph.py, export_to_neo4j.py) |
| HTTP | 7475 | `http://localhost:7475` | Neo4j Browser (queries interativas, `:source`) |
senha: hifomicetos123

---

## Critérios de aceite da validação

Antes de considerar a Fase 2 concluída:

1. Fase 1 rodou sem erros, `training_index.jsonl` tem pelo menos uma entrada por PDF (24 arquivos).
2. Pasta `_ambiguo\` tem volume baixo (ideal < 15% do total).
3. Taxa de binomiais extraídos (not `UNKNOWN_UNKNOWN`, not `_sp`) ≥ 70% no indice de treino.
4. Fase 2 gerou `predictions.csv` com uma linha por imagem real encontrada.
5. `summary_by_point.md` lista todos os pontos de amostragem com pelo menos um gênero identificado.
6. Spot-check manual em 10 imagens aleatórias para aferir se o gênero predito é plausível.
