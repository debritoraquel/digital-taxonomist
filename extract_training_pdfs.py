#!/usr/bin/env python3
r"""
extract_training_pdfs.py -- Taxonomista Digital (validacao 2026-04-20)
=======================================================================
FASE 1 -- Treinamento.

Extrai imagens dos 24 PDFs em "Chaves com imagem", separando-as em:
    treinamento_desenho_taxonomico\   (line drawings)
    treinamento_lamina_taxonomico\    (microscopy)

Para cada imagem salva:
    1. ID canonico UNICO:   {paper_slug}__p{page:03d}__fig{idx:02d}
    2. Legenda bruta (JSON sidecar)
    3. Genero e especie extraidos (epitheto rigoroso, ver genus_species_extractor.py)
    4. Classificacao DRAWING / MICROSCOPY / AMBIGUOUS (ver image_type_classifier.py)

Uso:
    python extract_training_pdfs.py ^
        --src "D:\MBA\tcc\REFERENCIAS\Chaves com imagem" ^
        --drawings-out   "D:\MBA\tcc\treinamento_desenho_taxonomico" ^
        --microscopy-out "D:\MBA\tcc\treinamento_lamina_taxonomico" ^
        --ambiguous-out  "D:\MBA\tcc\treinamento_ambiguo" ^
        --genera-db "D:\MBA\tcc\taxonomista-digital\data\taxonomista_digital.db" ^
        --min-width 120 --min-height 120

Dependencias:
    pip install PyMuPDF Pillow numpy opencv-python
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

import fitz                    # PyMuPDF
from PIL import Image

# Dependencias locais
sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_type_classifier import classify, ClassificationResult       # noqa: E402
from genus_species_extractor import EpithetExtractor, EpithetResult    # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract")

# Override semântico para imagens AMBIGUOUS baseado no texto da legenda.
# Árvores filogenéticas → DRAWING; morfologia/colônias → MICROSCOPY.
_TREE_CAPTION_RE = re.compile(
    r"\b(?:phylo(?:gram|genet|tree|genetic)|ML\s+tree|RAxML|neighbor[\s\-]joining|"
    r"parsimony|cladogram|dendrogram|maximum\s+likelihood|bayesian|"
    r"phylogenetic|multi[\s\-]locus|concatenat|bootstrap\s+support|"
    r"molecular\s+clock|divergence\s+time)\b",
    re.IGNORECASE,
)
_MICRO_CAPTION_RE = re.compile(
    r"\b(?:colon(?:y|ies)|conidi(?:a|um|ophore|óforo|oforo)|spore[s]?|"
    r"microscop|lamina|morpholog|hyphae?|ascomat|peridium|"
    r"submerged|aquatic|lignicol|holotype|fruiting|sporulat|"
    r"setae?|stroma|chlamydospore|germina)\b",
    re.IGNORECASE,
)


def caption_type_override(image_type: str, caption: str) -> str:
    """Refina classificação AMBIGUOUS usando texto da legenda."""
    if image_type != "AMBIGUOUS":
        return image_type
    if _TREE_CAPTION_RE.search(caption):
        return "DRAWING"
    if _MICRO_CAPTION_RE.search(caption):
        return "MICROSCOPY"
    return "AMBIGUOUS"


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")

def slugify(s: str) -> str:
    s = _SLUG_RE.sub("_", s.strip()).strip("_").lower()
    return s[:80] if len(s) > 80 else s


def short_hash(data: bytes, n: int = 6) -> str:
    return hashlib.sha1(data).hexdigest()[:n]


# -----------------------------------------------------------------------
# Extracao de texto proximo da bbox da imagem
# -----------------------------------------------------------------------
_FIG_CAPTION_RE = re.compile(
    r"(?:^|\n)\s*(?:fig(?:ure|ura)?\.?|plate|prancha|tab(?:le|ela)?\.?)\s*[\dIVXLCDM]+",
    re.IGNORECASE,
)


def extract_caption_for_bbox(page: fitz.Page, bbox: fitz.Rect, window_px: float = 120.0) -> str:
    """
    Retorna texto concatenado de blocos proximos a bbox da imagem, priorizando
    blocos logo ABAIXO dela (onde a legenda tipicamente aparece).
    """
    blocks = page.get_text("blocks")    # [x0, y0, x1, y1, text, block_no, block_type]
    if not blocks:
        return ""

    candidates: List[Tuple[float, str]] = []
    for b in blocks:
        if len(b) < 5:
            continue
        bx0, by0, bx1, by1, text = b[0], b[1], b[2], b[3], b[4] or ""
        text = text.strip()
        if not text:
            continue
        # distancia vertical assinada (bloco abaixo -> positiva e pequena)
        vertical = by0 - bbox.y1
        horizontal_overlap = max(0.0, min(bx1, bbox.x1) - max(bx0, bbox.x0))
        # bloco abaixo, dentro da janela, com alguma sobreposicao horizontal
        if 0 <= vertical <= window_px and horizontal_overlap > 0:
            candidates.append((vertical, text))
        elif vertical < 0 and abs(vertical) <= window_px * 0.5 and horizontal_overlap > 0:
            # bloco logo acima (raro, mas existe)
            candidates.append((abs(vertical) + 20.0, text))

    candidates.sort(key=lambda t: t[0])
    # Prioriza primeiro que comeca com 'Fig' / 'Figure' / 'Figura' / 'Plate'
    prioritized = [t for _, t in candidates if _FIG_CAPTION_RE.match(t)]
    remaining = [t for _, t in candidates if not _FIG_CAPTION_RE.match(t)]
    ordered = prioritized + remaining
    # Concatena ate 3 blocos mais proximos
    return " \n ".join(ordered[:3]).strip()


# -----------------------------------------------------------------------
# Extracao de imagens de um PDF
# -----------------------------------------------------------------------
def iter_images_in_pdf(pdf_path: Path, min_w: int, min_h: int):
    """
    Generator que produz (page_index, fig_index, pil_image, bbox, caption_text, full_page_text).
    """
    doc = fitz.open(pdf_path)
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            images = page.get_images(full=True)
            if not images:
                continue
            page_text = page.get_text()
            fig_idx = 0
            for img_info in images:
                xref = img_info[0]
                try:
                    base_img = doc.extract_image(xref)
                except Exception as e:
                    log.warning("xref=%d em %s p%d falhou: %s", xref, pdf_path.name, pno + 1, e)
                    continue
                img_bytes = base_img["image"]
                ext = base_img.get("ext", "png")

                try:
                    pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                except Exception as e:
                    log.debug("img corrompida xref=%d: %s", xref, e)
                    continue

                if pil.width < min_w or pil.height < min_h:
                    continue

                # Localiza bbox (retangulos onde a imagem aparece)
                rects = [r for r in page.get_image_rects(xref)] or [fitz.Rect(0, 0, 0, 0)]
                bbox = rects[0]

                caption = extract_caption_for_bbox(page, bbox)
                yield pno + 1, fig_idx, pil, bbox, caption, page_text, ext
                fig_idx += 1
    finally:
        doc.close()


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Pasta com os 24 PDFs")
    ap.add_argument("--drawings-out", required=True)
    ap.add_argument("--microscopy-out", required=True)
    ap.add_argument("--ambiguous-out", default=None)
    ap.add_argument("--genera-db", default=None,
                    help="SQLite do pipeline (opcional; usa fallback se omitido)")
    ap.add_argument("--min-width",  type=int, default=120)
    ap.add_argument("--min-height", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true",
                    help="So lista o que seria extraido, sem gravar")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        log.error("Pasta --src nao existe: %s", src)
        sys.exit(1)

    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        log.error("Nenhum PDF encontrado em %s", src)
        sys.exit(1)
    log.info("%d PDFs encontrados em %s", len(pdfs), src)

    out_draw = Path(args.drawings_out)
    out_micro = Path(args.microscopy_out)
    out_amb = Path(args.ambiguous_out) if args.ambiguous_out else out_micro.parent / "treinamento_ambiguo"
    for p in (out_draw, out_micro, out_amb):
        if not args.dry_run:
            p.mkdir(parents=True, exist_ok=True)

    # Extrator de epiteto
    # strict_vocab=False: aceita qualquer binômio válido (não restringe ao vocabulário
    # controlado de hifomicetos), pois os PDFs de treino cobrem fungos além do grupo-alvo.
    if args.genera_db and Path(args.genera_db).is_file():
        try:
            extractor = EpithetExtractor.from_sqlite(args.genera_db, strict_vocab=False)
            log.info("Vocabulario carregado do SQLite (vocab aberto): %s", args.genera_db)
        except Exception as e:
            log.warning("Fallback para lista embutida (%s)", e)
            extractor = EpithetExtractor.from_fallback(strict_vocab=False)
    else:
        extractor = EpithetExtractor.from_fallback(strict_vocab=False)
        log.info("Vocabulario: fallback + vocab aberto (%d generos base)", len(extractor._known))

    # Indice consolidado (JSONL de auditoria)
    index_path = out_amb.parent / "training_index.jsonl"
    index_file = None if args.dry_run else open(index_path, "w", encoding="utf-8")

    counts = {"DRAWING": 0, "MICROSCOPY": 0, "AMBIGUOUS": 0,
              "with_binomial": 0, "with_genus_only": 0, "no_epithet": 0}

    try:
        for pdf in pdfs:
            paper_slug = slugify(pdf.stem)
            log.info("-- %s", pdf.name)
            try:
                for page_no, fig_idx, pil, bbox, caption, page_text, src_ext in iter_images_in_pdf(
                    pdf, args.min_width, args.min_height
                ):
                    # Classificar tipo (imagem) + override semântico por legenda
                    cls: ClassificationResult = classify(pil)
                    effective_type = caption_type_override(cls.image_type, caption)
                    if effective_type == "DRAWING":
                        target = out_draw
                    elif effective_type == "MICROSCOPY":
                        target = out_micro
                    else:
                        target = out_amb
                    counts[effective_type] += 1

                    # Epiteto
                    epi: EpithetResult = extractor.extract_from_caption(caption)
                    if epi.binomial is None and epi.genus is None:
                        # Tenta no texto completo da pagina como contexto secundario
                        epi = extractor.extract_from_caption(page_text)

                    if epi.binomial:
                        counts["with_binomial"] += 1
                        epi_slug = slugify(epi.binomial)
                    elif epi.genus:
                        counts["with_genus_only"] += 1
                        epi_slug = slugify(epi.genus) + "_sp"
                    else:
                        counts["no_epithet"] += 1
                        epi_slug = "UNKNOWN_UNKNOWN"

                    # ID canonico unico
                    png_bytes = io.BytesIO()
                    pil.save(png_bytes, format="PNG")
                    h = short_hash(png_bytes.getvalue())
                    base_id = f"{paper_slug}__p{page_no:03d}__fig{fig_idx:02d}__{h}"
                    fname = f"{base_id}__{epi_slug}.png"
                    meta_name = f"{base_id}.json"

                    # Gravar
                    out_img = target / fname
                    out_meta = target / meta_name
                    if not args.dry_run:
                        pil.save(out_img, format="PNG")
                        meta = {
                            "id": base_id,
                            "paper": pdf.name,
                            "page": page_no,
                            "fig_index": fig_idx,
                            "image_type": effective_type,
                            "image_type_raw": cls.image_type,
                            "classifier": asdict(cls),
                            "epithet": asdict(epi),
                            "caption_raw": caption,
                            "bbox_pdf_points": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                            "source_extension": src_ext,
                            "saved_to": str(out_img),
                        }
                        out_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                        index_file.write(json.dumps(meta, ensure_ascii=False) + "\n")
                        index_file.flush()
                    log.info("  [%s] p%03d fig%02d  %-48s  caption=%r",
                             cls.image_type[:4], page_no, fig_idx, epi_slug,
                             (caption[:70] + "...") if len(caption) > 70 else caption)
            except Exception as e:
                log.exception("Erro em %s: %s", pdf.name, e)
    finally:
        if index_file:
            index_file.close()

    log.info("=" * 70)
    log.info("Resumo:")
    for k, v in counts.items():
        log.info("  %-18s  %d", k, v)
    if not args.dry_run:
        log.info("Indice consolidado: %s", index_path)


if __name__ == "__main__":
    main()
