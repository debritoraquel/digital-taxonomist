#!/usr/bin/env python3
"""
image_type_classifier.py -- Taxonomista Digital (validacao 2026-04-20)
======================================================================
Heuristica rapida, sem treino, para separar:
    DRAWING     -- desenho taxonomico baseado em tracos (line drawing)
    MICROSCOPY  -- foto de laminas microscopicas (400x)
    AMBIGUOUS   -- caso duvidoso (revisao manual recomendada)

Features usadas:
    - saturacao media (HSV)           : tracos tendem a ser quase acromaticos
    - ratio de pixels brancos (>240)  : desenhos tem fundo branco predominante
    - edge density (Canny)            : desenhos tem densidade de bordas mais alta
                                         por unidade de area
    - entropia do histograma          : lamina tem distribuicao mais suave
    - ratio cor azul (coloracao)       : laminas coradas com azul de algodao
                                         tem dominancia azul

Regras compostas (ordem importa):
    1. Se white_bg > 0.55 AND sat_mean < 28 AND edge_density > 0.030 -> DRAWING
    2. Se blue_dominance > 0.18 OR sat_mean > 55                     -> MICROSCOPY
    3. Se white_bg > 0.70 AND edge_density > 0.020                    -> DRAWING
    4. Se sat_mean < 15 AND edge_density < 0.010                      -> MICROSCOPY (lamina cinza)
    5. Caso contrario                                                  -> AMBIGUOUS

Uso standalone:
    python image_type_classifier.py <imagem.png>
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError as e:
    raise SystemExit("Falta opencv-python. pip install opencv-python") from e


ImageType = Literal["DRAWING", "MICROSCOPY", "AMBIGUOUS"]


@dataclass
class ClassificationResult:
    image_type: ImageType
    confidence: float              # 0.0 .. 1.0 (quanto longe da fronteira)
    sat_mean: float
    white_bg_ratio: float
    edge_density: float
    hist_entropy: float
    blue_dominance: float
    width: int
    height: int
    rule_fired: str


def _to_rgb_np(path_or_pil) -> np.ndarray:
    if isinstance(path_or_pil, (str, Path)):
        img = Image.open(path_or_pil).convert("RGB")
    else:
        img = path_or_pil.convert("RGB")
    return np.array(img)


def _compute_features(rgb: np.ndarray) -> dict:
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    sat = hsv[..., 1]
    sat_mean = float(sat.mean())

    white_bg_ratio = float((gray > 240).sum()) / float(gray.size)

    # Canny com limiares adaptativos
    v = np.median(gray)
    lo = int(max(0, 0.66 * v))
    hi = int(min(255, 1.33 * v))
    edges = cv2.Canny(gray, lo, hi)
    edge_density = float(edges.sum() / 255.0) / float(edges.size)

    hist, _ = np.histogram(gray, bins=32, range=(0, 256), density=True)
    hist = hist + 1e-12
    hist_entropy = float(-(hist * np.log2(hist)).sum())

    # Coloracao azul (azul de algodao): R baixo, G medio, B alto
    r, g, b = rgb[..., 0].astype(float), rgb[..., 1].astype(float), rgb[..., 2].astype(float)
    blue_mask = (b > r + 15) & (b > g - 5) & (b > 80)
    blue_dominance = float(blue_mask.sum()) / float(blue_mask.size)

    return dict(
        width=w,
        height=h,
        sat_mean=sat_mean,
        white_bg_ratio=white_bg_ratio,
        edge_density=edge_density,
        hist_entropy=hist_entropy,
        blue_dominance=blue_dominance,
    )


def classify(path_or_pil) -> ClassificationResult:
    rgb = _to_rgb_np(path_or_pil)
    feats = _compute_features(rgb)

    # Regras encadeadas
    image_type: ImageType
    rule: str
    conf: float

    if feats["white_bg_ratio"] > 0.55 and feats["sat_mean"] < 28 and feats["edge_density"] > 0.030:
        image_type = "DRAWING"
        rule = "R1: white_bg>0.55 AND sat_mean<28 AND edge_density>0.030"
        conf = 0.90
    elif feats["blue_dominance"] > 0.18 or feats["sat_mean"] > 55:
        image_type = "MICROSCOPY"
        rule = "R2: blue_dominance>0.18 OR sat_mean>55 (coloracao azul de algodao)"
        conf = 0.85
    elif feats["white_bg_ratio"] > 0.70 and feats["edge_density"] > 0.020:
        image_type = "DRAWING"
        rule = "R3: white_bg>0.70 AND edge_density>0.020"
        conf = 0.80
    elif feats["sat_mean"] < 15 and feats["edge_density"] < 0.010:
        image_type = "MICROSCOPY"
        rule = "R4: sat_mean<15 AND edge_density<0.010 (lamina cinza suave)"
        conf = 0.70
    elif feats["white_bg_ratio"] > 0.60 and feats["edge_density"] > 0.015:
        # Desenhos com fundo claro e bordas moderadas (árvores filogenéticas coloridas, esquemas)
        image_type = "DRAWING"
        rule = "R5: white_bg>0.60 AND edge_density>0.015 (desenho/esquema fundo claro)"
        conf = 0.72
    elif feats["white_bg_ratio"] < 0.30 and feats["edge_density"] < 0.025:
        # Fotos de colônias em substrato natural (fundo escuro, baixa densidade de bordas)
        image_type = "MICROSCOPY"
        rule = "R6: white_bg<0.30 AND edge_density<0.025 (foto colonia/substrato natural)"
        conf = 0.68
    elif feats["hist_entropy"] > 3.8 and feats["white_bg_ratio"] < 0.45:
        # Distribuição de cinza suave e contínua → imagem fotográfica (microscopia ou colônia)
        image_type = "MICROSCOPY"
        rule = "R7: hist_entropy>3.8 AND white_bg<0.45 (imagem fotografica distribuicao suave)"
        conf = 0.65
    elif feats["sat_mean"] < 3 and feats["white_bg_ratio"] < 0.40:
        # Foto muito escura ou quase monocromática (BW/SEM antigo) → microscopia
        image_type = "MICROSCOPY"
        rule = "R8: sat<3 AND white_bg<0.40 (foto BW/SEM escura)"
        conf = 0.68
    elif feats["sat_mean"] < 3 and feats["white_bg_ratio"] >= 0.40 and feats["edge_density"] < 0.022:
        # Tinta preta sobre branco, poucas bordas → line art / desenho BW
        image_type = "DRAWING"
        rule = "R9: sat<3 AND white_bg>=0.40 AND edge<0.022 (line art BW)"
        conf = 0.68
    elif feats["white_bg_ratio"] > 0.75 and feats["edge_density"] < 0.010:
        # Fundo muito branco, poucas bordas → mapa / gráfico / chart
        image_type = "DRAWING"
        rule = "R10: white_bg>0.75 AND edge<0.010 (mapa/grafico fundo branco)"
        conf = 0.65
    else:
        image_type = "AMBIGUOUS"
        rule = "R11: nenhuma regra disparou"
        conf = 0.40

    return ClassificationResult(
        image_type=image_type,
        confidence=conf,
        rule_fired=rule,
        **feats,
    )


def main():
    if len(sys.argv) < 2:
        print("Uso: python image_type_classifier.py <imagem.png>")
        sys.exit(1)
    for arg in sys.argv[1:]:
        r = classify(arg)
        print(arg)
        for k, v in asdict(r).items():
            print(f"  {k:20s}  {v}")
        print()


if __name__ == "__main__":
    main()
