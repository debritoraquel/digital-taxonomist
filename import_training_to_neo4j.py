#!/usr/bin/env python3
"""
import_training_to_neo4j.py -- Taxonomista Digital (2026-04-21)
================================================================
Importa training_index.jsonl para o Neo4j neo4j-hifomicetos.

Cria nós TrainingImage e os liga a:
  - Publicacao  ([:EXTRAIDA_DE])          via paper filename
  - Especie     ([:EXEMPLIFICA_ESPECIE])  via binomial (cria se não existir)
  - Genero      ([:PERTENCE_A_GENERO])    via genus    (cria se não existir)

Uso:
    python import_training_to_neo4j.py
    python import_training_to_neo4j.py --index D:/MBA/tcc/training_index.jsonl
    python import_training_to_neo4j.py --dry-run   # mostra stats sem gravar
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("neo4j_import")

NEO4J_URI  = "bolt://localhost:7688"
NEO4J_USER = "neo4j"
NEO4J_PASS = "Hifomicetos123"

DEFAULT_INDEX = Path("D:/MBA/tcc/training_index.jsonl")


def connect(uri: str, user: str, password: str):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        log.error("Falta neo4j driver. pip install neo4j")
        sys.exit(1)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    log.info("Conectado ao Neo4j: %s", uri)
    return driver


def create_constraints(session) -> None:
    session.run("""
        CREATE CONSTRAINT training_image_id IF NOT EXISTS
        FOR (t:TrainingImage) REQUIRE t.id IS UNIQUE
    """)
    session.run("""
        CREATE CONSTRAINT especie_name IF NOT EXISTS
        FOR (e:Especie) REQUIRE e.name IS UNIQUE
    """)
    session.run("""
        CREATE CONSTRAINT genero_name IF NOT EXISTS
        FOR (g:Genero) REQUIRE g.name IS UNIQUE
    """)
    log.info("Constraints verificadas/criadas.")


def import_record(tx, rec: dict) -> dict:
    # v8 schema uses "resolution"; older schemas use "epithet"
    epithet = rec.get("resolution") or rec.get("epithet") or {}
    classifier = rec.get("classifier") or {}
    binomial = epithet.get("binomial")
    genus    = epithet.get("genus")
    species  = epithet.get("epithet") or epithet.get("species")

    # 1. Cria/atualiza TrainingImage
    tx.run("""
        MERGE (t:TrainingImage {id: $id})
        SET t.paper        = $paper,
            t.page         = $page,
            t.fig_index    = $fig_index,
            t.image_type   = $image_type,
            t.image_type_raw = $image_type_raw,
            t.saved_to     = $saved_to,
            t.caption_raw  = $caption_raw,
            t.binomial     = $binomial,
            t.genus        = $genus,
            t.species      = $species,
            t.epithet_confidence = $epithet_confidence,
            t.epithet_rule = $epithet_rule,
            t.classifier_rule = $classifier_rule,
            t.width        = $width,
            t.height       = $height,
            t.sat_mean     = $sat_mean,
            t.edge_density = $edge_density,
            t.white_bg_ratio = $white_bg_ratio
    """, id=rec["id"],
         paper=rec.get("paper", ""),
         page=rec.get("page", 0),
         fig_index=rec.get("fig_index", 0),
         image_type=rec.get("image_type", "AMBIGUOUS"),
         image_type_raw=rec.get("image_type_raw", rec.get("image_type", "AMBIGUOUS")),
         saved_to=rec.get("saved_to", ""),
         caption_raw=(rec.get("caption_raw") or "")[:500],
         binomial=binomial,
         genus=genus,
         species=species,
         epithet_confidence=epithet.get("confidence", 0.0),
         epithet_rule=epithet.get("rule", ""),
         classifier_rule=classifier.get("rule_fired", ""),
         width=classifier.get("width", 0),
         height=classifier.get("height", 0),
         sat_mean=classifier.get("sat_mean", 0.0),
         edge_density=classifier.get("edge_density", 0.0),
         white_bg_ratio=classifier.get("white_bg_ratio", 0.0),
    )

    # 2. Liga a Publicacao existente (MATCH only — não cria publicações)
    tx.run("""
        MATCH (t:TrainingImage {id: $id})
        OPTIONAL MATCH (p:Publicacao {filename: $filename})
        FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
            MERGE (t)-[:EXTRAIDA_DE]->(p)
        )
    """, id=rec["id"], filename=rec.get("paper", ""))

    # 3. Liga/cria Especie
    if binomial:
        tx.run("""
            MERGE (e:Especie {name: $name})
            ON CREATE SET e.genero = $genus,
                          e.epiteto = $species,
                          e.spg_type = 'Especie',
                          e.origem = 'training_import'
            WITH e
            MATCH (t:TrainingImage {id: $id})
            MERGE (t)-[:EXEMPLIFICA_ESPECIE]->(e)
        """, name=binomial, genus=genus or "", species=species or "", id=rec["id"])

    # 4. Liga/cria Genero
    if genus:
        tx.run("""
            MERGE (g:Genero {name: $genus})
            ON CREATE SET g.spg_type = 'Genero',
                          g.origem = 'training_import'
            WITH g
            MATCH (t:TrainingImage {id: $id})
            MERGE (t)-[:PERTENCE_A_GENERO]->(g)
        """, genus=genus, id=rec["id"])

    return {"binomial": binomial, "genus": genus, "image_type": rec.get("image_type")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=str(DEFAULT_INDEX),
                    help="Caminho para training_index.jsonl")
    ap.add_argument("--uri",  default=NEO4J_URI)
    ap.add_argument("--user", default=NEO4J_USER)
    ap.add_argument("--pass", dest="password", default=NEO4J_PASS)
    ap.add_argument("--batch", type=int, default=50,
                    help="Tamanho do lote de transações")
    ap.add_argument("--dry-run", action="store_true",
                    help="Lê o JSONL e mostra estatísticas sem gravar no Neo4j")
    args = ap.parse_args()

    index_path = Path(args.index)
    if not index_path.is_file():
        log.error("training_index.jsonl não encontrado: %s", index_path)
        sys.exit(1)

    records = [json.loads(l) for l in index_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    log.info("%d registros no índice.", len(records))

    if args.dry_run:
        types = {}
        binomials = genus_only = unknown = 0
        for r in records:
            t = r.get("image_type", "?")
            types[t] = types.get(t, 0) + 1
            epi = r.get("resolution") or r.get("epithet") or {}
            if epi.get("binomial"):    binomials += 1
            elif epi.get("genus"):     genus_only += 1
            else:                      unknown += 1
        log.info("DRY-RUN — não grava nada.")
        log.info("Tipos: %s", types)
        log.info("Com binômio: %d | genus-only: %d | sem epíteto: %d", binomials, genus_only, unknown)
        return

    driver = connect(args.uri, args.user, args.password)

    with driver.session() as session:
        create_constraints(session)

    counts = {"created": 0, "with_binomial": 0, "with_genus": 0,
              "linked_publicacao": 0, "errors": 0}

    batch: list[dict] = []

    def flush(batch):
        with driver.session() as session:
            with session.begin_transaction() as tx:
                for rec in batch:
                    try:
                        result = import_record(tx, rec)
                        counts["created"] += 1
                        if result["binomial"]:   counts["with_binomial"] += 1
                        if result["genus"]:      counts["with_genus"] += 1
                    except Exception as e:
                        log.warning("Erro em %s: %s", rec.get("id"), e)
                        counts["errors"] += 1
                tx.commit()

    for i, rec in enumerate(records):
        batch.append(rec)
        if len(batch) >= args.batch:
            flush(batch)
            batch.clear()
            log.info("  %d/%d importados...", i + 1, len(records))

    if batch:
        flush(batch)

    driver.close()

    log.info("=" * 60)
    log.info("Importação concluída:")
    for k, v in counts.items():
        log.info("  %-20s %d", k, v)

    # Verificação rápida
    log.info("Verificando no Neo4j...")
    driver2 = connect(args.uri, args.user, args.password)
    with driver2.session() as s:
        n = s.run("MATCH (t:TrainingImage) RETURN count(t) as n").single()["n"]
        r = s.run("MATCH ()-[:EXEMPLIFICA_ESPECIE]->() RETURN count(*) as n").single()["n"]
        p = s.run("MATCH ()-[:EXTRAIDA_DE]->() RETURN count(*) as n").single()["n"]
        log.info("  TrainingImage nodes  : %d", n)
        log.info("  EXEMPLIFICA_ESPECIE  : %d relações", r)
        log.info("  EXTRAIDA_DE          : %d relações", p)
    driver2.close()


if __name__ == "__main__":
    main()
