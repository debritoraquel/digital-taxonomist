"""Tests for the conidia drawing extractor module."""

import numpy as np
import pytest

from preprocessing.extract_conidia_drawings import ConidiaDrawingExtractor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def extractor():
    """Extractor with default parameters (requires PyMuPDF)."""
    pytest.importorskip("fitz", reason="PyMuPDF not installed")
    return ConidiaDrawingExtractor()


@pytest.fixture
def white_plate():
    """800x600 white image with three black rectangles (fake conidia)."""
    img = np.full((600, 800, 3), 255, dtype=np.uint8)
    # Rectangle 1: 40x60 at (100, 100)
    cv2.rectangle(img, (100, 100), (140, 160), (0, 0, 0), -1)
    # Rectangle 2: 50x80 at (300, 200)
    cv2.rectangle(img, (300, 200), (350, 280), (0, 0, 0), -1)
    # Rectangle 3: 30x45 at (500, 350)
    cv2.rectangle(img, (500, 350), (530, 395), (0, 0, 0), -1)
    return img


@pytest.fixture
def gray_plate(white_plate):
    """Grayscale version of the white plate."""
    return cv2.cvtColor(white_plate, cv2.COLOR_BGR2GRAY)


try:
    import cv2
except ImportError:
    cv2 = None

pytestmark = pytest.mark.skipif(cv2 is None, reason="OpenCV not installed")


# ---------------------------------------------------------------------------
# Binomial parsing
# ---------------------------------------------------------------------------


class TestBinomialParsing:
    def test_basic_binomial(self):
        result = ConidiaDrawingExtractor._parse_binomial(
            "Fig. 1. Anguillospora longissima (drawing)"
        )
        assert result == "Anguillospora_longissima"

    def test_hyphenated_suffix_ignored(self):
        """Hyphenated suffixes like 'marchalianum-like' should not match."""
        result = ConidiaDrawingExtractor._parse_binomial(
            "Plate 2: Tetracladium marchalianum-like conidia"
        )
        assert result is None

    def test_no_match(self):
        result = ConidiaDrawingExtractor._parse_binomial("Figure 3. Scale bar = 10 um")
        assert result is None

    def test_ignores_short_words(self):
        result = ConidiaDrawingExtractor._parse_binomial("In situ observation")
        assert result is None

    def test_multiple_names_returns_first(self):
        result = ConidiaDrawingExtractor._parse_binomial(
            "Campylospora chaetocladia and Lunulospora curvula"
        )
        assert result == "Campylospora_chaetocladia"


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------


class TestPreprocessing:
    def test_preprocess_returns_grayscale(self, extractor, white_plate):
        result = extractor.preprocess(white_plate)
        assert len(result.shape) == 2
        assert result.dtype == np.uint8

    def test_preprocess_already_gray(self, extractor, gray_plate):
        result = extractor.preprocess(gray_plate)
        assert len(result.shape) == 2

    def test_binarise_output_is_binary(self, extractor, gray_plate):
        binary = extractor.binarise(gray_plate)
        unique = set(np.unique(binary))
        assert unique <= {0, 255}


# ---------------------------------------------------------------------------
# Contour detection
# ---------------------------------------------------------------------------


class TestContourDetection:
    def test_finds_shapes_on_white_plate(self, extractor, white_plate):
        gray = extractor.preprocess(white_plate)
        binary = extractor.binarise(gray)
        contours = extractor.find_conidia_contours(binary)
        assert len(contours) == 3

    def test_rejects_tiny_contours(self, extractor):
        # Image with a 3x3 dot (area ~9, below min_area=100)
        img = np.full((200, 200), 255, dtype=np.uint8)
        cv2.rectangle(img, (90, 90), (93, 93), 0, -1)
        binary = extractor.binarise(img)
        contours = extractor.find_conidia_contours(binary)
        assert len(contours) == 0

    def test_rejects_oversized_contours(self):
        ext = ConidiaDrawingExtractor.__new__(ConidiaDrawingExtractor)
        ext.min_area = 100
        ext.max_area = 500
        ext.min_solidity = 0.1
        ext.max_aspect_ratio = 20.0
        # 100x100 filled rectangle = area 10000
        img = np.full((300, 300), 255, dtype=np.uint8)
        cv2.rectangle(img, (50, 50), (150, 150), 0, -1)
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY_INV)
        contours = ext.find_conidia_contours(binary)
        assert len(contours) == 0


# ---------------------------------------------------------------------------
# Crop extraction
# ---------------------------------------------------------------------------


class TestCropExtraction:
    def test_crop_has_content(self, extractor, white_plate):
        gray = extractor.preprocess(white_plate)
        binary = extractor.binarise(gray)
        contours = extractor.find_conidia_contours(binary)
        assert len(contours) > 0
        crop = extractor.extract_crop(gray, contours[0])
        assert crop.shape[0] > 0 and crop.shape[1] > 0

    def test_crop_with_output_size(self, white_plate):
        pytest.importorskip("fitz", reason="PyMuPDF not installed")
        ext = ConidiaDrawingExtractor(output_size=(128, 128))
        gray = ext.preprocess(white_plate)
        binary = ext.binarise(gray)
        contours = ext.find_conidia_contours(binary)
        crop = ext.extract_crop(gray, contours[0])
        assert crop.shape == (128, 128)


# ---------------------------------------------------------------------------
# Full segmentation pipeline
# ---------------------------------------------------------------------------


class TestSegmentImage:
    def test_segment_returns_crops(self, extractor, white_plate):
        crops = extractor.segment_image(white_plate)
        assert len(crops) == 3
        for c in crops:
            assert isinstance(c, np.ndarray)
            assert len(c.shape) == 2  # grayscale

    def test_blank_image_returns_empty(self, extractor):
        blank = np.full((400, 400, 3), 255, dtype=np.uint8)
        crops = extractor.segment_image(blank)
        assert len(crops) == 0


# ---------------------------------------------------------------------------
# PDF listing
# ---------------------------------------------------------------------------


class TestPdfDiscovery:
    def test_list_pdfs_empty_dir(self, extractor, tmp_path):
        pdfs = extractor.list_pdfs(tmp_path)
        assert pdfs == []

    def test_list_pdfs_with_filter(self, extractor, tmp_path):
        (tmp_path / "Araujo_2020.pdf").touch()
        (tmp_path / "Santos_2019.pdf").touch()
        pdfs = extractor.list_pdfs(tmp_path, filter_name="Araujo")
        assert len(pdfs) == 1
        assert "Araujo" in pdfs[0].name

    def test_list_pdfs_no_filter(self, extractor, tmp_path):
        (tmp_path / "a.pdf").touch()
        (tmp_path / "b.pdf").touch()
        pdfs = extractor.list_pdfs(tmp_path)
        assert len(pdfs) == 2
