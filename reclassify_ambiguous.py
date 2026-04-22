#!/usr/bin/env python3
"""
reclassify_ambiguous.py -- Taxonomista Digital (2026-04-21)
============================================================
Reclassifica os nós TrainingImage AMBIGUOUS usando o classificador
atualizado + override por legenda. Move arquivos entre pastas e
atualiza image_type no Neo4j e no training_index.jsonl.

Uso:
    python reclassify_ambiguous.py
    python reclassify_ambiguous.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reclassify")

NEO4J_URI  = "bolt://localhost:7688"
NEO4J_USER = "neo4j"
NEO4J_PASS = "Hifomicetos123"

INDEX_PATH    = Path("D:/MBA/tcc/training_index.jsonl")
DIR_DRAW      = Path("D:/MBA/tcc/treinamento_desenho_taxonomico")
DIR_MICRO     = Path("D:/MBA/tcc/treinamento_lamina_taxonomico")
DIR_AMB       = Path("D:/MBA/tcc/treinamento_ambiguo")

# Captions de imagens que são ruído (copyright, cabeçalho institucional)
_NOISE_KEYWORDS = (
    "licensee mdpi", "copyright", "all rights reserved",
    "universidade estadual", "nrc canada", "open access",
    "creative commons", "license",
)


def is_noise_image(caption: str) -> bool:
    low = caption.lower()
    return any(kw in low for kw in _NOISE_KEYWORDS)


def reclassify_image(rec: dict) -> str:
    """Retorna o novo image_type para um registro AMBIGUOUS."""
    caption = rec.get("caption_raw") or ""

    # Imagem de ruído (copyright, logo) → descarta do treino
    if is_noise_image(caption):
        return "NOISE"

    # Importa após correções
    sys.path.insert(0, str(Path(__file__).parent))
    from image_type_classifier import classify
    from extract_training_pdfs import caption_type_override, _TREE_CAPTION_RE, _MICRO_CAPTION_RE

    saved_to = rec.get("saved_to", "")
    img_path = Path(saved_to) if saved_to else None

    # Se arquivo existe, reclassifica pela imagem
    if img_path and img_path.is_file():
        try:
            cls = classify(img_path)
            new_type = caption_type_override(cls.image_type, caption)
            return new_type
        except Exception as e:
            log.warning("Falhou classify em %s: %s", img_path.name, e)

    # Fallback: só legenda
    if _TREE_CAPTION_RE.search(caption):
        return "DRAWING"
    if _MICRO_CAPTION_RE.search(caption):
        return "MICROSCOPY"
    return "AMBIGUOUS"


def move_file(saved_to: str, new_type: str, dry_run: bool) -> str:
    """Move PNG + JSON sidecar para a pasta correta. Retorna novo saved_to."""
    if not saved_to:
        return saved_to
    src = Path(saved_to)
    if not src.is_file():
        return saved_to

    dest_dir = {
        "DRAWING":    DIR_DRAW,
        "MICROSCOPY": DIR_MICRO,
        "AMBIGUOUS":  DIR_AMB,
        "NOISE":      DIR_AMB / "noise",
    }.get(new_type, DIR_AMB)

    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / src.name
    if src.parent == dest_dir:
        return str(src)  # já no lugar certo

    if not dry_run:
        shutil.move(str(src), str(dest))
        # Move sidecar JSON também
        json_src = src.with_suffix("").parent / (src.stem.rsplit("__", 1)[0] + ".json")
        if json_src.is_file():
            shutil.move(str(json_src), str(dest_dir / json_src.name))

    return str(dest)


def update_neo4j(changes: list[dict]) -> None:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        log.error("pip install neo4j")
        return
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with driver.session() as session:
        with session.begin_transaction() as tx:
            for c in changes:
                tx.run("""
                    MATCH (t:TrainingImage {id: $id})
                    SET t.image_type     = $new_type,
                        t.image_type_raw = $new_type_raw,
                        t.saved_to       = $saved_to
                """, id=c["id"], new_type=c["new_type"],
                     new_type_raw=c["new_type_raw"],
                     saved_to=c["new_saved_to"])
            tx.commit()
    driver.close()
    log.info("Neo4j atualizado: %d nós.", len(changes))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    records = [json.loads(l) for l in INDEX_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    ambiguous = [r for r in records if r.get("image_type") == "AMBIGUOUS"]
    log.info("AMBIGUOUS a reclassificar: %d", len(ambiguous))

    changes: list[dict] = []
    stats = {"DRAWING": 0, "MICROSCOPY": 0, "AMBIGUOUS": 0, "NOISE": 0}

    for rec in ambiguous:
        new_type = reclassify_image(rec)
        old_saved = rec.get("saved_to", "")
        new_saved = move_file(old_saved, new_type, args.dry_run)
        stats[new_type] = stats.get(new_type, 0) + 1

        epi = rec.get("epithet", {})
        log.info("  %-50s  %s → %s  (%s)",
                 rec["id"][:50], "AMBIGUOUS", new_type,
                 (epi.get("binomial") or epi.get("genus") or "no-epithet"))

        if new_type != "AMBIGUOUS":
            changes.append({
                "id": rec["id"],
                "new_type": new_type if new_type != "NOISE" else "AMBIGUOUS",
                "new_type_raw": new_type,
                "new_saved_to": new_saved,
            })
            # Atualiza registro in-memory para reescrever o JSONL
            rec["image_type"] = new_type if new_type != "NOISE" else "AMBIGUOUS"
            rec["image_type_raw_v2"] = new_type
            rec["saved_to"] = new_saved

    log.info("Resultado: %s", stats)

    if args.dry_run:
        log.info("DRY-RUN — nenhuma alteração gravada.")
        return

    # Reescreve JSONL atualizado
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log.info("training_index.jsonl atualizado.")

    # Atualiza Neo4j
    if changes:
        update_neo4j(changes)

    log.info("Concluído. %d nós reclassificados.", len(changes))


if __name__ == "__main__":
    main()
