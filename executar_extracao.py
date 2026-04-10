"""
============================================================================
EXTRATOR DE DESENHOS DE CONIDIOS - Script Standalone para Windows
============================================================================

Script autocontido para rodar diretamente no seu computador.
Processa todos os PDFs da pasta, extrai pranchas com desenhos de conidios,
segmenta cada conidio individual e salva como PNG nomeado.

PLANEJAMENTO DE EXECUCAO:
=========================

PASSO 1 - INSTALAR PYTHON (se ainda nao tiver)
    Baixe em: https://www.python.org/downloads/
    Marque "Add Python to PATH" durante a instalacao

PASSO 2 - INSTALAR DEPENDENCIAS
    Abra o terminal (cmd ou PowerShell) e execute:

        pip install PyMuPDF opencv-python pytesseract Pillow numpy tqdm

    (Opcional) Para OCR de legendas, instale o Tesseract:
        https://github.com/UB-Mannheim/tesseract/wiki

PASSO 3 - COPIAR ESTE ARQUIVO
    Copie este arquivo para a pasta dos PDFs:
        D:\\MBA\\tcc\\REFERENCIAS\\CHAVES HIFOMICETOS\\executar_extracao.py

PASSO 4 - EXECUTAR
    No terminal, navegue ate a pasta e execute:

        cd "D:\\MBA\\tcc\\REFERENCIAS\\CHAVES HIFOMICETOS"
        python executar_extracao.py

    Ou para processar apenas um autor especifico:
        python executar_extracao.py --filter Araujo

    Ou com resolucao mais alta:
        python executar_extracao.py --dpi 600

PASSO 5 - RESULTADO
    Uma pasta "conidios_extraidos" sera criada com os PNGs:
        conidios_extraidos/
        ├── Anguillospora_longissima_pg001_img00_cond00.png
        ├── Anguillospora_longissima_pg001_img00_cond01.png
        ├── Campylospora_chaetocladia_pg003_img00_cond00.png
        └── ...

    Nomenclatura: {Genero_especie}_pg{pagina}_img{imagem}_cond{conidio}.png

AJUSTES SE NECESSARIO:
======================
    Se extrair ruido demais  -> aumente --min-area (ex: --min-area 500)
    Se perder conidios       -> diminua --min-area (ex: --min-area 50)
    Se pegar areas grandes   -> diminua --max-area (ex: --max-area 30000)
    Se qualidade baixa       -> aumente --dpi (ex: --dpi 600)
    Se quiser tamanho fixo   -> use --size 224

============================================================================
"""

import logging
import os
import re
import sys
from pathlib import Path

# -------------------------------------------------------------------------
# Verificacao de dependencias
# -------------------------------------------------------------------------

REQUIRED = {
    "fitz": "PyMuPDF",
    "cv2": "opencv-python",
    "numpy": "numpy",
    "tqdm": "tqdm",
}

missing = []
for module, package in REQUIRED.items():
    try:
        __import__(module)
    except ImportError:
        missing.append(package)

if missing:
    print("=" * 60)
    print("DEPENDENCIAS FALTANDO")
    print("=" * 60)
    print(f"\nInstale com:\n\n    pip install {' '.join(missing)}\n")
    sys.exit(1)

import cv2
import fitz
import numpy as np
from tqdm import tqdm

try:
    import pytesseract
except ImportError:
    pytesseract = None

# -------------------------------------------------------------------------
# Configuracao
# -------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Regex: Genero (>=4 letras, maiuscula) + epiteto (>=4 letras, minuscula)
_BINOMIAL_RE = re.compile(r"\b([A-Z][a-z]{3,})\s+([a-z]{4,})\b(?!-)")


# -------------------------------------------------------------------------
# Classe principal
# -------------------------------------------------------------------------


class ExtratorConidios:
    """Extrai desenhos individuais de conidios a partir de PDFs cientificos."""

    def __init__(
        self,
        dpi=300,
        min_area=100,
        max_area=50000,
        min_solidity=0.1,
        max_aspect_ratio=20.0,
        pad_fraction=0.12,
        output_size=None,
        min_image_area=10000,
    ):
        self.dpi = dpi
        self.min_area = min_area
        self.max_area = max_area
        self.min_solidity = min_solidity
        self.max_aspect_ratio = max_aspect_ratio
        self.pad_fraction = pad_fraction
        self.output_size = output_size
        self.min_image_area = min_image_area

    # -- Descoberta de PDFs -----------------------------------------------

    def listar_pdfs(self, pasta, filtro=None):
        """Lista PDFs na pasta, opcionalmente filtrando por substring."""
        pasta = Path(pasta)
        pdfs = sorted(pasta.glob("*.pdf")) + sorted(pasta.glob("*.PDF"))
        if filtro:
            pdfs = [p for p in pdfs if filtro.lower() in p.stem.lower()]
        print(f"\n  PDFs encontrados: {len(pdfs)}" +
              (f" (filtro: '{filtro}')" if filtro else ""))
        for p in pdfs:
            print(f"    - {p.name}")
        print()
        return pdfs

    # -- Extracao de imagens do PDF ---------------------------------------

    def _pixmap_para_numpy(self, pixmap):
        """Converte Pixmap do PyMuPDF para array NumPy BGR."""
        if pixmap.alpha:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
        data = np.frombuffer(pixmap.samples, dtype=np.uint8)
        img = data.reshape(pixmap.height, pixmap.width, pixmap.n)
        if img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img

    def extrair_imagens_pagina(self, pagina, idx_pagina):
        """
        Extrai imagens de uma pagina do PDF.
        Estrategia 1: imagens embutidas.
        Estrategia 2: rasterizacao da pagina inteira (fallback).
        """
        doc = pagina.parent
        imagens = []

        # Estrategia 1: imagens embutidas
        for img_info in pagina.get_images(full=True):
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
                if base is None:
                    continue
                arr = np.frombuffer(base["image"], dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None and img.shape[0] * img.shape[1] >= self.min_image_area:
                    imagens.append(img)
            except Exception as e:
                logger.debug(f"Pg {idx_pagina + 1}, xref {xref}: {e}")

        # Estrategia 2: rasterizar pagina
        if not imagens:
            zoom = self.dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = pagina.get_pixmap(matrix=mat)
            img = self._pixmap_para_numpy(pix)
            if img.shape[0] * img.shape[1] >= self.min_image_area:
                imagens.append(img)

        return imagens

    # -- Processamento de imagem ------------------------------------------

    def preprocessar(self, img):
        """Converte para cinza, melhora contraste, reduz ruido."""
        if len(img.shape) == 3:
            cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            cinza = img.copy()

        # CLAHE para contraste local
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        realcado = clahe.apply(cinza)

        # Blur leve para reduzir ruido de scanner
        suavizado = cv2.GaussianBlur(realcado, (3, 3), 0)
        return suavizado

    def binarizar(self, cinza):
        """Binarizacao Otsu (invertida: foreground = branco)."""
        _, binario = cv2.threshold(
            cinza, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )

        # Fechar lacunas e remover ruido
        kernel_fechar = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        kernel_abrir = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        binario = cv2.morphologyEx(binario, cv2.MORPH_CLOSE, kernel_fechar, iterations=2)
        binario = cv2.morphologyEx(binario, cv2.MORPH_OPEN, kernel_abrir, iterations=1)

        return binario

    def encontrar_contornos_conidios(self, binario):
        """Encontra e filtra contornos que sao provavelmente conidios."""
        contornos, _ = cv2.findContours(
            binario, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        validos = []
        for cnt in contornos:
            area = cv2.contourArea(cnt)
            if area < self.min_area or area > self.max_area:
                continue

            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            solidez = area / max(hull_area, 1)
            if solidez < self.min_solidity:
                continue

            x, y, w, h = cv2.boundingRect(cnt)
            aspecto = max(w, h) / max(min(w, h), 1)
            if aspecto > self.max_aspect_ratio:
                continue

            validos.append(cnt)

        return validos

    def extrair_recorte(self, cinza, contorno):
        """Recorta um conidio individual com padding."""
        x, y, w, h = cv2.boundingRect(contorno)

        pad_x = int(w * self.pad_fraction)
        pad_y = int(h * self.pad_fraction)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(cinza.shape[1], x + w + pad_x)
        y2 = min(cinza.shape[0], y + h + pad_y)

        recorte = cinza[y1:y2, x1:x2]

        if self.output_size is not None:
            th, tw = self.output_size
            ch, cw = recorte.shape[:2]
            escala = min(tw / max(cw, 1), th / max(ch, 1))
            nw = int(cw * escala)
            nh = int(ch * escala)
            redimensionado = cv2.resize(recorte, (nw, nh), interpolation=cv2.INTER_AREA)

            canvas = np.full((th, tw), 255, dtype=np.uint8)
            yo = (th - nh) // 2
            xo = (tw - nw) // 2
            canvas[yo:yo + nh, xo:xo + nw] = redimensionado
            return canvas

        return recorte

    def segmentar_imagem(self, img):
        """Pipeline completo de segmentacao em uma imagem."""
        cinza = self.preprocessar(img)
        binario = self.binarizar(cinza)
        contornos = self.encontrar_contornos_conidios(binario)

        recortes = []
        for cnt in contornos:
            recorte = self.extrair_recorte(cinza, cnt)
            recortes.append(recorte)

        return recortes

    # -- Extracao de nomes de especies ------------------------------------

    def extrair_nome_especie(self, pagina):
        """Tenta extrair nome binomial do texto da pagina."""
        texto = pagina.get_text()
        nome = self._parse_binomial(texto)
        if nome:
            return nome

        # Fallback: OCR
        if pytesseract is not None:
            try:
                zoom = self.dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = pagina.get_pixmap(matrix=mat)
                img = self._pixmap_para_numpy(pix)
                ocr_texto = pytesseract.image_to_string(img)
                return self._parse_binomial(ocr_texto)
            except Exception:
                pass

        return None

    @staticmethod
    def _parse_binomial(texto):
        """Retorna primeiro 'Genero_especie' encontrado, ou None."""
        match = _BINOMIAL_RE.search(texto)
        if match:
            return f"{match.group(1)}_{match.group(2)}"
        return None

    # -- Processamento de PDF ---------------------------------------------

    def processar_pdf(self, caminho_pdf, pasta_saida):
        """Processa um PDF: extrai pranchas, segmenta conidios, salva PNGs."""
        doc = fitz.open(str(caminho_pdf))
        stats = {"paginas": 0, "imagens": 0, "conidios": 0}
        nome_pdf = Path(caminho_pdf).stem

        for idx_pg in range(len(doc)):
            pagina = doc[idx_pg]
            stats["paginas"] += 1

            especie = self.extrair_nome_especie(pagina) or nome_pdf

            pranchas = self.extrair_imagens_pagina(pagina, idx_pg)
            for idx_img, prancha in enumerate(pranchas):
                stats["imagens"] += 1
                recortes = self.segmentar_imagem(prancha)

                for idx_cond, recorte in enumerate(recortes):
                    nome_arquivo = (
                        f"{especie}_pg{idx_pg + 1:03d}"
                        f"_img{idx_img:02d}_cond{idx_cond:02d}.png"
                    )
                    cv2.imwrite(str(Path(pasta_saida) / nome_arquivo), recorte)
                    stats["conidios"] += 1

        doc.close()
        return stats

    def processar_lote(self, pasta_entrada, pasta_saida, filtro=None):
        """Processa todos os PDFs da pasta."""
        pasta_saida = Path(pasta_saida)
        pasta_saida.mkdir(parents=True, exist_ok=True)

        pdfs = self.listar_pdfs(pasta_entrada, filtro)

        totais = {
            "pdfs_total": len(pdfs),
            "pdfs_processados": 0,
            "paginas": 0,
            "imagens": 0,
            "conidios": 0,
            "falhas": 0,
        }

        for pdf in tqdm(pdfs, desc="Processando PDFs"):
            try:
                st = self.processar_pdf(pdf, pasta_saida)
                totais["pdfs_processados"] += 1
                totais["paginas"] += st["paginas"]
                totais["imagens"] += st["imagens"]
                totais["conidios"] += st["conidios"]
                logger.info(
                    f"{pdf.name}: {st['paginas']} pgs, "
                    f"{st['imagens']} imgs, {st['conidios']} conidios"
                )
            except Exception as e:
                logger.error(f"Falha ao processar {pdf.name}: {e}")
                totais["falhas"] += 1

        return totais


# -------------------------------------------------------------------------
# Execucao principal
# -------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extrator de Desenhos de Conidios de Hifomicetos Aquaticos"
    )
    parser.add_argument(
        "--input", default=None,
        help="Pasta com PDFs (padrao: pasta onde este script esta)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Pasta de saida (padrao: <input>/conidios_extraidos)"
    )
    parser.add_argument(
        "--dpi", type=int, default=300,
        help="Resolucao para rasterizacao (padrao: 300)"
    )
    parser.add_argument(
        "--min-area", type=int, default=100,
        help="Area minima do conidio em px^2 (padrao: 100)"
    )
    parser.add_argument(
        "--max-area", type=int, default=50000,
        help="Area maxima do conidio em px^2 (padrao: 50000)"
    )
    parser.add_argument(
        "--filter", default=None,
        help="Filtrar PDFs por substring no nome (ex: Araujo)"
    )
    parser.add_argument(
        "--size", type=int, default=None,
        help="Redimensionar recortes para SIZExSIZE pixels"
    )

    args = parser.parse_args()

    # Pasta de entrada: argumento ou pasta do script
    pasta_entrada = Path(args.input) if args.input else Path(__file__).parent
    pasta_saida = Path(args.output) if args.output else pasta_entrada / "conidios_extraidos"

    print("=" * 60)
    print("EXTRATOR DE DESENHOS DE CONIDIOS")
    print("Hifomicetos Aquaticos - Pipeline Automatizado")
    print("=" * 60)
    print(f"\n  Pasta de entrada : {pasta_entrada}")
    print(f"  Pasta de saida   : {pasta_saida}")
    print(f"  DPI              : {args.dpi}")
    print(f"  Area minima      : {args.min_area} px^2")
    print(f"  Area maxima      : {args.max_area} px^2")
    print(f"  Tamanho saida    : {f'{args.size}x{args.size}' if args.size else 'original'}")
    print(f"  Filtro           : {args.filter or 'nenhum'}")
    print(f"  Tesseract OCR    : {'disponivel' if pytesseract else 'nao instalado'}")

    extrator = ExtratorConidios(
        dpi=args.dpi,
        min_area=args.min_area,
        max_area=args.max_area,
        output_size=(args.size, args.size) if args.size else None,
    )

    stats = extrator.processar_lote(pasta_entrada, pasta_saida, args.filter)

    print()
    print("=" * 60)
    print("RESUMO DA EXECUCAO")
    print("=" * 60)
    print(f"  PDFs processados   : {stats['pdfs_processados']}/{stats['pdfs_total']}")
    print(f"  Paginas analisadas : {stats['paginas']}")
    print(f"  Imagens extraidas  : {stats['imagens']}")
    print(f"  Conidios extraidos : {stats['conidios']}")
    print(f"  Falhas             : {stats['falhas']}")
    print(f"  Pasta de saida     : {pasta_saida}")
    print("=" * 60)

    if stats["conidios"] > 0:
        print("\n  Processamento concluido com sucesso!")

        # Listar alguns arquivos gerados
        arquivos = sorted(pasta_saida.glob("*.png"))[:10]
        if arquivos:
            print(f"\n  Primeiros arquivos gerados ({len(list(pasta_saida.glob('*.png')))} total):")
            for a in arquivos:
                print(f"    - {a.name}")
            if len(list(pasta_saida.glob("*.png"))) > 10:
                print("    - ...")
    else:
        print("\n  Nenhum conidio extraido.")
        print("  Verifique se ha PDFs na pasta e ajuste os parametros.")
        print("  Dica: tente --dpi 600 ou --min-area 50")


if __name__ == "__main__":
    main()
