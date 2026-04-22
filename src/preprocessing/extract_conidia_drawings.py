"""
Extractor for conidia drawings from scientific PDF publications on
aquatic hyphomycetes.

Automates the pipeline:
1. Iterate PDFs in a directory
2. Extract plate images (drawings) from each page
3. Apply threshold, denoise, enhance contrast
4. Segment individual conidia via OpenCV contour detection
5. Attempt species name extraction from captions via OCR
6. Save isolated PNG images ready for analysis or vectorisation

Usage:
    python src/preprocessing/extract_conidia_drawings.py --input data/pdfs/ --output data/conidia/
    python src/preprocessing/extract_conidia_drawings.py --input data/pdfs/ --filter Araujo
    python src/preprocessing/extract_conidia_drawings.py --input data/pdfs/ --dpi 600
"""

import logging
import re
from pathlib import Path
from typing import Optional

import click
import cv2
import numpy as np
from tqdm import tqdm

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Regex for binomial nomenclature (Genus species).
# Requires genus >= 4 chars, epithet >= 4 chars, and the epithet must NOT be
# followed by a hyphen (to avoid matching "marchalianum-like" style suffixes).
_BINOMIAL_RE = re.compile(
    r"\b([A-Z][a-z]{3,})\s+([a-z]{4,})\b(?!-)"
)


class ConidiaDrawingExtractor:
    """
    Extracts individual conidia drawings from scientific PDF plates.

    Designed for black-ink drawings on white background, typical of
    aquatic hyphomycete taxonomic publications.
    """

    def __init__(
        self,
        dpi: int = 300,
        min_area: int = 100,
        max_area: int = 50000,
        min_solidity: float = 0.1,
        max_aspect_ratio: float = 20.0,
        pad_fraction: float = 0.12,
        output_size: Optional[tuple[int, int]] = None,
        min_image_area: int = 10000,
    ):
        """
        Args:
            dpi: Resolution for PDF page rasterisation.
            min_area: Minimum contour area (px^2) to accept as a conidium.
            max_area: Maximum contour area (px^2).
            min_solidity: Minimum solidity (area / convex-hull area).
            max_aspect_ratio: Maximum bounding-box aspect ratio.
            pad_fraction: Fractional padding around each extracted conidium.
            output_size: If set, resize each crop to (H, W). None keeps original.
            min_image_area: Minimum area for an embedded image to be considered a plate.
        """
        if fitz is None:
            raise ImportError(
                "PyMuPDF is required. Install with: pip install PyMuPDF"
            )

        self.dpi = dpi
        self.min_area = min_area
        self.max_area = max_area
        self.min_solidity = min_solidity
        self.max_aspect_ratio = max_aspect_ratio
        self.pad_fraction = pad_fraction
        self.output_size = output_size
        self.min_image_area = min_image_area

    # ------------------------------------------------------------------
    # PDF discovery
    # ------------------------------------------------------------------

    def list_pdfs(self, directory: Path, filter_name: Optional[str] = None) -> list[Path]:
        """List PDF files in *directory*, optionally filtering by substring."""
        pdfs = sorted(directory.glob("*.pdf")) + sorted(directory.glob("*.PDF"))
        if filter_name:
            pdfs = [p for p in pdfs if filter_name.lower() in p.stem.lower()]
        logger.info(f"Found {len(pdfs)} PDF(s) in {directory}" +
                     (f" (filter='{filter_name}')" if filter_name else ""))
        return pdfs

    # ------------------------------------------------------------------
    # Image extraction from PDF
    # ------------------------------------------------------------------

    def _pixmap_to_numpy(self, pixmap: "fitz.Pixmap") -> np.ndarray:
        """Convert a PyMuPDF Pixmap to a NumPy BGR array."""
        if pixmap.alpha:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)  # drop alpha
        data = np.frombuffer(pixmap.samples, dtype=np.uint8)
        img = data.reshape(pixmap.height, pixmap.width, pixmap.n)
        if img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img

    def extract_images_from_page(
        self, page: "fitz.Page", page_index: int
    ) -> list[np.ndarray]:
        """
        Extract plate images from a PDF page.

        Uses two strategies:
        1. Extract embedded images via ``page.get_images()``
        2. If no large images found, rasterise the full page
        """
        doc = page.parent
        images: list[np.ndarray] = []

        # Strategy 1: embedded images
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                if base_image is None:
                    continue
                img_bytes = base_image["image"]
                arr = np.frombuffer(img_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None and img.shape[0] * img.shape[1] >= self.min_image_area:
                    images.append(img)
            except Exception as exc:
                logger.debug(f"Page {page_index + 1}, xref {xref}: {exc}")

        # Strategy 2: full-page rasterisation
        if not images:
            zoom = self.dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img = self._pixmap_to_numpy(pix)
            if img.shape[0] * img.shape[1] >= self.min_image_area:
                images.append(img)
                logger.debug(f"Page {page_index + 1}: rasterised at {self.dpi} DPI")

        return images

    # ------------------------------------------------------------------
    # Image processing & segmentation
    # ------------------------------------------------------------------

    def preprocess(self, img: np.ndarray) -> np.ndarray:
        """Convert to grayscale, enhance contrast, reduce noise."""
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img.copy()

        # CLAHE for local contrast enhancement
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        # Light Gaussian blur to reduce scanning noise
        denoised = cv2.GaussianBlur(enhanced, (3, 3), 0)

        return denoised

    def binarise(self, gray: np.ndarray) -> np.ndarray:
        """Otsu binarisation (inverted: foreground = white)."""
        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )

        # Morphological close to bridge small gaps, then open to remove specks
        kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=2)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel_open, iterations=1)

        return binary

    def find_conidia_contours(self, binary: np.ndarray) -> list[np.ndarray]:
        """Find and filter contours that are likely individual conidia."""
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        valid = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self.min_area or area > self.max_area:
                continue

            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            solidity = area / max(hull_area, 1)
            if solidity < self.min_solidity:
                continue

            x, y, w, h = cv2.boundingRect(cnt)
            aspect = max(w, h) / max(min(w, h), 1)
            if aspect > self.max_aspect_ratio:
                continue

            valid.append(cnt)

        return valid

    def extract_crop(self, gray: np.ndarray, contour: np.ndarray) -> np.ndarray:
        """Extract a padded crop around *contour* from the grayscale image."""
        x, y, w, h = cv2.boundingRect(contour)

        pad_x = int(w * self.pad_fraction)
        pad_y = int(h * self.pad_fraction)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(gray.shape[1], x + w + pad_x)
        y2 = min(gray.shape[0], y + h + pad_y)

        crop = gray[y1:y2, x1:x2]

        if self.output_size is not None:
            th, tw = self.output_size
            ch, cw = crop.shape[:2]
            scale = min(tw / max(cw, 1), th / max(ch, 1))
            new_w = int(cw * scale)
            new_h = int(ch * scale)
            resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)

            canvas = np.full((th, tw), 255, dtype=np.uint8)
            y_off = (th - new_h) // 2
            x_off = (tw - new_w) // 2
            canvas[y_off : y_off + new_h, x_off : x_off + new_w] = resized
            return canvas

        return crop

    def segment_image(self, img: np.ndarray) -> list[np.ndarray]:
        """Full segmentation pipeline on a single plate image."""
        gray = self.preprocess(img)
        binary = self.binarise(gray)
        contours = self.find_conidia_contours(binary)

        crops: list[np.ndarray] = []
        for cnt in contours:
            crop = self.extract_crop(gray, cnt)
            crops.append(crop)

        return crops

    # ------------------------------------------------------------------
    # Species name extraction (OCR)
    # ------------------------------------------------------------------

    def extract_species_name(self, page: "fitz.Page") -> Optional[str]:
        """
        Try to extract a binomial species name from the page text.

        First uses PyMuPDF's built-in text extraction. Falls back to
        Tesseract OCR if no name is found and pytesseract is available.
        """
        text = page.get_text()
        name = self._parse_binomial(text)
        if name:
            return name

        # Fallback: OCR on the rasterised page
        if pytesseract is not None:
            try:
                zoom = self.dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                img = self._pixmap_to_numpy(pix)
                ocr_text = pytesseract.image_to_string(img)
                return self._parse_binomial(ocr_text)
            except Exception as exc:
                logger.debug(f"OCR fallback failed: {exc}")

        return None

    @staticmethod
    def _parse_binomial(text: str) -> Optional[str]:
        """Return the first ``Genus_species`` match, or None."""
        match = _BINOMIAL_RE.search(text)
        if match:
            return f"{match.group(1)}_{match.group(2)}"
        return None

    # ------------------------------------------------------------------
    # PDF processing
    # ------------------------------------------------------------------

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> dict:
        """
        Process a single PDF: extract plates, segment conidia, save PNGs.

        Returns a stats dict with page/image/conidium counts.
        """
        doc = fitz.open(str(pdf_path))
        stats = {"pages": 0, "images": 0, "conidia": 0}
        pdf_stem = pdf_path.stem

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            stats["pages"] += 1

            species = self.extract_species_name(page) or pdf_stem

            plate_images = self.extract_images_from_page(page, page_idx)
            for img_idx, plate in enumerate(plate_images):
                stats["images"] += 1
                crops = self.segment_image(plate)

                for cond_idx, crop in enumerate(crops):
                    filename = (
                        f"{species}_pg{page_idx + 1:03d}"
                        f"_img{img_idx:02d}_cond{cond_idx:02d}.png"
                    )
                    cv2.imwrite(str(output_dir / filename), crop)
                    stats["conidia"] += 1

        doc.close()
        return stats

    def process_batch(
        self,
        input_dir: Path,
        output_dir: Path,
        filter_name: Optional[str] = None,
    ) -> dict:
        """
        Process all PDFs in *input_dir*.

        Returns aggregate stats.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        pdfs = self.list_pdfs(input_dir, filter_name)

        totals = {
            "pdfs_total": len(pdfs),
            "pdfs_processed": 0,
            "pages": 0,
            "images": 0,
            "conidia": 0,
            "failed": 0,
        }

        for pdf_path in tqdm(pdfs, desc="Processing PDFs"):
            try:
                st = self.process_pdf(pdf_path, output_dir)
                totals["pdfs_processed"] += 1
                totals["pages"] += st["pages"]
                totals["images"] += st["images"]
                totals["conidia"] += st["conidia"]
                logger.info(
                    f"{pdf_path.name}: {st['pages']} pages, "
                    f"{st['images']} images, {st['conidia']} conidia"
                )
            except Exception as exc:
                logger.error(f"Failed to process {pdf_path.name}: {exc}")
                totals["failed"] += 1

        return totals


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


@click.command()
@click.option(
    "--input", "input_dir", required=True,
    type=click.Path(exists=True),
    help="Directory containing PDF files.",
)
@click.option(
    "--output", "output_dir", default=None,
    type=click.Path(),
    help="Output directory for extracted conidia PNGs (default: <input>/conidios_extraidos).",
)
@click.option("--dpi", default=300, help="DPI for page rasterisation (default 300).")
@click.option("--min-area", default=100, help="Minimum conidium contour area in px^2.")
@click.option("--max-area", default=50000, help="Maximum conidium contour area in px^2.")
@click.option("--filter", "filter_name", default=None, help="Substring filter for PDF filenames.")
@click.option("--size", default=None, type=int, help="Resize output crops to SIZExSIZE.")
def main(
    input_dir: str,
    output_dir: Optional[str],
    dpi: int,
    min_area: int,
    max_area: int,
    filter_name: Optional[str],
    size: Optional[int],
):
    input_path = Path(input_dir)
    output_path = Path(output_dir) if output_dir else input_path / "conidios_extraidos"

    extractor = ConidiaDrawingExtractor(
        dpi=dpi,
        min_area=min_area,
        max_area=max_area,
        output_size=(size, size) if size else None,
    )

    stats = extractor.process_batch(input_path, output_path, filter_name)

    click.echo("")
    click.echo("=" * 60)
    click.echo("RESUMO DA EXECUCAO")
    click.echo("=" * 60)
    click.echo(f"PDFs processados: {stats['pdfs_processed']}/{stats['pdfs_total']}")
    click.echo(f"Paginas analisadas: {stats['pages']}")
    click.echo(f"Imagens extraidas: {stats['images']}")
    click.echo(f"Conidios extraidos: {stats['conidia']}")
    click.echo(f"Falhas: {stats['failed']}")
    click.echo(f"Pasta de saida: {output_path}")
    click.echo("=" * 60)

    if stats["conidia"] > 0:
        click.echo("\nProcessamento concluido!")
    else:
        click.echo("\nNenhum conidio extraido. Verifique os PDFs e parametros.")


if __name__ == "__main__":
    main()
