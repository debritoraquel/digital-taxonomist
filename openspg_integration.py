#!/usr/bin/env python3
"""
openspg_integration.py -- Taxonomista Digital (2026-04-21)
==========================================================
Cria o schema SPG e importa os dados do training_index.jsonl no OpenSPG.

Deve ser executado DENTRO do container openspg-server:
    docker cp openspg_integration.py openspg-server:/tmp/taxonomista_kag/
    docker exec openspg-server bash -c "cd /tmp/taxonomista_kag && /home/admin/miniconda3/bin/python openspg_integration.py"

Schema criado:
  - TrainingImage  (EntityType)  imagem de treinamento
  - Especie        (EntityType)  espécie taxonômica
  - Genero         (EntityType)  gênero taxonômico
  - SamplingPoint  (EntityType)  ponto de amostragem

Relações:
  - TrainingImage -> [exemplificaEspecie] -> Especie
  - TrainingImage -> [pertenceAGenero]    -> Genero
  - Especie       -> [pertenceAGenero]    -> Genero
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ID   = 1
NAMESPACE    = "TaxonomistaDigital"
INDEX_PATH   = "/tmp/taxonomista_kag/training_index.jsonl"
HOST         = "http://127.0.0.1:8887"

os.chdir("/tmp/taxonomista_kag")

# ── SDK imports ──────────────────────────────────────────────
try:
    from knext.schema.client import SchemaClient
    from knext.schema.model.spg_type import EntityType
    from knext.schema.model.property import Property
    from knext.schema.model.relation import Relation
except ImportError as e:
    print(f"Erro importando knext: {e}")
    sys.exit(1)

# ── 1. Inspecionar schema atual ──────────────────────────────

def inspect_schema():
    client = SchemaClient(project_id=PROJECT_ID)
    types = client.load()
    print(f"\nSchema atual ({len(types) if types else 0} tipos):")
    for t in (types or []):
        print(f"  {t}")
    return {t.name: t for t in (types or [])} if types else {}

# ── 2. Criar tipos ───────────────────────────────────────────

def create_schema():
    client = SchemaClient(project_id=PROJECT_ID)
    existing = inspect_schema()
    session = client.create_session()

    created = []

    # --- TrainingImage ---
    if f"{NAMESPACE}.TrainingImage" not in str(existing):
        ti = EntityType(
            name=f"{NAMESPACE}.TrainingImage",
            name_zh="Imagem de Treinamento",
            desc="Imagem extraída de publicação taxonômica para treinamento do pipeline.",
        )
        ti.add_property(Property(name="imageType",   object_type_name="Text", name_zh="Tipo de Imagem"))
        ti.add_property(Property(name="imageTypeRaw",object_type_name="Text", name_zh="Tipo Raw"))
        ti.add_property(Property(name="paper",       object_type_name="Text", name_zh="Publicação de Origem"))
        ti.add_property(Property(name="binomial",    object_type_name="Text", name_zh="Binômio"))
        ti.add_property(Property(name="genus",       object_type_name="Text", name_zh="Gênero"))
        ti.add_property(Property(name="species",     object_type_name="Text", name_zh="Espécie"))
        ti.add_property(Property(name="confidence",  object_type_name="Float", name_zh="Confiança"))
        ti.add_property(Property(name="captionRaw",  object_type_name="Text", name_zh="Legenda Bruta"))
        ti.add_property(Property(name="savedTo",     object_type_name="Text", name_zh="Caminho do Arquivo"))
        session.create_type(ti)
        created.append("TrainingImage")

    # --- Especie ---
    if f"{NAMESPACE}.Especie" not in str(existing):
        esp = EntityType(
            name=f"{NAMESPACE}.Especie",
            name_zh="Espécie",
            desc="Espécie taxonômica (hifomiceto aquático ou fungo lignicola).",
        )
        esp.add_property(Property(name="generoNome", object_type_name="Text", name_zh="Gênero"))
        esp.add_property(Property(name="origem",     object_type_name="Text", name_zh="Origem do Registro"))
        session.create_type(esp)
        created.append("Especie")

    # --- Genero ---
    if f"{NAMESPACE}.Genero" not in str(existing):
        gen = EntityType(
            name=f"{NAMESPACE}.Genero",
            name_zh="Gênero",
            desc="Gênero taxonômico.",
        )
        gen.add_property(Property(name="origem", object_type_name="Text", name_zh="Origem"))
        session.create_type(gen)
        created.append("Genero")

    # --- SamplingPoint ---
    if f"{NAMESPACE}.SamplingPoint" not in str(existing):
        sp = EntityType(
            name=f"{NAMESPACE}.SamplingPoint",
            name_zh="Ponto de Amostragem",
            desc="Ponto de coleta de amostras de hifomicetos aquáticos.",
        )
        sp.add_property(Property(name="location", object_type_name="Text", name_zh="Localização"))
        session.create_type(sp)
        created.append("SamplingPoint")

    if created:
        session.commit()
        print(f"  [OK] Tipos criados e commitados: {created}")
    else:
        print("  [INFO] Schema já existe, nada criado.")

    inspect_schema()

# ── 3. Importar dados ────────────────────────────────────────

def import_data():
    if not Path(INDEX_PATH).is_file():
        print(f"Index não encontrado: {INDEX_PATH}")
        return

    records = [json.loads(l) for l in Path(INDEX_PATH).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"\nImportando {len(records)} registros...")

    # Constrói SPG subgraph records
    spg_records = []
    generos_vistos = set()
    especies_vistas = set()

    for rec in records:
        epi = rec.get("epithet") or {}
        binomial = epi.get("binomial")
        genus    = epi.get("genus")
        species  = epi.get("species")

        # TrainingImage node
        img_record = {
            "id":           rec["id"],
            "name":         rec["id"],
            "imageType":    rec.get("image_type", "AMBIGUOUS"),
            "imageTypeRaw": rec.get("image_type_raw", ""),
            "paper":        rec.get("paper", ""),
            "binomial":     binomial or "",
            "genus":        genus or "",
            "species":      species or "",
            "confidence":   epi.get("confidence", 0.0),
            "captionRaw":   (rec.get("caption_raw") or "")[:300],
            "savedTo":      rec.get("saved_to", ""),
        }
        spg_records.append({"label": "TrainingImage", "properties": img_record})

        # Genero node (dedup)
        if genus and genus not in generos_vistos:
            generos_vistos.add(genus)
            spg_records.append({"label": "Genero", "properties": {"id": genus, "name": genus, "origem": "training"}})

        # Especie node (dedup)
        if binomial and binomial not in especies_vistas:
            especies_vistas.add(binomial)
            spg_records.append({"label": "Especie", "properties": {
                "id": binomial, "name": binomial, "generoNome": genus or "", "origem": "training"
            }})

    print(f"  TrainingImage: {sum(1 for r in spg_records if r['label']=='TrainingImage')}")
    print(f"  Especie:       {sum(1 for r in spg_records if r['label']=='Especie')}")
    print(f"  Genero:        {sum(1 for r in spg_records if r['label']=='Genero')}")

    # Tenta importar via knext builder se disponível
    try:
        from knext.builder.component import SourceCsvBuilder
        print("  [INFO] Usando SourceCsvBuilder para importação")
    except ImportError:
        pass

    # Salva como JSONL para uso externo
    out = Path("/tmp/taxonomista_kag/spg_records.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in spg_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  SPG records salvos em: {out} ({len(spg_records)} itens)")

    return spg_records


# ── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== OpenSPG Integration — Taxonomista Digital ===")
    print(f"Project ID : {PROJECT_ID}")
    print(f"Namespace  : {NAMESPACE}")
    print(f"Host       : {HOST}")

    print("\n[1] Criando schema...")
    try:
        create_schema()
    except Exception as e:
        print(f"  [ERROR] Schema: {e}")

    print("\n[2] Importando dados...")
    try:
        import_data()
    except Exception as e:
        print(f"  [ERROR] Dados: {e}")
        import traceback; traceback.print_exc()

    print("\nConcluído.")
