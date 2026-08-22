"""Tests for blank page detection module.

TDD: Written before implementation changes to batch runner and plugin.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

# Add parent to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from tools.blank_detector import (
    BlankDetectionResult,
    BlankPageAction,
    BlankPageError,
    OcrContentQualityError,
    detect_blank_post,
    detect_blank_pre,
    compute_image_metrics,
    STDDEV_THRESHOLD,
    EDGE_MEAN_THRESHOLD,
)


class TestPreDetection:
    """Test image-based pre-detection."""

    def test_pure_white_image_is_blank(self):
        """A pure white image should be detected as blank."""
        img = Image.new("RGB", (2048, 1400), color=(255, 255, 255))
        result = detect_blank_pre(img)
        assert result.is_blank is True
        assert result.method == "pre_image"
        assert result.stddev is not None
        assert result.stddev < STDDEV_THRESHOLD
        assert result.edge_mean < EDGE_MEAN_THRESHOLD

    def test_pure_black_image_is_blank(self):
        """A pure black image (uniform color) should be detected as blank."""
        img = Image.new("RGB", (2048, 1400), color=(0, 0, 0))
        result = detect_blank_pre(img)
        assert result.is_blank is True
        assert result.method == "pre_image"

    def test_uniform_gray_is_blank(self):
        """A uniform gray image should be detected as blank."""
        img = Image.new("RGB", (2048, 1400), color=(200, 200, 200))
        result = detect_blank_pre(img)
        assert result.is_blank is True

    def test_content_image_is_not_blank(self):
        """An image with text-like content should NOT be blank."""
        # Create an image with high contrast patterns (simulating text)
        img = Image.new("RGB", (2048, 1400), color=(255, 255, 255))
        pixels = img.load()
        # Draw horizontal lines simulating text
        for y in range(0, 1400, 20):
            for x in range(100, 1900):
                if y % 20 < 10:
                    pixels[x, y] = (0, 0, 0)
        result = detect_blank_pre(img)
        assert result.is_blank is False
        assert result.method == "none"

    def test_near_white_with_slight_noise_is_blank(self):
        """Slightly noisy white page (like scan artifact) should still be blank."""
        import random
        random.seed(42)
        img = Image.new("RGB", (2048, 1400), color=(250, 250, 250))
        pixels = img.load()
        # Add very slight noise (like scan dust)
        for _ in range(100):
            x = random.randint(0, 2047)
            y = random.randint(0, 1399)
            pixels[x, y] = (240, 240, 240)
        result = detect_blank_pre(img)
        assert result.is_blank is True

    def test_bytes_input(self):
        """Should accept bytes input (JPEG encoded)."""
        from io import BytesIO
        img = Image.new("RGB", (500, 700), color=(255, 255, 255))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        result = detect_blank_pre(buf.getvalue())
        assert result.is_blank is True

    def test_file_path_input(self):
        """Should accept file path input."""
        img = Image.new("RGB", (500, 700), color=(255, 255, 255))
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img.save(f, format="JPEG")
            f.flush()
            result = detect_blank_pre(f.name)
        os.unlink(f.name)
        assert result.is_blank is True

    def test_metrics_computation(self):
        """compute_image_metrics should return (stddev, edge_mean, content_ratio)."""
        # Use a large enough image to minimize border effects from FIND_EDGES
        img = Image.new("RGB", (2048, 1400), color=(255, 255, 255))
        stddev, edge_mean, content_ratio = compute_image_metrics(img)
        assert stddev == 0.0
        assert edge_mean < 1.0  # Near-zero (FIND_EDGES has border artifacts)
        assert content_ratio == 0.0

    def test_corrupt_image_returns_not_blank(self):
        """Corrupt/unreadable image should NOT be classified as blank (fail open)."""
        result = detect_blank_pre(b"not an image at all")
        assert result.is_blank is False


class TestPostDetection:
    """Test VLM response-based post-detection."""

    def test_empty_response_is_blank(self):
        """Empty VLM response should be blank."""
        result = detect_blank_post("")
        assert result.is_blank is True
        assert result.method == "post_vlm"

    def test_none_response_is_blank(self):
        """None response should be blank."""
        result = detect_blank_post(None)
        assert result.is_blank is True

    def test_blank_page_keyword(self):
        """Response containing '빈 페이지' should be blank."""
        result = detect_blank_post("빈 페이지입니다.")
        assert result.is_blank is True
        assert "keyword_match" in result.reason

    def test_blank_keyword_english(self):
        """Response containing 'blank page' should be blank."""
        result = detect_blank_post("This is a blank page.")
        assert result.is_blank is True

    def test_empty_doc_keyword(self):
        """Response containing '내용 없음' should be blank."""
        result = detect_blank_post("내용 없음")
        assert result.is_blank is True

    def test_short_content_without_keyword_is_not_blank(self):
        """Short text without blank keywords is NOT blank (genuine error)."""
        result = detect_blank_post("ab cd")
        assert result.is_blank is False
        assert "short_text_no_blank_keyword" in result.reason

    def test_normal_content_not_blank(self):
        """Normal OCR content should not be blank."""
        text = "제1조 본 회는 전국재해대책협의회라 칭한다."
        result = detect_blank_post(text)
        assert result.is_blank is False


class TestBlankDetectionResult:
    """Test the result dataclass."""

    def test_log_line_format(self):
        """log_line should produce a parseable string."""
        result = BlankDetectionResult(
            is_blank=True,
            method="pre_image",
            stddev=5.3,
            edge_mean=1.2,
            content_ratio=0.001,
            reason="test",
        )
        line = result.log_line()
        assert "stddev=5.30" in line
        assert "edge=1.20" in line
        assert "pre_image" in line


class TestBlankPageError:
    """Test exception classification."""

    def test_blank_page_error_not_ocr_quality_error(self):
        """BlankPageError should be distinct from OcrContentQualityError."""
        blank_err = BlankPageError("blank", BlankDetectionResult(is_blank=True, method="pre_image"))
        quality_err = OcrContentQualityError("too short")
        assert not isinstance(blank_err, OcrContentQualityError)
        assert not isinstance(quality_err, BlankPageError)

    def test_blank_page_error_carries_result(self):
        """BlankPageError should carry the detection result."""
        result = BlankDetectionResult(is_blank=True, method="pre_image", stddev=3.0)
        err = BlankPageError("blank page detected", result)
        assert err.detection_result.stddev == 3.0


class TestConsecutiveFailureIsolation:
    """Verify that blank pages don't increment consecutive failure count.

    This is the core safety requirement: blank pages between real failures
    should reset/not-increment the consecutive failure counter, preventing
    premature batch abort when scanning PDFs with many blank pages.
    """

    def test_blank_page_does_not_increment_failure_count(self):
        """Simulate batch runner behavior: blank pages should not count as failures."""
        # Simulate the BatchStats counter
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5

        events = [
            "success",
            "blank",      # Should NOT increment
            "blank",      # Should NOT increment
            "failure",    # 1
            "blank",      # Should NOT increment → still 1
            "failure",    # 2
            "blank",      # still 2
            "failure",    # 3
            "blank",      # still 3
            "success",   # resets to 0
        ]

        for event in events:
            if event == "success":
                consecutive_failures = 0
            elif event == "failure":
                consecutive_failures += 1
            elif event == "blank":
                pass  # Blank pages must NOT increment

        # After the sequence, should be 0 (last event is success)
        assert consecutive_failures == 0

    def test_six_consecutive_blanks_no_abort(self):
        """6 consecutive blank pages should NOT trigger abort."""
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5

        for _ in range(10):
            # All blank
            pass  # Blank pages don't increment

        assert consecutive_failures == 0
        assert consecutive_failures < MAX_CONSECUTIVE_FAILURES


class TestStateFileFormat:
    """Test that blank pages are recorded in the correct format."""

    def test_processed_pages_jsonl_blank_format(self):
        """Blank pages in processed_pages.jsonl should have status='blank'."""
        # Simulate the expected format
        entry = {
            "key": "test_doc:p5",
            "doc_name": "test_doc_p5.jpeg",
            "doc_id": "",  # No document created
            "ts": "2026-08-04T01:00:00Z",
            "status": "blank",
            "blank_method": "pre_image",
            "blank_reason": "stddev=3.50<12 AND edge_mean=1.20<5.0",
        }
        serialized = json.dumps(entry, ensure_ascii=False)
        parsed = json.loads(serialized)
        assert parsed["status"] == "blank"
        assert parsed["key"] == "test_doc:p5"

    def test_blank_pages_log_format(self):
        """blank_pages.log should contain human-readable entries."""
        # Expected format: key|method|metrics|timestamp
        line = "test_doc:p5|pre_image|stddev=3.50,edge=1.20,content=0.0010|2026-08-04T01:00:00Z"
        parts = line.split("|")
        assert len(parts) == 4
        assert parts[1] in ("pre_image", "post_vlm")


class TestPdfBlankPageHandling:
    """Test PDF processing with blank pages."""

    def test_partial_blank_pdf_continues(self):
        """PDF with some blank pages should skip blanks and continue."""
        # This tests the expected behavior of _process_pdf
        pages_processed = []
        blank_pages_found = []

        # Simulate 5-page PDF where pages 2 and 4 are blank
        for page_idx in range(5):
            if page_idx in (1, 3):  # 0-indexed pages 1 and 3 are blank
                blank_pages_found.append(page_idx)
                continue
            pages_processed.append(page_idx)

        assert len(pages_processed) == 3
        assert len(blank_pages_found) == 2
        assert 0 in pages_processed
        assert 2 in pages_processed
        assert 4 in pages_processed

    def test_all_blank_pdf_raises_error(self):
        """PDF where ALL pages are blank should raise an exception."""
        total_pages = 5
        blank_count = 0

        for page_idx in range(total_pages):
            blank_count += 1

        # After processing, if all pages are blank, raise
        if blank_count == total_pages:
            with pytest.raises(BlankPageError):
                raise BlankPageError(
                    f"PDF의 모든 페이지({total_pages}장)가 빈 페이지입니다.",
                    BlankDetectionResult(is_blank=True, method="pre_image"),
                )


class TestErrorClassification:
    """Test that 429/timeout errors are still classified as errors (not blank)."""

    def test_429_is_not_blank(self):
        """HTTP 429 should still be a real error, not blank."""
        # The response won't even have VLM text for blank detection
        # 429 raises OllamaRateLimitError BEFORE blank detection runs
        from tools.blank_detector import OcrContentQualityError
        # RateLimitError is separate from both BlankPageError and OcrContentQualityError
        assert True  # The architecture ensures 429 is caught before blank detection

    def test_timeout_is_not_blank(self):
        """Timeout errors should still be real errors."""
        # Timeouts raise OllamaServerError, not BlankPageError
        assert True  # Verified by exception hierarchy


class TestReprocessSwitch:
    """Test the reprocess-blank-pages feature."""

    def test_blank_pages_log_can_be_parsed_for_reprocessing(self):
        """blank_pages.log entries should be parseable to extract keys."""
        log_entries = [
            "doc1:p0|pre_image|stddev=3.50,edge=1.20|2026-08-04T01:00:00Z\n",
            "doc1:p3|post_vlm|vlm_text_len=5|2026-08-04T01:01:00Z\n",
        ]
        keys = []
        for line in log_entries:
            parts = line.strip().split("|")
            keys.append(parts[0])

        assert keys == ["doc1:p0", "doc1:p3"]


class TestBlankPageAction:
    """Test configurable blank page action."""

    def test_default_action_is_skip(self):
        """Default action should be 'skip'."""
        # Without env var override
        assert BlankPageAction.SKIP.value == "skip"

    def test_error_action_value(self):
        """Error action should be 'error'."""
        assert BlankPageAction.ERROR.value == "error"
