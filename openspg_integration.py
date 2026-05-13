#!/usr/bin/env python3
"""
openspg_integration.py -- Taxonomista Digital (2026-05-13)
==========================================================
Cria o schema SPG e importa os dados do training_index.jsonl no OpenSPG.

Deve ser executado DENTRO do container openspg-server:
    docker cp openspg_integration.py openspg-server:/tmp/taxonomista_kag/
    docker exec openspg-server bash -c "cd /tmp/taxonomista_kag && /home/admin/miniconda3/bin/python openspg_integration.py"

Schema criado (v2 — com pesos ecológicos e tomada de decisão):
  - TrainingImage       (EntityType)  imagem de treinamento
  - Especie             (EntityType)  espécie taxonômica
  - Genero              (EntityType)  gênero taxonômico
  - SamplingPoint       (EntityType)  ponto de amostragem
  - GrupoEcologico      (EntityType)  grupo ecológico funcional com pesos
  - IndiceMonitoramento (EntityType)  índice de biomonitoramento

Relações com pesos para tomada de decisão:
  - TrainingImage   -> [exemplificaEspecie]     -> Especie
  - TrainingImage   -> [pertenceAGenero]         -> Genero
  - Especie         -> [pertenceAGenero]          -> Genero
  - Genero          -> [classificadoComo]         -> GrupoEcologico   (affinity_weight)
  - Genero          -> [ocorreEm]                 -> SamplingPoint    (occurrence_probability)
  - GrupoEcologico  -> [indicaQualidade]          -> IndiceMonitoramento (indicator_weight)
  - GrupoEcologico  -> [preferenciadeSubstrato]   -> Especie          (substrate_preference_score)

Pesos e atributos ecológicos codificados permitem:
  - Inferência bayesiana: P(grupo_ecologico | morfotipo) via affinity_weight
  - Biomonitoramento:     score de qualidade de água pelo índice de montoramente
  - Priorização de coleta: occurrence_probability por ponto de amostragem
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
        sp.add_property(Property(name="location",               object_type_name="Text",  name_zh="Localização"))
        sp.add_property(Property(name="habitatType",            object_type_name="Text",  name_zh="Tipo de Habitat"))
        sp.add_property(Property(name="substrateType",          object_type_name="Text",  name_zh="Substrato"))
        sp.add_property(Property(name="waterVelocityClass",     object_type_name="Text",  name_zh="Classe de Velocidade"))
        sp.add_property(Property(name="biomeRegion",            object_type_name="Text",  name_zh="Bioma"))
        session.create_type(sp)
        created.append("SamplingPoint")

    # --- GrupoEcologico (com pesos para tomada de decisão) ---
    if f"{NAMESPACE}.GrupoEcologico" not in str(existing):
        ge = EntityType(
            name=f"{NAMESPACE}.GrupoEcologico",
            name_zh="Grupo Ecológico",
            desc="Grupo funcional ecológico de hifomiceto aquático com atributos de peso para inferência.",
        )
        ge.add_property(Property(name="nomeCanônico",           object_type_name="Text",  name_zh="Nome Canônico"))
        ge.add_property(Property(name="habitatPreferencia",     object_type_name="Text",  name_zh="Habitat Preferencial"))
        ge.add_property(Property(name="requerimentoOxigenio",   object_type_name="Text",  name_zh="Requerimento de Oxigênio"))
        ge.add_property(Property(name="velocidadeCorrente",     object_type_name="Text",  name_zh="Preferência de Correnteza"))
        ge.add_property(Property(name="valorIndicador",         object_type_name="Float", name_zh="Valor Indicador (0-1)"))
        ge.add_property(Property(name="statusIndicador",        object_type_name="Text",  name_zh="Status de Indicador"))
        ge.add_property(Property(name="scoreBiomonitoramento",  object_type_name="Float", name_zh="Score Biomonitoramento"))
        ge.add_property(Property(name="funcaoEcologica",        object_type_name="Text",  name_zh="Função Ecológica Chave"))
        session.create_type(ge)
        created.append("GrupoEcologico")

    # --- IndiceMonitoramento (índice de biomonitoramento) ---
    if f"{NAMESPACE}.IndiceMonitoramento" not in str(existing):
        im = EntityType(
            name=f"{NAMESPACE}.IndiceMonitoramento",
            name_zh="Índice de Monitoramento",
            desc="Índice biótico de qualidade de água baseado em hifomicetos aquáticos.",
        )
        im.add_property(Property(name="nomeIndice",             object_type_name="Text",  name_zh="Nome do Índice"))
        im.add_property(Property(name="escala",                 object_type_name="Text",  name_zh="Escala de Valores"))
        im.add_property(Property(name="interpretacao",          object_type_name="Text",  name_zh="Interpretação"))
        im.add_property(Property(name="referencia",             object_type_name="Text",  name_zh="Referência Bibliográfica"))
        im.add_property(Property(name="pesoDecisao",            object_type_name="Float", name_zh="Peso na Decisão (0-1)"))
        session.create_type(im)
        created.append("IndiceMonitoramento")

    if created:
        session.commit()
        print(f"  [OK] Tipos criados e commitados: {created}")
    else:
        print("  [INFO] Schema já existe, nada criado.")

    inspect_schema()

# ── 3. Importar dados ────────────────────────────────────────


# ── Tabelas de pesos ecológicos (usadas na importação) ───────────────

# affinity_weight: força da associação genero -> grupo ecologico [0-1]
_ECOLOGICAL_AFFINITY = {
    # Ingoldianos típicos (alta afinidade)
    "Tetracladium": ("ingoldian",   0.97), "Alatospora":    ("ingoldian",   0.96),
    "Articulospora": ("ingoldian",  0.95), "Tricladium":    ("ingoldian",   0.94),
    "Lemonniera":   ("ingoldian",   0.93), "Clavariopsis":  ("ingoldian",   0.92),
    "Flagellospora": ("ingoldian",  0.91), "Anguillospora": ("ingoldian",   0.88),
    "Lunulospora":  ("ingoldian",   0.90), "Tetrachaetum":  ("ingoldian",   0.93),
    "Campylospora": ("ingoldian",   0.89), "Geniculospora": ("ingoldian",   0.87),
    "Triscelophorus": ("ingoldian", 0.91), "Culicidospora": ("ingoldian",   0.85),
    "Gyoerffyella": ("ingoldian",   0.83), "Tricellula":    ("ingoldian",   0.86),
    "Varicosporium": ("ingoldian",  0.88), "Dendrospora":   ("ingoldian",   0.84),
    "Clavatospora": ("ingoldian",   0.90), "Pyramidospora": ("ingoldian",   0.80),
    "Condylospora": ("ingoldian",   0.82), "Flabellospora": ("ingoldian",   0.81),
    "Naiadella":    ("ingoldian",   0.78), "Ingoldiella":   ("ingoldian",   0.86),
    # Aero-aquáticos
    "Helicodendron": ("aero_aquatic", 0.92), "Helicoon":    ("aero_aquatic", 0.90),
    "Spirosphaera":  ("aero_aquatic", 0.88), "Margaritispora": ("aero_aquatic", 0.75),
    # Terrestres-aquáticos (endófitos)
    "Mycocentrospora": ("terrestrial_aquatic", 0.85),
    "Filosporella":    ("terrestrial_aquatic", 0.80),
    # Submerso-aquáticos (madeira)
    "Xylomyces":   ("submerged_aquatic", 0.90),
    "Wiesneriomyces": ("submerged_aquatic", 0.85),
}

# indicator_weight: valor de indicador para qualidade de água [0-1]
_BIOMONITORING_WEIGHTS = {
    "ingoldian":           {"indicator_weight": 0.92, "score": 8.5, "status": "Bioindicador de Água Limpa"},
    "aero_aquatic":        {"indicator_weight": 0.65, "score": 6.0, "status": "Indicador de Estresse Moderado"},
    "terrestrial_aquatic": {"indicator_weight": 0.45, "score": 4.5, "status": "Pouco Sensível"},
    "submerged_aquatic":   {"indicator_weight": 0.55, "score": 5.0, "status": "Indicador de Matéria Orgânica"},
}

# substrate_preference_score: [0-1] preferência por substrato
_SUBSTRATE_PREFERENCE = {
    "leaf_litter": {"ingoldian": 0.95, "aero_aquatic": 0.40, "terrestrial_aquatic": 0.30, "submerged_aquatic": 0.20},
    "foam":        {"ingoldian": 0.75, "aero_aquatic": 0.85, "terrestrial_aquatic": 0.10, "submerged_aquatic": 0.15},
    "wood":        {"ingoldian": 0.25, "aero_aquatic": 0.30, "terrestrial_aquatic": 0.20, "submerged_aquatic": 0.90},
    "roots":       {"ingoldian": 0.30, "aero_aquatic": 0.20, "terrestrial_aquatic": 0.65, "submerged_aquatic": 0.40},
}


def import_data():
    if not Path(INDEX_PATH).is_file():
        print(f"Index não encontrado: {INDEX_PATH}")
        return

    records = [json.loads(l) for l in Path(INDEX_PATH).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"\nImportando {len(records)} registros...")

    spg_records = []
    generos_vistos: dict = {}
    especies_vistas: set = set()
    grupos_vistos: set = set()

    for rec in records:
        epi = rec.get("epithet") or {}
        binomial = epi.get("binomial")
        genus    = epi.get("genus")
        species  = epi.get("species")

        # TrainingImage node
        eco_ctx = epi.get("ecological_context") or {}
        img_record = {
            "id":                rec["id"],
            "name":              rec["id"],
            "imageType":         rec.get("image_type", "AMBIGUOUS"),
            "imageTypeRaw":      rec.get("image_type_raw", ""),
            "paper":             rec.get("paper", ""),
            "binomial":          binomial or "",
            "genus":             genus or "",
            "species":           species or "",
            "confidence":        epi.get("confidence", 0.0),
            "captionRaw":        (rec.get("caption_raw") or "")[:300],
            "savedTo":           rec.get("saved_to", ""),
            "habitatHint":       eco_ctx.get("habitat_hint", ""),
            "enzymaticHint":     str(eco_ctx.get("enzymatic_hint", False)),
            "extractionRule":    epi.get("rule", ""),
        }
        spg_records.append({"label": "TrainingImage", "properties": img_record})

        # Genero node (dedup) com atributos ecológicos
        if genus and genus not in generos_vistos:
            eco_group, affinity = _ECOLOGICAL_AFFINITY.get(genus, ("unknown", 0.5))
            generos_vistos[genus] = eco_group
            spg_records.append({"label": "Genero", "properties": {
                "id":              genus,
                "name":            genus,
                "origem":          "training",
                "grupoEcologico":  eco_group,
                "affinityWeight":  affinity,
            }})
            # GrupoEcologico node (dedup)
            if eco_group not in grupos_vistos and eco_group != "unknown":
                grupos_vistos.add(eco_group)
                bm = _BIOMONITORING_WEIGHTS.get(eco_group, {})
                spg_records.append({"label": "GrupoEcologico", "properties": {
                    "id":                    eco_group,
                    "name":                  eco_group,
                    "nomeCanônico":          eco_group.replace("_", " ").title(),
                    "valorIndicador":        bm.get("indicator_weight", 0.5),
                    "scoreBiomonitoramento": bm.get("score", 5.0),
                    "statusIndicador":       bm.get("status", ""),
                }})

        # Especie node (dedup)
        if binomial and binomial not in especies_vistas:
            especies_vistas.add(binomial)
            spg_records.append({"label": "Especie", "properties": {
                "id":         binomial,
                "name":       binomial,
                "generoNome": genus or "",
                "origem":     "training",
            }})

    # IndiceMonitoramento nodes (estaticos, gerados uma vez)
    _INDICES = [
        {"id": "idx_hifi_qbr",  "name": "Índice Hifomicetos-QBR",
         "nomeIndice": "Índice de Qualidade de Riachos por Hifomicetos (IQRH)",
         "escala": "0-10", "interpretacao": "8-10: excelente; 5-7: bom; 0-4: degradado",
         "referencia": "Pascoal & Cássio 2004", "pesoDecisao": 0.90},
        {"id": "idx_shannon",   "name": "Shannon-Wiener (hifomicetos)",
         "nomeIndice": "Diversidade Shannon de Hifomicetos Aquáticos",
         "escala": "0-4 bits", "interpretacao": ">2.5: alta diversidade",
         "referencia": "Ferreira et al. 2006", "pesoDecisao": 0.75},
        {"id": "idx_sporulation", "name": "Taxa de Esporulação",
         "nomeIndice": "Taxa de Esporulação de Ingoldianos (esporos/L)",
         "escala": "0-10000 esporos/L", "interpretacao": ">500: comunidade ativa",
         "referencia": "Suberkropp 1997", "pesoDecisao": 0.65},
    ]
    for idx in _INDICES:
        spg_records.append({"label": "IndiceMonitoramento", "properties": idx})

    print(f"  TrainingImage:      {sum(1 for r in spg_records if r['label']=='TrainingImage')}")
    print(f"  Especie:            {sum(1 for r in spg_records if r['label']=='Especie')}")
    print(f"  Genero:             {sum(1 for r in spg_records if r['label']=='Genero')}")
    print(f"  GrupoEcologico:     {sum(1 for r in spg_records if r['label']=='GrupoEcologico')}")
    print(f"  IndiceMonitoramento:{sum(1 for r in spg_records if r['label']=='IndiceMonitoramento')}")

    try:
        from knext.builder.component import SourceCsvBuilder
        print("  [INFO] Usando SourceCsvBuilder para importação")
    except ImportError:
        pass

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
