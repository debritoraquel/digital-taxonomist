"""
Preprocessing pipeline v3: percentile-based segmentation of individual
fungal specimens from TIFF microscopy images.

Replaces the flawed adaptive thresholding approach (v1-v2) with a robust
percentile-based method that correctly isolates individual conidial specimens
for ML training.

Usage:
    python src/preprocessing/segment_specimens.py --input data/raw/ --output data/processed/
"""

import logging
from pathlib import Path
from typing import Optional

import click
import cv2
import numpy as np
from tqdm import tqdm

try:
    import tifffile
except ImportError:
    tifffile = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SpecimenSegmenter:
    """
    Segments individual fungal specimens from TIFF microscopy images
    using a percentile-based thresholding approach.
    
    Pipeline stages:
    1. Load TIFF and convert to grayscale
    2. Apply percentile-based thresholding (robust to illumination variation)
    3. Morphological operations to clean binary mask
    4. Connected component analysis to isolate individual specimens
    5. Filter by area, aspect ratio, and solidity to reject debris
    6. Extract and pad individual specimen crops
    """

    def __init__(
        self,
        percentile_low: float = 5.0,
        percentile_high: float = 95.0,
        min_area_px: int = 500,
        max_area_px: int = 200000,
        min_solidity: float = 0.15,
        max_aspect_ratio: float = 15.0,
        pad_fraction: float = 0.15,
        output_size: tuple[int, int] = (224, 224),
    ):
        self.percentile_low = percentile_low
        self.percentile_high = percentile_high
        self.min_area_px = min_area_px
        self.max_area_px = max_area_px
        self.min_solidity = min_solidity
        self.max_aspect_ratio = max_aspect_ratio
        self.pad_fraction = pad_fraction
        self.output_size = output_size

    def load_image(self, path: Path) -> np.ndarray:
        """Load a TIFF or standard image file."""
        suffix = path.suffix.lower()
        if suffix in (".tif", ".tiff") and tifffile is not None:
            img = tifffile.imread(str(path))
        else:
            img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)

        if img is None:
            raise ValueError(f"Failed to load image: {path}")

        # Convert to grayscale if needed
        if len(img.shape) == 3:
            if img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
            else:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Normalize to 8-bit
        if img.dtype != np.uint8:
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

        return img

    def percentile_threshold(self, gray: np.ndarray) -> np.ndarray:
        """
        Apply percentile-based thresholding. More robust than adaptive
        thresholding for microscopy images with uneven illumination.
        """
        low = np.percentile(gray, self.percentile_low)
        high = np.percentile(gray, self.percentile_high)

        # Normalize to full range based on percentiles
        normalized = np.clip((gray.astype(float) - low) / max(high - low, 1) * 255, 0, 255).astype(np.uint8)

        # Otsu threshold on the normalized image
        _, binary = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        return binary

    def clean_mask(self, binary: np.ndarray) -> np.ndarray:
        """Morphological operations to clean the binary mask."""
        kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close, iterations=2)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel_open, iterations=1)

        return cleaned

    def filter_contour(self, contour: np.ndarray) -> bool:
        """Check if a contour passes quality filters for a valid specimen."""
        area = cv2.contourArea(contour)
        if area < self.min_area_px or area > self.max_area_px:
            return False

        hull = cv2.convexHull(contour)
        hull_area = cv2.contourArea(hull)
        solidity = area / max(hull_area, 1)
        if solidity < self.min_solidity:
            return False

        x, y, w, h = cv2.boundingRect(contour)
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect > self.max_aspect_ratio:
            return False

        return True

    def extract_specimen(
        self, gray: np.ndarray, contour: np.ndarray
    ) -> np.ndarray:
        """Extract and pad a single specimen crop, resized to output_size."""
        x, y, w, h = cv2.boundingRect(contour)

        # Add padding
        pad_x = int(w * self.pad_fraction)
        pad_y = int(h * self.pad_fraction)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(gray.shape[1], x + w + pad_x)
        y2 = min(gray.shape[0], y + h + pad_y)

        crop = gray[y1:y2, x1:x2]

        # Resize maintaining aspect ratio with padding
        target_h, target_w = self.output_size
        h_crop, w_crop = crop.shape[:2]
        scale = min(target_w / w_crop, target_h / h_crop)
        new_w = int(w_crop * scale)
        new_h = int(h_crop * scale)

        resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # Center on white canvas
        canvas = np.full((target_h, target_w), 255, dtype=np.uint8)
        y_off = (target_h - new_h) // 2
        x_off = (target_w - new_w) // 2
        canvas[y_off : y_off + new_h, x_off : x_off + new_w] = resized

        return canvas

    def segment(self, image_path: Path) -> list[np.ndarray]:
        """Full segmentation pipeline for a single image."""
        gray = self.load_image(image_path)
        binary = self.percentile_threshold(gray)
        cleaned = self.clean_mask(binary)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        specimens = []
        for contour in contours:
            if self.filter_contour(contour):
                specimen = self.extract_specimen(gray, contour)
                specimens.append(specimen)

        return specimens

    def process_directory(
        self,
        input_dir: Path,
        output_dir: Path,
        extensions: tuple[str, ...] = (".tif", ".tiff", ".png", ".jpg"),
    ) -> dict:
        """Process all images in a directory."""
        output_dir.mkdir(parents=True, exist_ok=True)

        image_files = []
        for ext in extensions:
            image_files.extend(input_dir.glob(f"*{ext}"))
            image_files.extend(input_dir.glob(f"*{ext.upper()}"))

        stats = {"total_images": len(image_files), "total_specimens": 0, "failed": 0}

        for img_path in tqdm(image_files, desc="Segmenting specimens"):
            try:
                specimens = self.segment(img_path)
                for i, spec in enumerate(specimens):
                    out_name = f"{img_path.stem}_specimen_{i:03d}.png"
                    cv2.imwrite(str(output_dir / out_name), spec)
                    stats["total_specimens"] += 1
            except Exception as e:
                logger.warning(f"Failed to process {img_path.name}: {e}")
                stats["failed"] += 1

        return stats


@click.command()
@click.option("--input", "input_dir", required=True, type=click.Path(exists=True))
@click.option("--output", "output_dir", required=True, type=click.Path())
@click.option("--size", default=224, help="Output image size (square)")
@click.option("--min-area", default=500, help="Minimum contour area in pixels")
def main(input_dir: str, output_dir: str, size: int, min_area: int):
    segmenter = SpecimenSegmenter(
        output_size=(size, size),
        min_area_px=min_area,
    )
    stats = segmenter.process_directory(Path(input_dir), Path(output_dir))

    click.echo(f"\n✓ Segmentation complete:")
    click.echo(f"  Images processed: {stats['total_images']}")
    click.echo(f"  Specimens extracted: {stats['total_specimens']}")
    click.echo(f"  Failed: {stats['failed']}")


if __name__ == "__main__":
    main()
