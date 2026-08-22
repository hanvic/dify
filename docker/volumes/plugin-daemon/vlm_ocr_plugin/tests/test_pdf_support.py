"""Tests for PDF support in the VLM OCR tool.

TDD tests: PDF detection, page order preservation, page limit enforcement,
compression target, existing image path regression, fail-fast on 429.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

# Make the plugin package importable from the tests directory.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from tools.vlm_ocr import (
    VlmOcrTool,
    OllamaRateLimitError,
    OllamaServerError,
    OcrContentQualityError,
    PdfPageLimitExceededError,
)


@pytest.fixture
def tool() -> VlmOcrTool:
    """Return a VlmOcrTool instance with mocked runtime/session."""
    mock_runtime = MagicMock()
    mock_runtime.credentials = {}
    mock_session = MagicMock()
    mock_session.session_id = "test-session"
    instance = VlmOcrTool.__new__(VlmOcrTool)
    instance.runtime = mock_runtime
    instance.session = mock_session
    return instance


def _make_sample_pdf(page_count: int = 3) -> bytes:
    """Create a minimal in-memory PDF with the specified number of pages."""
    import fitz

    doc = fitz.open()
    for i in range(page_count):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"페이지 {i + 1} 내용", fontsize=14)
        page.insert_text((72, 120), f"Page {i + 1} content for OCR testing", fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _make_image_bytes(width: int = 100, height: int = 100) -> bytes:
    """Create an in-memory JPEG image."""
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(
        output, format="JPEG", quality=85
    )
    return output.getvalue()


def _make_mock_file(
    filename: str = "test.jpg",
    extension: str = "jpg",
    mime_type: str = "image/jpeg",
    blob: bytes | None = None,
) -> MagicMock:
    """Create a mock File object."""
    from dify_plugin.file.file import File

    mock_file = MagicMock(spec=File)
    mock_file.filename = filename
    mock_file.extension = extension
    mock_file.mime_type = mime_type
    mock_file.url = f"http://localhost/{filename}"
    mock_file.blob = blob if blob is not None else _make_image_bytes()
    return mock_file


# ============================================================
# 1. PDF 감지: PDF MIME/extension → PDF 처리 경로 진입
# ============================================================


class TestPdfDetection:
    """PDF 파일이 들어올 때 PDF 처리 경로로 분기하는지 확인."""

    def test_detects_pdf_by_mime_type(self, tool: VlmOcrTool) -> None:
        """mime_type이 application/pdf이면 PDF로 처리."""
        pdf_bytes = _make_sample_pdf(1)
        mock_file = _make_mock_file(
            filename="doc.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )
        assert tool._is_pdf_file(mock_file) is True

    def test_detects_pdf_by_extension(self, tool: VlmOcrTool) -> None:
        """extension이 pdf이면 PDF로 처리 (MIME 없더라도)."""
        pdf_bytes = _make_sample_pdf(1)
        mock_file = _make_mock_file(
            filename="doc.pdf",
            extension="pdf",
            mime_type=None,
            blob=pdf_bytes,
        )
        # mime_type이 None이어도 확장자로 감지
        assert tool._is_pdf_file(mock_file) is True

    def test_image_not_detected_as_pdf(self, tool: VlmOcrTool) -> None:
        """일반 이미지는 PDF로 감지되지 않음."""
        mock_file = _make_mock_file()
        assert tool._is_pdf_file(mock_file) is False


# ============================================================
# 2. 페이지 순서 보장: 마크다운에서 p.1 → p.2 → p.3 순서
# ============================================================


class TestPageOrderPreservation:
    """PDF 페이지가 순서대로 마크다운에 나타나는지 확인."""

    def test_pages_in_order(self, tool: VlmOcrTool) -> None:
        """3페이지 PDF → 마크다운에 p.1, p.2, p.3 순서로 구분자가 나타남."""
        pdf_bytes = _make_sample_pdf(3)
        mock_file = _make_mock_file(
            filename="multi.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        # Mock _call_ollama to return page-specific text
        page_texts = [
            "첫 번째 페이지의 OCR 결과입니다. 충분히 긴 텍스트.",
            "두 번째 페이지의 OCR 결과입니다. 충분히 긴 텍스트.",
            "세 번째 페이지의 OCR 결과입니다. 충분히 긴 텍스트.",
        ]
        call_count = [0]

        def mock_call_ollama(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return page_texts[idx]

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=mock_call_ollama):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    result = tool._process_pdf(mock_file, tool_parameters={
                        "max_pages": 120,
                        "include_summary": False,
                        "enable_thinking": False,
                    })

        # 순서 확인: p.1이 p.2보다 앞에, p.2가 p.3보다 앞에
        pos1 = result.find("## 📄 p.1")
        pos2 = result.find("## 📄 p.2")
        pos3 = result.find("## 📄 p.3")
        assert pos1 < pos2 < pos3
        assert "첫 번째" in result
        assert "두 번째" in result
        assert "세 번째" in result


# ============================================================
# 3. 페이지 수 한도 초과 시 예외
# ============================================================


class TestPageLimitExceeded:
    """max_pages 초과 시 예외가 raise되는지 확인."""

    def test_exceeds_page_limit_raises_error(self, tool: VlmOcrTool) -> None:
        """5페이지 PDF에 max_pages=3 → 예외."""
        pdf_bytes = _make_sample_pdf(5)
        mock_file = _make_mock_file(
            filename="big.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        with pytest.raises(PdfPageLimitExceededError, match="초과|exceed|limit|한도"):
            tool._process_pdf(mock_file, tool_parameters={
                "max_pages": 3,
                "include_summary": False,
                "enable_thinking": False,
            })

    def test_within_page_limit_succeeds(self, tool: VlmOcrTool) -> None:
        """3페이지 PDF에 max_pages=5 → 정상 처리."""
        pdf_bytes = _make_sample_pdf(3)
        mock_file = _make_mock_file(
            filename="ok.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        page_texts = [
            f"페이지 {i+1}의 충분히 긴 OCR 결과입니다."
            for i in range(3)
        ]
        call_count = [0]

        def mock_call_ollama(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return page_texts[idx]

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=mock_call_ollama):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    result = tool._process_pdf(mock_file, tool_parameters={
                        "max_pages": 5,
                        "include_summary": False,
                        "enable_thinking": False,
                    })

        assert "페이지 1" in result or "p.1" in result


# ============================================================
# 4. 압축 범위: 렌더링된 페이지 이미지가 250KB 이하
# ============================================================


class TestCompressionTarget:
    """PDF 페이지 렌더링 후 JPEG 크기가 250KB 이하인지 확인."""

    def test_rendered_page_within_size_limit(self, tool: VlmOcrTool) -> None:
        """렌더링된 페이지 이미지가 250KB 이하."""
        pdf_bytes = _make_sample_pdf(1)
        mock_file = _make_mock_file(
            filename="test.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        rendered_sizes = []
        original_call = None

        def capture_call_ollama(**kwargs):
            # raw_base64의 크기로 원본 이미지 크기 추정
            import base64
            raw = kwargs.get("raw_base64", "")
            decoded_size = len(base64.b64decode(raw))
            rendered_sizes.append(decoded_size)
            return "OCR 결과 텍스트 - 충분히 긴 내용입니다."

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=capture_call_ollama):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    tool._process_pdf(mock_file, tool_parameters={
                        "max_pages": 120,
                        "include_summary": False,
                        "enable_thinking": False,
                    })

        # 250KB = 256000 bytes
        for size in rendered_sizes:
            assert size <= 256000, f"렌더링된 이미지가 250KB를 초과: {size} bytes"


# ============================================================
# 5. 기존 이미지 경로 회귀: 이미지 파일은 여전히 정상 작동
# ============================================================


class TestImagePathRegression:
    """기존 이미지 처리 경로가 PDF 추가 후에도 정상 동작."""

    def test_jpeg_not_treated_as_pdf(self, tool: VlmOcrTool) -> None:
        """JPEG 파일은 PDF로 감지되지 않고 기존 경로로 처리됨."""
        mock_file = _make_mock_file()
        # JPEG는 PDF가 아님
        assert tool._is_pdf_file(mock_file) is False

    def test_image_prepare_still_works(self, tool: VlmOcrTool) -> None:
        """기존 이미지 처리 로직(_prepare_image_bytes)이 정상 동작."""
        image_bytes = _make_image_bytes(800, 600)
        prepared, resized, width, height = tool._prepare_image_bytes(image_bytes)
        assert resized is False
        assert width == 800
        assert height == 600
        assert len(prepared) > 0

    def test_call_ollama_still_works_for_images(self, tool: VlmOcrTool) -> None:
        """이미지 OCR 결과가 기존처럼 정상 반환됨."""
        expected_text = "기존 이미지 OCR 결과입니다. 충분히 긴 텍스트로 검증합니다."
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": expected_text}
        }

        with patch("requests.post", return_value=mock_response):
            result = tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="qwen3.5:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )
        assert result == expected_text


# ============================================================
# 6. Fail-fast 유지: PDF 처리 중 429 → 예외 전파
# ============================================================


class TestPdfFailFast:
    """PDF 처리 중 Ollama 429가 발생하면 예외가 전파됨."""

    def test_429_during_pdf_processing_raises(self, tool: VlmOcrTool) -> None:
        """PDF 2페이지 처리 중 첫 페이지에서 429 발생 → 예외."""
        pdf_bytes = _make_sample_pdf(2)
        mock_file = _make_mock_file(
            filename="doc.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        def mock_call_ollama_429(**kwargs):
            raise OllamaRateLimitError("쿼터 초과 429")

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=mock_call_ollama_429):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    with pytest.raises(OllamaRateLimitError):
                        tool._process_pdf(mock_file, tool_parameters={
                            "max_pages": 120,
                            "include_summary": False,
                            "enable_thinking": False,
                        })

    def test_server_error_during_pdf_raises(self, tool: VlmOcrTool) -> None:
        """PDF 처리 중 5xx 에러 → OllamaServerError 전파."""
        pdf_bytes = _make_sample_pdf(2)
        mock_file = _make_mock_file(
            filename="doc.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        def mock_call_ollama_500(**kwargs):
            raise OllamaServerError("서버 에러 500")

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=mock_call_ollama_500):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    with pytest.raises(OllamaServerError):
                        tool._process_pdf(mock_file, tool_parameters={
                            "max_pages": 120,
                            "include_summary": False,
                            "enable_thinking": False,
                        })


# ============================================================
# 7. 페이지 구분자 형식 검증
# ============================================================


class TestPageSeparator:
    """마크다운에 페이지 구분자가 올바른 형식으로 포함."""

    def test_page_separator_format(self, tool: VlmOcrTool) -> None:
        """각 페이지 앞에 '## 📄 p.N' 형식 구분자가 있음."""
        pdf_bytes = _make_sample_pdf(2)
        mock_file = _make_mock_file(
            filename="doc.pdf",
            extension="pdf",
            mime_type="application/pdf",
            blob=pdf_bytes,
        )

        def mock_call_ollama(**kwargs):
            return "충분히 긴 OCR 결과 텍스트입니다. 페이지 내용."

        from tools.blank_detector import BlankDetectionResult
        not_blank = BlankDetectionResult(is_blank=False, method="none")

        with patch.object(tool, "_call_ollama", side_effect=mock_call_ollama):
            with patch.object(tool, "_resolve_ollama_config", return_value=("model", "http://localhost:11434", False)):
                with patch("tools.vlm_ocr.detect_blank_pre", return_value=not_blank):
                    result = tool._process_pdf(mock_file, tool_parameters={
                        "max_pages": 120,
                        "include_summary": False,
                        "enable_thinking": False,
                    })

        assert "## 📄 p.1" in result
        assert "## 📄 p.2" in result
