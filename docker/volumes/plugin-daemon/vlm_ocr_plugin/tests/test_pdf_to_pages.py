"""Tests for pdf_to_pages.py page extraction and compression."""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pdf_to_pages import (
    DEFAULT_MAX_LONG_SIDE,
    DEFAULT_QUALITY,
    TARGET_SIZE_BYTES,
    get_page_count,
    render_page_to_jpeg,
)


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Create a minimal single-page PDF for testing."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    page.insert_text((72, 72), "테스트 문서 페이지 1", fontsize=14)
    page.insert_text((72, 120), "희망브리지 문서전자화 프로젝트", fontsize=12)

    pdf_path = tmp_path / "test.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def multi_page_pdf(tmp_path: Path) -> Path:
    """Create a 5-page PDF for testing."""
    import fitz

    doc = fitz.open()
    for i in range(5):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"페이지 {i + 1}", fontsize=14)

    pdf_path = tmp_path / "multi.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


class TestGetPageCount:
    def test_single_page(self, sample_pdf: Path) -> None:
        assert get_page_count(sample_pdf) == 1

    def test_multi_page(self, multi_page_pdf: Path) -> None:
        assert get_page_count(multi_page_pdf) == 5

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            get_page_count(tmp_path / "nonexistent.pdf")


class TestRenderPageToJpeg:
    def test_renders_first_page(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.jpg"
        size = render_page_to_jpeg(sample_pdf, 0, output)

        assert output.exists()
        assert size > 0
        assert size == output.stat().st_size

    def test_output_is_valid_jpeg(self, sample_pdf: Path, tmp_path: Path) -> None:
        from PIL import Image

        output = tmp_path / "out.jpg"
        render_page_to_jpeg(sample_pdf, 0, output)

        img = Image.open(output)
        assert img.format == "JPEG"
        assert img.mode == "RGB"

    def test_respects_max_long_side(self, sample_pdf: Path, tmp_path: Path) -> None:
        from PIL import Image

        output = tmp_path / "out.jpg"
        render_page_to_jpeg(sample_pdf, 0, output, max_long_side=512)

        img = Image.open(output)
        assert max(img.size) <= 512

    def test_output_within_size_target(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.jpg"
        size = render_page_to_jpeg(sample_pdf, 0, output)

        assert size <= TARGET_SIZE_BYTES

    def test_invalid_page_number(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.jpg"
        with pytest.raises(ValueError, match="범위를 벗어났습니다"):
            render_page_to_jpeg(sample_pdf, 5, output)

    def test_negative_page_number(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.jpg"
        with pytest.raises(ValueError, match="범위를 벗어났습니다"):
            render_page_to_jpeg(sample_pdf, -1, output)

    def test_missing_pdf(self, tmp_path: Path) -> None:
        output = tmp_path / "out.jpg"
        with pytest.raises(FileNotFoundError):
            render_page_to_jpeg(tmp_path / "gone.pdf", 0, output)

    def test_multi_page_extraction(self, multi_page_pdf: Path, tmp_path: Path) -> None:
        for i in range(5):
            output = tmp_path / f"page_{i}.jpg"
            size = render_page_to_jpeg(multi_page_pdf, i, output)
            assert output.exists()
            assert size > 0

    def test_creates_parent_dirs(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "deep" / "nested" / "out.jpg"
        render_page_to_jpeg(sample_pdf, 0, output)
        assert output.exists()


class TestCLI:
    def test_page_count_flag(self, multi_page_pdf: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "pdf_to_pages.py"),
             str(multi_page_pdf), "0", "/dev/null", "--page-count"],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "5"

    def test_render_page_cli(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "cli_out.jpg"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "pdf_to_pages.py"),
             str(sample_pdf), "0", str(output)],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
        assert output.exists()
        # stdout should contain the size in bytes
        size_str = result.stdout.strip()
        assert size_str.isdigit()

    def test_invalid_page_cli(self, sample_pdf: Path, tmp_path: Path) -> None:
        output = tmp_path / "bad.jpg"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "pdf_to_pages.py"),
             str(sample_pdf), "99", str(output)],
            capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 1
        assert "오류" in result.stderr
