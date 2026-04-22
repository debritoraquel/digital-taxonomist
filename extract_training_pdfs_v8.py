#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
extract_training_pdfs_v8.py -- Taxonomista Digital (validacao 2026-04-20)
==========================================================================
FASE 1 -- Treinamento v8 (base adequada aos grafos).

Integra:
  - v7 extractor (watershed + grid_split + multi-strategy legend parser)
  - image_type_classifier (DRAWING / MICROSCOPY / AMBIGUOUS)
  - pipeline atual (sidecar JSON + filename canonico)
  - Arquitetura com TRES pilares de robustez:

    1) PROPAGACAO DE INCERTEZA
       Cada identificacao taxonomica recebe:
         - resolution.confidence  [0..1]
         - resolution.strategy    qual regra disparou
         - resolution.candidates  demais binomios concorrentes (para desempate
                                  downstream via motor bayesiano)
       Idea: OCR em PDF antigo eh argila com pedra. O motor bayesiano
       precisa saber o peso de cada evidencia.

    2) REPRODUTIBILIDADE (modo offline hermetico)
       Zero chamadas de API. O vocabulario controlado vem de:
         (a) --vocab-snapshot arquivo.json   (snapshot congelado com hash)
         (b) --genera-db   path.sqlite       (SQLite local)
         (c) --neo4j-bolt ...                (futuro: Neo4j/openSPG hook)
         (d) fallback embutido               (81 generos ingoldianos)
       Cada sidecar carrega `vocab_snapshot_hash` para rastreabilidade.

    3) MIGRACAO POR DELTAS
       Cada sidecar tem `schema_version`. Migradores futuros aplicam
       deltas por versao em vez de wipe-and-reimport.

Output por imagem (canonica, compativel com validate_real_samples.py):
    {paper_slug}__p{page:03d}__fig{idx:02d}__{hash6}.json    (sidecar)
    {paper_slug}__p{page:03d}__fig{idx:02d}__{hash6}__{epi_slug}.png

Uso basico:
    python extract_training_pdfs_v8.py ^
        --src          "D:\MBA\tcc\REFERENCIAS\Chaves com imagem" ^
        --drawings-out "D:\MBA\tcc\treinamento_v8_desenho" ^
        --microscopy-out "D:\MBA\tcc\treinamento_v8_lamina" ^
        --ambiguous-out  "D:\MBA\tcc\treinamento_v8_ambiguo" ^
        --vocab-snapshot "D:\MBA\tcc\data\vocab_snapshot_2026_04_21.json" ^
        --min-width 120 --min-height 120

Dependencias: PyMuPDF, Pillow, numpy, opencv-python
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import fitz                                              # PyMuPDF
import numpy as np
from PIL import Image

# Dependencias locais (mesma pasta)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_type_classifier import classify, ClassificationResult  # noqa: E402

try:
    import cv2
except ImportError as e:  # pragma: no cover
    raise SystemExit("Falta opencv-python. pip install opencv-python") from e


SCHEMA_VERSION = "taxonomista.training.v8.1"
PIPELINE_VERSION = "extract_training_pdfs_v8/1.0.0"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_v8")


# =============================================================================
# CONFIG
# =============================================================================

CFG = {
    # Segmentacao
    "merge_threshold":   40,
    "min_crop_w":        60,
    "min_crop_h":        60,
    "min_crop_area":     5000,

    # Watershed
    "merged_area_ratio":  4,
    "merged_extent":      0.30,
    "ws_dist_frac":       0.25,
    "ws_min_marker":      300,

    # Matching legenda <-> desenho
    "max_label_dist":    250,

    # Crop e salvamento
    "crop_padding":       20,
    "min_img_px":        300,
}


# =============================================================================
# REGEXES
# =============================================================================

RE_LEGEND_STRICT = re.compile(
    r"(?:^|[.;]\s*|,\s+)"
    r"(\d{1,3})"
    r"\s*[.)\-\u2013:]\s*"
    r"([A-Z][a-z]{2,20}\s+[a-z][a-z\-]{2,25})",
    re.MULTILINE,
)
RE_LEGEND_LOOSE = re.compile(
    r"(\d{1,3})\s*[.,:)\-\u2013]\s*"
    r"([A-Z][a-z]{2,20})\s+([a-z][a-z\-]{2,25})"
)
RE_FIGURE_CAPTION = re.compile(
    r"(?:Figs?\.?|Figures?|Plate|Prancha)\s*"
    r"[\d.,\-\u2013\s]+[.:\-\u2013]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)
RE_CAPTION_PAIRS = re.compile(
    r"(\d{1,3})\s*[.,;:)\-\u2013]\s*"
    r"([A-Z][a-z]{2,20})\s+([a-z][a-z\-]{2,25})"
)
RE_BINOMIAL = re.compile(
    r"\b([A-Z][a-z]{2,20})\s+([a-z][a-z\-]{2,25})\b"
)
RE_ABBREVIATED = re.compile(
    r"(\d{1,3})\s*[.,;:)\-\u2013]\s*([A-Z])\.\s*([a-z][a-z\-]{2,25})"
)

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def slugify(s: str) -> str:
    s = _SLUG_RE.sub("_", s.strip()).strip("_").lower()
    return s[:80] if len(s) > 80 else s


def short_hash(data: bytes, n: int = 6) -> str:
    return hashlib.sha1(data).hexdigest()[:n]


def json_hash(obj, n: int = 12) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:n]


# =============================================================================
# VOCABULARIO CONTROLADO (PROVIDER)
# =============================================================================
#
# Arquitetura: a v8 nao liga em API ao vivo. Os provedores sao:
#     1. JsonSnapshotProvider    -- arquivo JSON congelado (offline-first)
#     2. SqliteProvider          -- SQLite do pipeline
#     3. FallbackProvider        -- 81 generos ingoldianos embutidos
#     4. (futuro) Neo4jProvider  -- Bolt driver contra Neo4j/openSPG
#
# Todos expoem a mesma API:
#     .is_known_genus(name) -> bool
#     .snapshot_hash() -> str
#     .size() -> int

_NON_GENERA = {
    "Figure", "Table", "Plate", "Appendix", "Section", "Species", "Genus",
    "Family", "Order", "Class", "Phylum", "Kingdom", "Author", "Study",
    "Results", "Methods", "Discussion", "Conclusion", "Introduction",
    "Abstract", "References", "Notes", "Scale", "Bar", "Fig", "Figs",
    "Supplementary", "Supporting", "Material", "Supplemental",
    "This", "These", "Their", "From", "With", "Also", "Such", "Both",
    "Some", "Many", "Most", "Other", "Each", "Type", "New", "All", "Its",
    "The", "For", "Are", "Was", "Were", "Has", "Have", "Had", "Can", "May",
    "Should", "Would", "Could", "Does", "Did", "Been", "Being", "Into",
    "Over", "Under", "After", "Before", "Between", "During", "Within",
    "However", "Therefore", "Furthermore", "Moreover", "Although",
    "Based", "Using", "Used", "Among", "Along", "Around", "Upon",
    "Including", "Excluding", "According", "Respectively", "Previously",
    "Combined", "Shown", "Given", "Found", "Known", "Made", "Note",
    "Taxonomy", "Systematics", "Phylogeny", "Phylogenetic",
    "Molecular", "Morphology", "Morphological", "Analysis", "Analyses",
    "Isolates", "Isolate", "Strains", "Strain", "Cultures", "Culture",
    "Fungal", "Fungus", "Fungi", "Ascomycota", "Basidiomycota",
    "European", "American", "Brazilian", "Asian", "African", "Australian",
    "North", "South", "East", "West", "Central", "Upper", "Lower",
    "Novel", "Unknown", "Undescribed", "Sexual", "Asexual",
    "Collection", "Collections", "Specimen", "Specimens", "Voucher",
    "Generic", "Similar", "Different", "Present", "Recent", "Current",
    "Several", "Various", "Multiple", "Single", "Large", "Small",
    "Long", "Short", "Wide", "Narrow", "Dark", "Light", "White", "Black",
    "Hyaline", "Septate", "Branched", "Simple", "Complex", "Typical",
    "Common", "Rare", "Frequent", "Abundant", "Diverse",
    "Important", "Notable", "Distinct", "Unique", "Characteristic",
    "Spores", "Conidia", "Conidial", "Sample", "Samples",
    "Colonies", "Colony", "Conidiophores", "Mycelium", "Hyphae",
    "Average", "General", "Chemical", "Download", "Downloaded",
    "Biotic", "Parasitic", "Saprophytic", "Heterotrophic",
    "Despite", "While", "Content", "Minimum", "Maximum",
    "Article", "History", "Academic", "Editor", "Open", "Source",
    "Image", "Data", "Bootstrap", "Neighbour", "Joining",
    "Relationship", "Configured", "Solutions", "Statistical",
    "Cluster", "Monthly", "Variations", "Nickel", "Sorensen",
    "Pooled", "Mere", "Exposure", "Substantive", "Springer",
    "Successful", "Primer", "Major", "Milestones", "Conjugate",
    "Formation", "Ingoldian", "Ingold", "Microscopic", "Structures",
    "Blue", "Periascal", "Immature", "Newly", "Generated",
    "Products", "Pubs", "Kappa", "Values", "Mosaic", "When",
    "Here", "Only", "Incertae", "Sedis", "Even", "Though",
    "Total", "Number", "Dimethyl", "Phthalate", "World", "Map",
    "Cladogram", "Showing", "Phylogram", "Unethical", "Practices",
    "Seven", "Nannfeldt", "Erected", "Foot", "Cells",
    "Parsimony", "Emended", "Description",
    "Forest", "Haridwar", "River", "Asan", "Conservation",
    "Wetland", "Limnology", "Environmental",
    "Esta", "Este", "Para", "Como", "Mais", "Foram", "Sendo", "Ainda",
}

_INVALID_EPITHETS = {
    "and", "the", "was", "were", "are", "for", "from", "with",
    "had", "has", "have", "been", "not", "but", "can", "may",
    "data", "into", "also", "such", "which", "when", "than",
    "recognized", "pubs", "values", "issue", "number",
    "showed", "during", "between", "species", "growing",
    "typically", "effuse", "reduced", "cells", "body",
    "elements", "axis", "branches", "conidia", "fungi",
    "interactions", "structures", "milestones", "formation",
    "chemical", "exposure", "difference", "matches",
    "variations", "analysis", "solutions", "history",
    "editor", "source", "practices",
}

# Lista ingoldiana embutida (fallback). 81 generos conforme DB do projeto.
_FALLBACK_INGOLDIAN_GENERA = frozenset([
    "Actinospora", "Alatospora", "Anguillospora", "Articulospora",
    "Campylospora", "Casaresia", "Centrospora", "Ceratopsis",
    "Clavariopsis", "Clavatospora", "Condylospora", "Culicidospora",
    "Cylindrocarpon", "Dactylella", "Dendrospora", "Descalsia",
    "Diplocladiella", "Dwayaangam", "Filosporella", "Flabellocladia",
    "Flagellospora", "Fontanospora", "Geniculospora", "Goniopila",
    "Gyoerffyella", "Heliscella", "Heliscina", "Heliscus",
    "Helicoon", "Helicomyces", "Helicodendron", "Helicosporium",
    "Helicomina", "Helicoma",
    "Hydrometrospora", "Ingoldiella", "Isthmolongispora",
    "Isthmotricladia", "Jaculispora", "Lateriramulosa",
    "Lemonniera", "Leptosporomyces", "Lunulospora",
    "Magdalaenaea", "Margaritispora", "Mycocentrospora",
    "Pleuropedium", "Pseudaegerita", "Pyramidospora",
    "Retiarius", "Scorpiosporium", "Sigmoidea", "Speiropsis",
    "Stenocladiella", "Taeniospora", "Tetrachaetum", "Tetracladium",
    "Tricellula", "Tricladium", "Trinacrium", "Tripospermum",
    "Triposporina", "Triscelophorus", "Tumularia", "Varicosporium",
    "Volucrispora", "Xylomyces",
    "Brachiosphaera", "Camposporidium", "Camposporium",
    "Cancellidium", "Dicranidion", "Helicosporium",
    "Hydrocina", "Mirandina", "Pestalotia", "Piricauda",
    "Pleuropedium", "Riessia", "Spegazzinia", "Trichocladium",
    "Wiesneriomyces",
])


@dataclass
class VocabSnapshot:
    """Metadados imutaveis do snapshot de vocabulario em uso."""
    source: str                      # "json_snapshot" | "sqlite" | "fallback" | "neo4j"
    hash: str                        # hash curto do conjunto de generos
    size: int
    path: Optional[str] = None
    created_at: Optional[str] = None


class VocabularyProvider:
    """API comum. Implementacoes sao offline-first."""

    def __init__(self, known: Sequence[str], meta: VocabSnapshot):
        self._known = {k.strip() for k in known if k and isinstance(k, str)}
        self._meta = meta

    def is_known_genus(self, name: str) -> bool:
        return name in self._known

    def snapshot(self) -> VocabSnapshot:
        return self._meta

    def __len__(self) -> int:
        return len(self._known)

    # ---- factories -------------------------------------------------------

    @classmethod
    def from_json_snapshot(cls, path: Path) -> "VocabularyProvider":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        generos = data.get("genera") or data.get("generos") or []
        h = json_hash(sorted(generos))
        meta = VocabSnapshot(
            source="json_snapshot",
            hash=h,
            size=len(generos),
            path=str(path),
            created_at=data.get("created_at"),
        )
        log.info("Vocab [json]  %d generos  (hash=%s  from %s)", len(generos), h, path.name)
        return cls(generos, meta)

    @classmethod
    def from_sqlite(cls, db_path: Path) -> "VocabularyProvider":
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            # Procura uma tabela plausivel
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            candidate_tables = ["genera", "genus", "taxa"]
            generos: List[str] = []
            for t in candidate_tables:
                if t in tables:
                    try:
                        cur.execute(f"SELECT name FROM {t}")
                        generos = [r[0] for r in cur.fetchall() if r[0]]
                        break
                    except sqlite3.Error:
                        continue
            if not generos:
                raise RuntimeError(f"Nenhuma tabela de generos encontrada em {db_path}")
        finally:
            conn.close()
        h = json_hash(sorted(generos))
        meta = VocabSnapshot(
            source="sqlite", hash=h, size=len(generos), path=str(db_path),
        )
        log.info("Vocab [sqlite] %d generos  (hash=%s)", len(generos), h)
        return cls(generos, meta)

    @classmethod
    def from_fallback(cls) -> "VocabularyProvider":
        generos = sorted(_FALLBACK_INGOLDIAN_GENERA)
        h = json_hash(generos)
        meta = VocabSnapshot(
            source="fallback", hash=h, size=len(generos),
            path="<embedded>",
        )
        log.info("Vocab [fallback] %d generos embutidos (hash=%s)", len(generos), h)
        return cls(generos, meta)


# =============================================================================
# VALIDACAO DE BINOMIOS (v7 + vocabulario)
# =============================================================================

def _is_valid_genus_syntax(name: str) -> bool:
    if not name:
        return False
    if name in _NON_GENERA:
        return False
    if len(name) < 4:
        return False
    bad_suffixes = ("aceae", "ales", "idae", "inae", "oideae",
                    "mycota", "mycetes", "ing", "tion", "sion",
                    "ment", "ness", "less", "ance", "ence",
                    "ical", "ular", "ious", "eous", "able")
    if name.endswith(bad_suffixes):
        return False
    if not name[0].isupper():
        return False
    if not all(c.islower() or c == "-" for c in name[1:]):
        return False
    return True


def _is_valid_epithet(epithet: str) -> bool:
    if len(epithet) < 3:
        return False
    if not all(c.islower() or c == "-" for c in epithet):
        return False
    if epithet in _INVALID_EPITHETS:
        return False
    return True


def is_valid_binomial(genus: str, epithet: str, vocab: VocabularyProvider,
                     require_known_genus: bool = True) -> bool:
    """
    Valida binomio. Se `require_known_genus` for True, o genero precisa estar
    no vocabulario controlado. Caso contrario, so exige sintaxe taxonomica.
    """
    if not _is_valid_genus_syntax(genus):
        return False
    if not _is_valid_epithet(epithet):
        return False
    if require_known_genus and not vocab.is_known_genus(genus):
        return False
    return True


# =============================================================================
# PARSING MULTI-ESTRATEGIA COM CONFIANCA
# =============================================================================

# Confianca por estrategia. Sera gravada no sidecar.
_STRATEGY_CONFIDENCE = {
    "legend_strict":       0.95,   # "15. Tripospermum camelopardus"
    "legend_loose":        0.85,   # "15, Genus epithet"
    "caption_pairs":       0.80,   # "Figures 11-27. ... 11, Gyoerffyella ..."
    "abbreviated":         0.70,   # "12. G. biappendiculata"
    "adjacent_pages":      0.65,   # mesma estrategia, mas em paginas vizinhas
    "proximity_binomial":  0.55,   # binomio completo proximo do bbox
    "page_fallback":       0.40,   # primeiro binomio valido na pagina
    "pdf_filename":        0.15,   # ultimo recurso
}


@dataclass
class LegendHit:
    number: int
    binomial: str
    strategy: str
    confidence: float


def parse_legend_multi(full_page_text: str,
                       vocab: VocabularyProvider,
                       adjacent_pages: Optional[List[str]] = None
                       ) -> Dict[int, LegendHit]:
    """
    Retorna mapa {numero -> LegendHit} com estrategia e confianca por entrada.
    """
    mapa: Dict[int, LegendHit] = {}

    def _try_add(num: int, genus: str, epithet: str, strategy: str):
        if num in mapa:
            return
        # PATCH v8.1: epiteto curto (<5 chars) geralmente e artefato de kerning
        # do PDF (ex.: "Articulospora mon" <- "monotheca", "Campylospora
        # chaetoc" <- "chaetocladia"). Rejeita mesmo em legend_strict.
        if len(epithet) < 5:
            return
        # Aceita genero fora do vocab como candidato de baixa confianca
        known = vocab.is_known_genus(genus)
        if not is_valid_binomial(genus, epithet, vocab, require_known_genus=False):
            return
        conf = _STRATEGY_CONFIDENCE[strategy]
        if not known:
            conf *= 0.55   # penalidade por genero fora do vocabulario
        mapa[num] = LegendHit(
            number=num,
            binomial=f"{genus} {epithet}",
            strategy=strategy,
            confidence=round(conf, 3),
        )

    # Estrategia 1: regex rigoroso
    for m in RE_LEGEND_STRICT.finditer(full_page_text):
        num = int(m.group(1))
        especie = m.group(2).strip()
        parts = especie.split()
        if len(parts) >= 2:
            _try_add(num, parts[0], parts[1], "legend_strict")

    # Estrategia 2: regex relaxado
    if len(mapa) < 2:
        for m in RE_LEGEND_LOOSE.finditer(full_page_text):
            num = int(m.group(1))
            _try_add(num, m.group(2), m.group(3), "legend_loose")

    # Estrategia 3: caption completo
    if len(mapa) < 2:
        for cap_m in RE_FIGURE_CAPTION.finditer(full_page_text):
            caption_text = cap_m.group(1)
            for m in RE_CAPTION_PAIRS.finditer(caption_text):
                num = int(m.group(1))
                _try_add(num, m.group(2), m.group(3), "caption_pairs")

    # Estrategia 4: abreviadas ("G. biappendiculata")
    if mapa:
        genera = [hit.binomial.split()[0] for hit in mapa.values()]
        if genera:
            most_common = Counter(genera).most_common(1)[0][0]
            for m in RE_ABBREVIATED.finditer(full_page_text):
                num = int(m.group(1))
                initial = m.group(2)
                epithet = m.group(3)
                if num not in mapa and most_common[0] == initial:
                    _try_add(num, most_common, epithet, "abbreviated")

    # Estrategia 5: paginas adjacentes (fallback)
    if not mapa and adjacent_pages:
        for pg_text in adjacent_pages:
            for m in RE_LEGEND_STRICT.finditer(pg_text):
                num = int(m.group(1))
                parts = m.group(2).strip().split()
                if len(parts) >= 2:
                    _try_add(num, parts[0], parts[1], "adjacent_pages")

    return mapa


# =============================================================================
# SEGMENTACAO (watershed + grid_split) - v7 port
# =============================================================================

def merge_bounding_boxes(boxes, threshold=None):
    if not boxes:
        return []
    threshold = threshold or CFG["merge_threshold"]
    rects = [[b[0], b[1], b[0] + b[2], b[1] + b[3]] for b in boxes]

    def should_merge(r1, r2):
        r1_exp = [r1[0] - threshold, r1[1] - threshold,
                  r1[2] + threshold, r1[3] + threshold]
        return (r1_exp[0] < r2[2] and r1_exp[2] > r2[0] and
                r1_exp[1] < r2[3] and r1_exp[3] > r2[1])

    changed = True
    while changed:
        changed = False
        new_rects: List[List[int]] = []
        while rects:
            r1 = rects.pop(0)
            merged = False
            for i, r2 in enumerate(new_rects):
                if should_merge(r1, r2):
                    new_rects[i] = [min(r1[0], r2[0]), min(r1[1], r2[1]),
                                    max(r1[2], r2[2]), max(r1[3], r2[3])]
                    merged = True
                    changed = True
                    break
            if not merged:
                new_rects.append(r1)
        rects = new_rects
    return [(r[0], r[1], r[2] - r[0], r[3] - r[1]) for r in rects]


def _find_gap_centers(projection, threshold, min_gap=8):
    n = len(projection)
    is_gap = projection <= threshold
    gaps = []
    in_gap = False
    gap_start = 0
    for i in range(n):
        if is_gap[i] and not in_gap:
            gap_start = i
            in_gap = True
        elif not is_gap[i] and in_gap:
            gap_len = i - gap_start
            if gap_len >= min_gap:
                gaps.append(gap_start + gap_len // 2)
            in_gap = False
    if in_gap:
        gap_len = n - gap_start
        if gap_len >= min_gap:
            gaps.append(gap_start + gap_len // 2)
    return gaps


def _watershed_split(clean, mask):
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return None
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    roi = mask[y1:y2, x1:x2]
    roi_clean = cv2.bitwise_and(clean[y1:y2, x1:x2], roi)
    dist = cv2.distanceTransform(roi_clean, cv2.DIST_L2, 5)
    if dist.max() < 2:
        return None
    thresh_val = max(dist.max() * CFG["ws_dist_frac"], 2.0)
    _, sure_fg = cv2.threshold(dist, thresh_val, 255, 0)
    sure_fg = np.uint8(sure_fg)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    sure_fg = cv2.morphologyEx(sure_fg, cv2.MORPH_OPEN, kernel)
    n_markers, markers = cv2.connectedComponents(sure_fg, connectivity=8)
    if n_markers <= 2:
        return None
    for m in range(1, n_markers):
        if np.count_nonzero(markers == m) < CFG["ws_min_marker"]:
            markers[markers == m] = 0
    markers_clean = np.zeros_like(markers)
    new_id = 1
    for m in range(1, n_markers):
        if np.any(markers == m):
            markers_clean[markers == m] = new_id
            new_id += 1
    if new_id <= 2:
        return None
    markers_ws = markers_clean + 1
    sure_bg = cv2.dilate(roi_clean, kernel, iterations=5)
    unknown = cv2.subtract(sure_bg, sure_fg)
    markers_ws[unknown == 255] = 0
    roi_color = cv2.cvtColor(roi_clean, cv2.COLOR_GRAY2BGR)
    markers_ws = cv2.watershed(roi_color, markers_ws.astype(np.int32))
    results = []
    for m_id in range(2, new_id + 1):
        mask_m = (markers_ws == m_id)
        if np.count_nonzero(mask_m) < 300:
            continue
        ys_m, xs_m = np.where(mask_m)
        bx1 = int(xs_m.min()) + x1
        by1 = int(ys_m.min()) + y1
        bx2 = int(xs_m.max()) + x1
        by2 = int(ys_m.max()) + y1
        bw, bh = bx2 - bx1, by2 - by1
        if bw >= 40 and bh >= 40:
            results.append((bx1, by1, bw, bh))
    return results if results else None


def _grid_split(clean, mask):
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return None
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    roi = cv2.bitwise_and(clean[y1:y2, x1:x2], mask[y1:y2, x1:x2])
    h, w = roi.shape
    if h < 100 or w < 100:
        return None
    col_proj = np.sum(roi > 0, axis=0).astype(float)
    row_proj = np.sum(roi > 0, axis=1).astype(float)
    k = max(5, int(min(w, h) * 0.015))
    if k % 2 == 0:
        k += 1
    col_smooth = np.convolve(col_proj, np.ones(k) / k, mode="same")
    row_smooth = np.convolve(row_proj, np.ones(k) / k, mode="same")
    col_thresh = max(np.max(col_smooth) * 0.05, 1)
    row_thresh = max(np.max(row_smooth) * 0.05, 1)
    min_gap = max(8, int(min(w, h) * 0.01))
    col_gaps = _find_gap_centers(col_smooth, col_thresh, min_gap)
    row_gaps = _find_gap_centers(row_smooth, row_thresh, min_gap)
    if not col_gaps and not row_gaps:
        return None
    col_bounds = [0] + col_gaps + [w]
    row_bounds = [0] + row_gaps + [h]
    results = []
    min_cell_ink = max(1500, int(h * w * 0.001))
    for ri in range(len(row_bounds) - 1):
        for ci in range(len(col_bounds) - 1):
            ry1 = row_bounds[ri]
            ry2 = row_bounds[ri + 1]
            cx1 = col_bounds[ci]
            cx2 = col_bounds[ci + 1]
            cell = roi[ry1:ry2, cx1:cx2]
            cell_ink = cv2.countNonZero(cell)
            cell_w = cx2 - cx1
            cell_h = ry2 - ry1
            if cell_ink < min_cell_ink or cell_w < 50 or cell_h < 50:
                continue
            cell_ys, cell_xs = np.where(cell > 0)
            if len(cell_ys) == 0:
                continue
            tx1 = int(cell_xs.min()) + cx1 + x1
            ty1 = int(cell_ys.min()) + ry1 + y1
            tx2 = int(cell_xs.max()) + cx1 + x1
            ty2 = int(cell_ys.max()) + ry1 + y1
            tw, th = tx2 - tx1, ty2 - ty1
            if tw >= 50 and th >= 50:
                results.append((tx1, ty1, tw, th))
    return results if results else None


def segment_conidia(img_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """
    Retorna lista de bboxes (x, y, w, h) de conidios individuais.
    Aplica watershed + grid_split para separar sobrepostos.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h_img, w_img = img_bgr.shape[:2]
    dpi_est = max(w_img / 8.5, h_img / 11.7, 72)
    char_h = dpi_est * 14 / 72.0

    _, thresh = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if cv2.countNonZero(thresh) > thresh.size * 0.5:
        thresh = cv2.bitwise_not(thresh)

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        thresh, connectivity=8)
    clean = np.zeros_like(thresh)
    noise_max = max(80, int(char_h * 2))
    text_max_dim = int(char_h * 3.5)
    text_max_area = int(text_max_dim * text_max_dim * 0.8)
    for i in range(1, n_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        cw = stats[i, cv2.CC_STAT_WIDTH]
        ch = stats[i, cv2.CC_STAT_HEIGHT]
        if area < noise_max:
            continue
        max_dim = max(cw, ch)
        elongation = max_dim / max(min(cw, ch), 1)
        solidity = area / max(cw * ch, 1)
        is_text = (area < text_max_area and max_dim < text_max_dim
                   and elongation < 5 and solidity > 0.20)
        if is_text:
            continue
        clean[labels == i] = 255

    n_comp, labels_comp, stats_comp, _ = cv2.connectedComponentsWithStats(
        clean, connectivity=8)
    if n_comp <= 1:
        return []
    min_comp_area = int(char_h * char_h * 3)
    min_comp_dim = int(char_h * 2)
    valid_areas = []
    for i in range(1, n_comp):
        a = stats_comp[i, cv2.CC_STAT_AREA]
        w = stats_comp[i, cv2.CC_STAT_WIDTH]
        h = stats_comp[i, cv2.CC_STAT_HEIGHT]
        if a > min_comp_area and w > min_comp_dim and h > min_comp_dim:
            valid_areas.append(a)
    if not valid_areas:
        return []
    median_area = float(np.median(valid_areas))

    raw_boxes: List[Tuple[int, int, int, int]] = []
    for i in range(1, n_comp):
        area = stats_comp[i, cv2.CC_STAT_AREA]
        cx = stats_comp[i, cv2.CC_STAT_LEFT]
        cy = stats_comp[i, cv2.CC_STAT_TOP]
        cw = stats_comp[i, cv2.CC_STAT_WIDTH]
        ch_c = stats_comp[i, cv2.CC_STAT_HEIGHT]
        if area < min_comp_area or cw < min_comp_dim or ch_c < min_comp_dim:
            continue
        is_full_plate = (cw > w_img * 0.60 and ch_c > h_img * 0.60)
        is_oversized = (
            area > median_area * CFG["merged_area_ratio"] and
            (cw > w_img * CFG["merged_extent"] or
             ch_c > h_img * CFG["merged_extent"])
        )
        if is_full_plate or is_oversized:
            mask_i = (labels_comp == i).astype(np.uint8) * 255
            sub = _watershed_split(clean, mask_i)
            if sub:
                raw_boxes.extend(sub)
            elif is_full_plate:
                sub2 = _grid_split(clean, mask_i)
                if sub2:
                    raw_boxes.extend(sub2)
                else:
                    raw_boxes.append((cx, cy, cw, ch_c))
            else:
                raw_boxes.append((cx, cy, cw, ch_c))
        else:
            raw_boxes.append((cx, cy, cw, ch_c))

    merged = merge_bounding_boxes(raw_boxes)
    result = []
    for (x, y, w, h) in merged:
        if w < CFG["min_crop_w"] or h < CFG["min_crop_h"]:
            continue
        if w * h < CFG["min_crop_area"]:
            continue
        if w > w_img * 0.85 and h > h_img * 0.85:
            continue
        aspect = w / max(h, 1)
        if aspect > 8.0 or aspect < 0.12:
            continue
        result.append((x, y, w, h))
    return result


# =============================================================================
# RESOLUCAO TAXONOMICA POR CONIDIO
# =============================================================================

@dataclass
class Resolution:
    binomial: Optional[str]
    genus: Optional[str]
    epithet: Optional[str]
    strategy: str
    confidence: float
    candidates: List[Dict] = field(default_factory=list)


def _nearest_text_blocks(cx: float, cy: float, text_blocks: List[Dict],
                         limit: int = 8) -> List[Tuple[float, Dict]]:
    out = []
    for tb in text_blocks:
        bbox = tb["bbox"]
        tcx = (bbox[0] + bbox[2]) / 2.0
        tcy = (bbox[1] + bbox[3]) / 2.0
        out.append((math.hypot(tcx - cx, tcy - cy), tb))
    out.sort(key=lambda t: t[0])
    return out[:limit]


def resolve_species(cx_pdf: float, cy_pdf: float,
                    text_blocks: List[Dict],
                    legend_map: Dict[int, LegendHit],
                    page_fallback: Optional[LegendHit],
                    vocab: VocabularyProvider,
                    pdf_path: Path) -> Resolution:
    """
    Cascata de 6 estrategias. Retorna Resolution com confianca calibrada.
    """
    candidates: List[Dict] = []

    blocos = _nearest_text_blocks(cx_pdf, cy_pdf, text_blocks)

    # Estrategia A: numero proximo -> legenda
    if legend_map and blocos:
        for dist, tb in blocos:
            if dist > CFG["max_label_dist"]:
                break
            for n_str in re.findall(r"\b(\d{1,3})\b", tb["text"]):
                n = int(n_str)
                if n in legend_map:
                    hit = legend_map[n]
                    genus, epithet = hit.binomial.split()
                    candidates.append(dict(
                        binomial=hit.binomial, strategy=hit.strategy,
                        confidence=hit.confidence, dist=round(dist, 2)))

    # Estrategia B: binomio valido proximo ao bbox
    # PATCH v8.1: exige genero conhecido. Proximidade sozinha nao e evidencia
    # suficiente para admitir genero fora do vocabulario controlado. Sem esse
    # filtro, pares de palavras comuns ("Seasonal occurrence", "Bauhinia
    # purpurea") eram aceitos como binomios.
    # PATCH v8.1: epiteto curto (<5 chars) em fallback geralmente e artefato
    # de kerning ("Lunulospora cymb" <- "Lunulospora cymbiformis" quebrado).
    for dist, tb in blocos:
        if dist > CFG["max_label_dist"]:
            break
        for m in RE_BINOMIAL.finditer(tb["text"]):
            g, e = m.group(1), m.group(2)
            if len(e) < 5:
                continue
            if is_valid_binomial(g, e, vocab, require_known_genus=True):
                conf = _STRATEGY_CONFIDENCE["proximity_binomial"]
                candidates.append(dict(
                    binomial=f"{g} {e}", strategy="proximity_binomial",
                    confidence=round(conf, 3), dist=round(dist, 2)))

    # Estrategia C: fallback da pagina
    if page_fallback:
        candidates.append(dict(
            binomial=page_fallback.binomial,
            strategy=page_fallback.strategy,
            confidence=page_fallback.confidence, dist=None))

    # Se ha candidatos validos, escolhe por confianca (desempate: menor dist)
    if candidates:
        candidates.sort(key=lambda c: (-c["confidence"],
                                       c.get("dist") or 1e9))
        best = candidates[0]
        g, e = best["binomial"].split()
        return Resolution(
            binomial=best["binomial"],
            genus=g, epithet=e,
            strategy=best["strategy"],
            confidence=best["confidence"],
            candidates=candidates[:5],
        )

    # Estrategia D: nome do PDF (ultimo recurso, confianca minima)
    stem = re.sub(r"[^\w\s\-]", "", pdf_path.stem)
    stem = re.sub(r"[\s\-]+", "_", stem)[:30].rsplit("_", 1)[0]
    return Resolution(
        binomial=None, genus=None, epithet=None,
        strategy="pdf_filename",
        confidence=_STRATEGY_CONFIDENCE["pdf_filename"],
        candidates=[dict(binomial=None, strategy="pdf_filename",
                         confidence=_STRATEGY_CONFIDENCE["pdf_filename"],
                         note=f"PDF stem={stem}")],
    )


def resolve_page_fallback(full_text: str,
                          vocab: VocabularyProvider) -> Optional[LegendHit]:
    """Primeiro binomio valido na pagina (fallback de baixa confianca).

    PATCH v8.1: exige genero conhecido no vocabulario. Sem esse filtro, o
    primeiro par de palavras maiusculas da pagina (ex.: "Lignocellulolytic
    Enzyme", "Microbial Biodiversity", "They Question") era aceito como
    binomio. O fallback so e util quando realmente encontra um genero
    Ingoldiano valido dentro do corpo do texto.
    """
    for m in RE_BINOMIAL.finditer(full_text):
        g, e = m.group(1), m.group(2)
        # Epiteto curto = quase sempre artefato de kerning do PDF
        if len(e) < 5:
            continue
        if is_valid_binomial(g, e, vocab, require_known_genus=True):
            conf = _STRATEGY_CONFIDENCE["page_fallback"]
            return LegendHit(
                number=-1, binomial=f"{g} {e}",
                strategy="page_fallback", confidence=round(conf, 3),
            )
    return None


# =============================================================================
# EXTRACAO DE IMAGEM E TEXTO
# =============================================================================

def pil_from_pixmap(pix: "fitz.Pixmap") -> np.ndarray:
    """Pixmap -> numpy BGR."""
    if pix.colorspace and pix.colorspace.n == 4:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    n_ch = pix.n - (1 if pix.alpha else 0)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if n_ch == 1:
        return cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
    if n_ch == 3:
        return cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR)
    if n_ch == 4:
        return cv2.cvtColor(arr[:, :, :4], cv2.COLOR_RGBA2BGR)
    raise ValueError(f"canais inesperados: {n_ch}")


def iter_page_images(doc: "fitz.Document", pno: int,
                     min_px: int) -> List[Dict]:
    """
    Retorna lista de {img_bgr, width, height, pdf_rect, xref} para a pagina.
    Fallback: renderiza pagina inteira se nao houver imagens embutidas.
    """
    page = doc[pno]
    out = []
    seen: set = set()
    for img_tuple in page.get_images(full=True):
        xref = img_tuple[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            pix = fitz.Pixmap(doc, xref)
            img = pil_from_pixmap(pix)
            w_px, h_px = pix.width, pix.height
        except Exception:
            continue
        if w_px < min_px or h_px < min_px:
            continue
        try:
            rects = page.get_image_rects(xref)
            pdf_rect = rects[0] if rects else None
        except Exception:
            pdf_rect = None
        out.append(dict(img=img, width=w_px, height=h_px,
                        pdf_rect=pdf_rect, xref=xref))
    if not out:
        try:
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            img = pil_from_pixmap(pix)
            out.append(dict(img=img, width=pix.width, height=pix.height,
                            pdf_rect=fitz.Rect(page.rect), xref=None))
        except Exception:
            pass
    return out


_RE_HYPHEN_LINEBREAK = re.compile(r"(\w)-\s*\n\s*(\w)")


def _dehyphenate(text: str) -> str:
    """Junta palavras quebradas por hifenizacao no fim da linha.

    PATCH v8.1: PDFs frequentemente quebram binomios como 'Lunulospora
    cymb-\\niformis' -> apos extracao vira 'cymb- iformis', e RE_BINOMIAL
    para em 'cymb' porque `-` rompe o word boundary. Sem esse passo,
    perdemos epitetos longos (cymbiformis, marchalianum, longibrachiata...).
    """
    return _RE_HYPHEN_LINEBREAK.sub(r"\1\2", text)


def page_text_blocks(page: "fitz.Page") -> Tuple[str, List[Dict]]:
    """Retorna (full_text, blocks com bbox) via page.get_text('dict')."""
    full = _dehyphenate(page.get_text("text") or "")
    page_dict = page.get_text("dict") or {}
    blocks = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        content = ""
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                content += span.get("text", "") + " "
        content = _dehyphenate(content.strip())
        if content and len(content) < 500:
            blocks.append({"bbox": block["bbox"], "text": content})
    return full, blocks


# =============================================================================
# PIPELINE PRINCIPAL
# =============================================================================

def process_pdf(pdf: Path, out_draw: Path, out_micro: Path, out_amb: Path,
                vocab: VocabularyProvider, min_w: int, min_h: int,
                dry_run: bool, index_file,
                counts: Dict[str, int]) -> int:
    """Processa 1 PDF. Retorna numero de conidios individuais salvos."""
    try:
        doc = fitz.open(pdf)
    except Exception as e:
        log.error("  Erro ao abrir %s: %s", pdf.name, e)
        return 0

    paper_slug = slugify(pdf.stem)
    total_saved = 0

    all_pages_text: List[str] = []
    for pno in range(len(doc)):
        try:
            all_pages_text.append(doc[pno].get_text("text") or "")
        except Exception:
            all_pages_text.append("")

    try:
        for pno in range(len(doc)):
            page = doc[pno]
            full_text, text_blocks = page_text_blocks(page)

            adjacent: List[str] = []
            if pno > 0:
                adjacent.append(all_pages_text[pno - 1])
            if pno < len(all_pages_text) - 1:
                adjacent.append(all_pages_text[pno + 1])

            legend_map = parse_legend_multi(full_text, vocab, adjacent)
            page_fb = resolve_page_fallback(full_text, vocab)

            imagens = iter_page_images(doc, pno, CFG["min_img_px"])
            if not imagens:
                continue

            fig_idx = 0
            for img_data in imagens:
                img = img_data["img"]
                w_px, h_px = img_data["width"], img_data["height"]
                pdf_rect = img_data["pdf_rect"]

                if pdf_rect is not None:
                    pdf_w = pdf_rect[2] - pdf_rect[0]
                    pdf_h = pdf_rect[3] - pdf_rect[1]
                    scale_x = pdf_w / float(max(w_px, 1))
                    scale_y = pdf_h / float(max(h_px, 1))
                else:
                    scale_x = scale_y = 1.0
                    pdf_rect = (0, 0, w_px, h_px)

                bboxes = segment_conidia(img)
                if not bboxes:
                    # Fallback: usa a imagem inteira como 1 conidio
                    if w_px >= min_w and h_px >= min_h:
                        bboxes = [(0, 0, w_px, h_px)]
                    else:
                        fig_idx += 1
                        continue

                for (x, y, w, h) in bboxes:
                    cx_pdf = pdf_rect[0] + (x + w / 2.0) * scale_x
                    cy_pdf = pdf_rect[1] + (y + h / 2.0) * scale_y

                    pad = CFG["crop_padding"]
                    y1 = max(0, y - pad)
                    y2 = min(img.shape[0], y + h + pad)
                    x1 = max(0, x - pad)
                    x2 = min(img.shape[1], x + w + pad)
                    crop_bgr = img[y1:y2, x1:x2]
                    if crop_bgr.size == 0 or (x2 - x1) < min_w or (y2 - y1) < min_h:
                        continue

                    pil = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))

                    # Classificar tipo
                    cls: ClassificationResult = classify(pil)
                    if cls.image_type == "DRAWING":
                        target = out_draw
                    elif cls.image_type == "MICROSCOPY":
                        target = out_micro
                    else:
                        target = out_amb
                    counts[cls.image_type] = counts.get(cls.image_type, 0) + 1

                    # Resolucao taxonomica com confianca
                    res = resolve_species(
                        cx_pdf, cy_pdf, text_blocks,
                        legend_map, page_fb, vocab, pdf,
                    )

                    if res.binomial:
                        counts["with_binomial"] += 1
                        epi_slug = slugify(res.binomial)
                    elif res.genus:
                        counts["with_genus_only"] += 1
                        epi_slug = slugify(res.genus) + "_sp"
                    else:
                        counts["no_epithet"] += 1
                        epi_slug = "UNKNOWN_UNKNOWN"

                    # ID canonico
                    png_bytes = io.BytesIO()
                    pil.save(png_bytes, format="PNG")
                    h6 = short_hash(png_bytes.getvalue())
                    base_id = f"{paper_slug}__p{pno + 1:03d}__fig{fig_idx:02d}__{h6}"
                    fname = f"{base_id}__{epi_slug}.png"
                    meta_name = f"{base_id}.json"

                    out_img = target / fname
                    out_meta = target / meta_name

                    if not dry_run:
                        pil.save(out_img, format="PNG")
                        meta = {
                            "schema_version": SCHEMA_VERSION,
                            "pipeline_version": PIPELINE_VERSION,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "id": base_id,
                            "paper": pdf.name,
                            "page": pno + 1,
                            "fig_index": fig_idx,
                            "bbox_pixel": [int(x), int(y), int(w), int(h)],
                            "bbox_pdf_points": [
                                float(pdf_rect[0] + x * scale_x),
                                float(pdf_rect[1] + y * scale_y),
                                float(pdf_rect[0] + (x + w) * scale_x),
                                float(pdf_rect[1] + (y + h) * scale_y),
                            ],
                            "image_type": cls.image_type,
                            "classifier": asdict(cls),
                            "resolution": asdict(res),
                            "vocab_snapshot": asdict(vocab.snapshot()),
                            "saved_to": str(out_img),
                        }
                        out_meta.write_text(
                            json.dumps(meta, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        index_file.write(
                            json.dumps(meta, ensure_ascii=False) + "\n")
                        index_file.flush()
                        total_saved += 1

                    log.info(
                        "  [%s conf=%.2f] p%03d fig%02d  %-48s  %s",
                        cls.image_type[:4], res.confidence,
                        pno + 1, fig_idx, epi_slug,
                        f"via {res.strategy}",
                    )

                fig_idx += 1
    finally:
        doc.close()

    return total_saved


# =============================================================================
# CLI
# =============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Pasta com PDFs de treino")
    ap.add_argument("--drawings-out", required=True)
    ap.add_argument("--microscopy-out", required=True)
    ap.add_argument("--ambiguous-out", default=None)
    ap.add_argument("--vocab-snapshot", default=None,
                    help="JSON congelado com {genera: [...], created_at: ...}")
    ap.add_argument("--genera-db", default=None,
                    help="SQLite do pipeline (fallback se sem snapshot)")
    ap.add_argument("--min-width", type=int, default=120)
    ap.add_argument("--min-height", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        log.error("Pasta --src nao existe: %s", src)
        sys.exit(1)
    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        log.error("Nenhum PDF em %s", src)
        sys.exit(1)
    log.info("%d PDFs em %s", len(pdfs), src)

    out_draw = Path(args.drawings_out)
    out_micro = Path(args.microscopy_out)
    out_amb = Path(args.ambiguous_out) if args.ambiguous_out \
        else out_micro.parent / "treinamento_v8_ambiguo"
    for p in (out_draw, out_micro, out_amb):
        if not args.dry_run:
            p.mkdir(parents=True, exist_ok=True)

    # Provider (offline-first: snapshot > sqlite > fallback)
    if args.vocab_snapshot and Path(args.vocab_snapshot).is_file():
        vocab = VocabularyProvider.from_json_snapshot(Path(args.vocab_snapshot))
    elif args.genera_db and Path(args.genera_db).is_file():
        try:
            vocab = VocabularyProvider.from_sqlite(Path(args.genera_db))
        except Exception as e:
            log.warning("SQLite falhou (%s); usando fallback", e)
            vocab = VocabularyProvider.from_fallback()
    else:
        vocab = VocabularyProvider.from_fallback()

    index_path = out_amb.parent / "training_index_v8.jsonl"
    index_file = None if args.dry_run else open(index_path, "w", encoding="utf-8")

    counts: Dict[str, int] = {
        "DRAWING": 0, "MICROSCOPY": 0, "AMBIGUOUS": 0,
        "with_binomial": 0, "with_genus_only": 0, "no_epithet": 0,
    }
    total = 0

    try:
        for pdf in pdfs:
            log.info("-- %s", pdf.name)
            try:
                total += process_pdf(
                    pdf, out_draw, out_micro, out_amb,
                    vocab, args.min_width, args.min_height,
                    args.dry_run, index_file, counts,
                )
            except Exception as e:
                log.exception("Erro em %s: %s", pdf.name, e)
    finally:
        if index_file:
            index_file.close()

    log.info("=" * 70)
    log.info("Resumo:")
    log.info("  total_conidios_salvos  %d", total)
    for k, v in counts.items():
        log.info("  %-22s %d", k, v)
    if not args.dry_run:
        log.info("index JSONL: %s", index_path)
    log.info("schema_version: %s", SCHEMA_VERSION)
    snap = vocab.snapshot()
    log.info("vocab snapshot: %s  hash=%s  size=%d",
             snap.source, snap.hash[:12], snap.size)


if __name__ == "__main__":
    main()
