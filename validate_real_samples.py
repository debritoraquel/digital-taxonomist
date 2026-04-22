#!/usr/bin/env python3
r"""
validate_real_samples.py -- Taxonomista Digital (validacao 2026-04-20)
=======================================================================
FASE 2 -- Validacao com dados reais.

Roda inferencia de genero/especie sobre a pasta
    C:\Users\Usuario\Desktop\Fotos_Organizadas_20260128_184422
Preservando a hierarquia de pontos de amostragem (cada subpasta = ponto),
e salvando o resultado em
    C:\Users\Usuario\Desktop\Validacao_e_teste_20_04_26\<ponto>\<img_identificada>

Pipeline por imagem:
  1. Carrega DINOv2-small (mesmo modelo usado no pipeline principal).
  2. Gera embedding da imagem alvo.
  3. Faz k-NN contra o indice FAISS construido a partir das pastas:
        treinamento_desenho_taxonomico\
        treinamento_lamina_taxonomico\
     (ambas juntas; a origem e registrada como fonte no metadado)
  4. Agrega votos dos top-K vizinhos ponderado por similaridade.
  5. Retorna: genero, especie, confianca [0..1], top-3 candidatos.
  6. Salva imagem renomeada como:
        <nome_original_sem_ext>__<Genero>_<especie>__conf<NN>.<ext>
     e um CSV mestre com todas as predicoes.

Uso:
    python validate_real_samples.py ^
        --target "C:\Users\Usuario\Desktop\Fotos_Organizadas_20260128_184422" ^
        --out    "C:\Users\Usuario\Desktop\Validacao_e_teste_20_04_26" ^
        --training-dirs "D:\MBA\tcc\treinamento_desenho_taxonomico" ^
                        "D:\MBA\tcc\treinamento_lamina_taxonomico" ^
        --model dinov2-small --topk 5

Dependencias:
    pip install torch torchvision transformers faiss-cpu Pillow tqdm
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("validate")

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# -----------------------------------------------------------------------
# DINOv2 loader (mesmo contrato do pipeline principal)
# -----------------------------------------------------------------------
def load_dinov2(model_name: str = "dinov2-small", device: str = "cpu"):
    import torch
    from transformers import AutoImageProcessor, AutoModel
    hf_ids = {
        "dinov2-small": "facebook/dinov2-small",
        "dinov2-base":  "facebook/dinov2-base",
    }
    hf_id = hf_ids.get(model_name, model_name)
    log.info("Carregando %s (%s)...", model_name, hf_id)
    processor = AutoImageProcessor.from_pretrained(hf_id)
    model = AutoModel.from_pretrained(hf_id).to(device).eval()
    return processor, model, device


def embed_image(pil: Image.Image, processor, model, device) -> np.ndarray:
    import torch
    with torch.no_grad():
        inputs = processor(images=pil.convert("RGB"), return_tensors="pt").to(device)
        out = model(**inputs)
        # Pooled via CLS token (primeiro)
        vec = out.last_hidden_state[:, 0, :].squeeze(0).cpu().numpy().astype("float32")
    # Normalizacao L2 (para cosseno == produto interno)
    n = np.linalg.norm(vec) + 1e-12
    return vec / n


# -----------------------------------------------------------------------
# Reconstruir rotulos a partir do nome do arquivo OU do JSON sidecar
# -----------------------------------------------------------------------
# Nome do arquivo:
#   {paper_slug}__p{NNN}__fig{NN}__{hash6}__{epi_slug}.png
# epi_slug eh lowercased pelo slugify():
#   "alatospora_acuminata", "alatospora_sp", "unknown_unknown"
# Sidecar JSON esta em:
#   {paper_slug}__p{NNN}__fig{NN}__{hash6}.json
# -----------------------------------------------------------------------
_BINOMIAL_FROM_NAME = re.compile(
    r"__([A-Za-z][a-z]{2,})_([a-z]{3,}|sp)(?:\.[A-Za-z]+)?$", re.UNICODE
)


def _strip_epi_suffix(stem: str) -> str:
    """
    De "{paper}__pNNN__figNN__HHHHHH__alatospora_acuminata" -> "{paper}__pNNN__figNN__HHHHHH"
    """
    parts = stem.split("__")
    if len(parts) >= 5:
        return "__".join(parts[:4])
    return stem


@dataclass
class TrainSample:
    path: Path
    genus: Optional[str]
    species: Optional[str]
    source_kind: str             # "drawing" ou "microscopy"
    label_confidence: float = 1.0   # extraction confidence (v8: resolution.confidence)


def load_training_samples(dirs: Sequence[Path]) -> List[TrainSample]:
    out: List[TrainSample] = []
    skipped_unknown = 0
    for d in dirs:
        if not d.is_dir():
            log.warning("Pasta de treino ausente: %s", d)
            continue
        name_lower = d.name.lower()
        if "desenho" in name_lower:
            kind = "drawing"
        elif "lamina" in name_lower or "lâmina" in name_lower:
            kind = "microscopy"
        elif "ambig" in name_lower:
            kind = "ambiguous"
        else:
            kind = "unknown"
        for p in sorted(d.iterdir()):
            if p.suffix.lower() not in IMG_EXT:
                continue
            genus = species = None
            # 1. JSON sidecar (removendo o sufixo __{epi_slug})
            base_id = _strip_epi_suffix(p.stem)
            j = d / f"{base_id}.json"
            if not j.is_file():
                # fallback: tenta o proprio stem (caso o sidecar tenha outro nome)
                j_alt = p.with_suffix(".json")
                if j_alt.is_file():
                    j = j_alt
            label_conf = 1.0
            if j.is_file():
                try:
                    meta = json.loads(j.read_text(encoding="utf-8"))
                    # v8 sidecar: resolution.binomial + resolution.confidence
                    res = meta.get("resolution") or {}
                    if res.get("binomial") and "UNKNOWN" not in res["binomial"]:
                        parts = res["binomial"].replace("_", " ").split()
                        if len(parts) >= 2:
                            genus = parts[0].capitalize()
                            species = parts[1].lower()
                        elif len(parts) == 1:
                            genus = parts[0].capitalize()
                        label_conf = float(res.get("confidence", 1.0))
                    # v1 sidecar: epithet.genus / epithet.species
                    if not genus:
                        epi = meta.get("epithet") or {}
                        genus = epi.get("genus") or None
                        species = epi.get("species") or None
                except Exception:
                    pass
            # 2. Padrao no nome (aceita lowercase, por causa do slugify)
            if not genus:
                m = _BINOMIAL_FROM_NAME.search(p.name)
                if m:
                    g_raw = m.group(1)
                    s_raw = m.group(2)
                    # Rejeita rotulos sentinelas
                    if g_raw.lower() in {"unknown", "sp"}:
                        pass
                    else:
                        genus = g_raw[:1].upper() + g_raw[1:].lower()
                        species = None if s_raw == "sp" else s_raw.lower()
            if not genus:
                skipped_unknown += 1
                continue
            # Normaliza capitalizacao
            genus = genus[:1].upper() + genus[1:].lower()
            if species:
                species = species.lower()
            out.append(TrainSample(path=p, genus=genus, species=species, source_kind=kind, label_confidence=label_conf))
    log.info("Amostras de treino carregadas: %d (de %d pastas; %d sem rotulo)",
             len(out), len(dirs), skipped_unknown)
    return out


# -----------------------------------------------------------------------
# Construcao / uso do indice FAISS
# -----------------------------------------------------------------------
def _balance_samples(samples: List[TrainSample], max_per_class: int = 8) -> List[TrainSample]:
    """Cap por espécie para evitar que classes dominantes monopolizem o índice."""
    from collections import defaultdict
    import random
    buckets: dict = defaultdict(list)
    for s in samples:
        key = (s.genus or "UNKNOWN", s.species or "sp")
        buckets[key].append(s)
    balanced = []
    for key, items in buckets.items():
        # Prioriza amostras com maior confiança de extração
        items.sort(key=lambda x: x.label_confidence, reverse=True)
        balanced.extend(items[:max_per_class])
    log.info("Balanceamento: %d → %d amostras (cap=%d/espécie, %d espécies únicas)",
             len(samples), len(balanced), max_per_class, len(buckets))
    return balanced


def build_faiss_index(samples: List[TrainSample], processor, model, device,
                      max_per_class: int = 8):
    import faiss
    if not samples:
        raise RuntimeError("Sem amostras de treino. Rode a Fase 1 primeiro.")
    samples = _balance_samples(samples, max_per_class)
    vecs = []
    for s in tqdm(samples, desc="Embed (train)"):
        try:
            pil = Image.open(s.path)
            vecs.append(embed_image(pil, processor, model, device))
        except Exception as e:
            log.warning("Falhou embed em %s: %s", s.path.name, e)
            vecs.append(np.zeros(384, dtype="float32"))
    V = np.vstack(vecs).astype("float32")
    dim = V.shape[1]
    index = faiss.IndexFlatIP(dim)    # inner product (L2-normalizado -> cosseno)
    index.add(V)
    return index, V


@dataclass
class Prediction:
    target_path: Path
    sampling_point: str              # nome da subpasta original
    top_k: List[Tuple[str, str, float]]     # [(genus, species_or_sp, score), ...]
    best_genus: str
    best_species: str
    confidence: float


def aggregate_votes(neighbors: List[Tuple[TrainSample, float]]) -> Tuple[str, str, float, List[Tuple[str, str, float]]]:
    """
    Votacao ponderada por similaridade (score coseno em [-1, 1]).
    Retorna (best_genus, best_species, conf, top3) onde species pode ser "sp".
    """
    # Agrega por binomial "Genus species" (species pode ser None -> "sp")
    bucket: Dict[Tuple[str, str], float] = defaultdict(float)
    for sample, score in neighbors:
        g = sample.genus
        s = sample.species or "sp"
        # Peso = similaridade visual × confiança de extração da amostra de treino
        # Implementa o ponto 1 do TCC: P(D|H) ponderado pelo confidence score
        w = max(0.0, float(score)) * sample.label_confidence
        bucket[(g, s)] += w
    if not bucket:
        return "UNKNOWN", "sp", 0.0, []
    ranked = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(w for _, w in ranked) + 1e-12
    top3 = [(g, s, w / total) for (g, s), w in ranked[:3]]
    (best_g, best_s), best_w = ranked[0]
    return best_g, best_s, best_w / total, top3


def sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:80]


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True,
                    help="Pasta com imagens reais (ex.: Fotos_Organizadas_...)")
    ap.add_argument("--out", required=True,
                    help="Pasta de saida (ex.: Validacao_e_teste_20_04_26)")
    ap.add_argument("--training-dirs", nargs="+", required=True,
                    help="Pastas de treino: desenho e/ou lamina")
    ap.add_argument("--model", default="dinov2-small")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--min-confidence", type=float, default=0.35,
                    help="Abaixo disso, marca como UNCERTAIN no nome")
    ap.add_argument("--device", default=None,
                    help="cpu ou cuda; default=auto")
    ap.add_argument("--max-per-class", type=int, default=8,
                    help="Cap de imagens por especie no indice FAISS (balanceamento)")
    args = ap.parse_args()

    target_root = Path(args.target)
    if not target_root.is_dir():
        log.error("--target nao existe: %s", target_root)
        sys.exit(1)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    # 1. Carrega treino
    train_dirs = [Path(d) for d in args.training_dirs]
    samples = load_training_samples(train_dirs)
    if not samples:
        log.error("Sem amostras rotuladas. Rode Fase 1 antes.")
        sys.exit(2)

    # 2. Detecta device
    device = args.device
    if device is None:
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            log.error("PyTorch nao instalado. pip install torch torchvision")
            sys.exit(3)
    log.info("Device: %s", device)

    # 3. Modelo + indice
    processor, model, device = load_dinov2(args.model, device)
    index, _V = build_faiss_index(samples, processor, model, device,
                                  max_per_class=args.max_per_class)

    # 4. Enumera imagens alvo, agrupadas por ponto de amostragem
    target_images: List[Path] = []
    for p in target_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMG_EXT:
            target_images.append(p)
    log.info("Imagens reais a classificar: %d", len(target_images))

    # 5. CSV mestre
    csv_path = out_root / "predictions.csv"
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow([
        "sampling_point", "target_path", "best_genus", "best_species",
        "confidence", "top3_json", "output_path",
    ])

    predictions: List[Prediction] = []
    for img_path in tqdm(target_images, desc="Inferencia"):
        rel = img_path.relative_to(target_root)
        sampling_point = rel.parts[0] if len(rel.parts) > 1 else "RAIZ"
        try:
            pil = Image.open(img_path)
            vec = embed_image(pil, processor, model, device).reshape(1, -1)
        except Exception as e:
            log.warning("Falhou em %s: %s", img_path.name, e)
            continue

        D, I = index.search(vec, args.topk)
        neighbors = [(samples[int(idx)], float(D[0][i])) for i, idx in enumerate(I[0]) if idx >= 0]
        best_g, best_s, conf, top3 = aggregate_votes(neighbors)

        # Determina destino
        point_dir = out_root / sanitize(sampling_point)
        point_dir.mkdir(parents=True, exist_ok=True)
        stem = img_path.stem
        epi_part = f"{best_g}_{best_s}" if best_g != "UNKNOWN" else "UNKNOWN"
        flag = "UNCERTAIN_" if conf < args.min_confidence else ""
        conf_int = int(round(conf * 100))
        out_name = f"{sanitize(stem)}__{flag}{epi_part}__conf{conf_int:02d}{img_path.suffix.lower()}"
        out_path = point_dir / out_name

        # Copia (nao move) o arquivo original
        try:
            pil.save(out_path)
        except Exception as e:
            log.warning("Falhou salvar %s: %s", out_path.name, e)
            continue

        predictions.append(Prediction(
            target_path=img_path,
            sampling_point=sampling_point,
            top_k=top3,
            best_genus=best_g,
            best_species=best_s,
            confidence=conf,
        ))
        writer.writerow([
            sampling_point, str(img_path), best_g, best_s, f"{conf:.4f}",
            json.dumps(top3, ensure_ascii=False), str(out_path),
        ])

    csv_file.close()

    # 6. Relatorio final por ponto
    per_point: Dict[str, Counter] = defaultdict(Counter)
    for pr in predictions:
        per_point[pr.sampling_point][pr.best_genus] += 1

    report = out_root / "summary_by_point.md"
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"# Sumario de validacao -- {len(predictions)} imagens\n\n")
        f.write(f"Diretorio de saida: `{out_root}`\n\n")
        f.write(f"Threshold de confianca: {args.min_confidence}\n\n")
        for point, cnt in sorted(per_point.items()):
            f.write(f"## {point}\n\n")
            f.write(f"Total: {sum(cnt.values())} imagens\n\n")
            f.write("| Genero | N |\n|---|---|\n")
            for g, n in cnt.most_common():
                f.write(f"| {g} | {n} |\n")
            f.write("\n")
    log.info("CSV: %s", csv_path)
    log.info("Relatorio: %s", report)
    log.info("Pronto. %d imagens classificadas em %d pontos.", len(predictions), len(per_point))


if __name__ == "__main__":
    main()
