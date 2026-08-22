"""Blank page detection module.

Provides both pre-detection (image-based, before VLM call) and
post-detection (after VLM response) to identify blank/empty pages.

Thresholds calibrated on 2026-08-04 using 500 real pages from the
희망브리지 문서전자화(2025.04) dataset:
- 200 known blank pages (block_text < 10 chars in index.db3)
- 300 known content pages (block_text >= 50 chars)
- Result: FP=0%, TP=31.5% for pre-detection
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Optional

from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)


class BlankPageAction(Enum):
    """What to do when a blank page is detected."""
    SKIP = "skip"       # Skip silently, don't create document (default)
    ERROR = "error"     # Create error document in knowledge base


# Environment variable to control blank page behavior
# Values: "skip" (default) or "error"
BLANK_PAGE_ACTION = BlankPageAction(
    os.environ.get("BLANK_PAGE_ACTION", "skip").lower()
)

# ============================================================
# Calibrated thresholds (2026-08-04, n=500)
# ============================================================
# Pre-detection: both conditions must be true to classify as blank.
# Conservative to avoid false positives (content classified as blank).
#
# Data:
#   Content pages (n=300): min stddev=14.47, min edge_mean=2.09
#   Blank pages (n=200):   median stddev=13.79, median edge_mean=5.31
#
# Chosen: stddev < 12 AND edge_mean < 5.0
#   FP = 0/300 (0%)
#   TP = 63/200 (31.5%)
STDDEV_THRESHOLD: float = float(os.environ.get("BLANK_STDDEV_THRESHOLD", "12.0"))
EDGE_MEAN_THRESHOLD: float = float(os.environ.get("BLANK_EDGE_THRESHOLD", "5.0"))


# ============================================================
# Post-detection keywords
# ============================================================
# If VLM returns text shorter than MIN_CONTENT_LENGTH (10 chars, from
# OcrContentQualityError) AND contains these patterns, classify as blank
# rather than error.
BLANK_KEYWORDS = [
    "빈 페이지",
    "빈 용지",
    "blank page",
    "blank",
    "빈페이지",
    "내용 없음",
    "내용없음",
    "텍스트 없음",
    "텍스트가 없",
    "문서가 비어",
    "비어 있",
    "아무 내용",
    "아무런 내용",
    "no text",
    "no content",
    "empty page",
    "empty document",
]


@dataclass
class BlankDetectionResult:
    """Result of blank page detection."""
    is_blank: bool
    method: str  # "pre_image", "post_vlm", or "none"
    stddev: Optional[float] = None
    edge_mean: Optional[float] = None
    content_ratio: Optional[float] = None
    vlm_text: Optional[str] = None
    reason: str = ""

    def log_line(self) -> str:
        """Format a log line for blank_pages.log."""
        parts = [f"method={self.method}"]
        if self.stddev is not None:
            parts.append(f"stddev={self.stddev:.2f}")
        if self.edge_mean is not None:
            parts.append(f"edge={self.edge_mean:.2f}")
        if self.content_ratio is not None:
            parts.append(f"content_ratio={self.content_ratio:.4f}")
        if self.vlm_text is not None:
            parts.append(f"vlm_text_len={len(self.vlm_text)}")
        if self.reason:
            parts.append(f"reason={self.reason}")
        return "|".join(parts)


# ============================================================
# Pre-detection (image-based, before VLM call)
# ============================================================

def compute_image_metrics(img: Image.Image) -> tuple[float, float, float]:
    """Compute blank-detection metrics from a PIL Image.

    Returns:
        (stddev, edge_mean, content_ratio)
    """
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    stddev = stat.stddev[0]

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    edge_mean = edge_stat.mean[0]

    total_pixels = gray.size[0] * gray.size[1]
    # Content ratio: pixels darker than 200 (content on whitish background)
    dark_count = sum(1 for p in gray.getdata() if p < 200)
    content_ratio = dark_count / total_pixels

    return stddev, edge_mean, content_ratio


def detect_blank_pre(image_source) -> BlankDetectionResult:
    """Pre-detect blank page from image data (before VLM call).

    Args:
        image_source: PIL Image, bytes, or file path (str/Path)

    Returns:
        BlankDetectionResult with is_blank=True if page appears blank.
    """
    try:
        if isinstance(image_source, Image.Image):
            img = image_source
        elif isinstance(image_source, bytes):
            img = Image.open(BytesIO(image_source))
        else:
            img = Image.open(str(image_source))

        stddev, edge_mean, content_ratio = compute_image_metrics(img)

        is_blank = (stddev < STDDEV_THRESHOLD and edge_mean < EDGE_MEAN_THRESHOLD)

        reason = ""
        if is_blank:
            reason = (
                f"stddev={stddev:.2f}<{STDDEV_THRESHOLD} AND "
                f"edge_mean={edge_mean:.2f}<{EDGE_MEAN_THRESHOLD}"
            )

        return BlankDetectionResult(
            is_blank=is_blank,
            method="pre_image" if is_blank else "none",
            stddev=stddev,
            edge_mean=edge_mean,
            content_ratio=content_ratio,
            reason=reason,
        )
    except Exception as e:
        logger.warning(f"Pre-detection failed (proceeding with VLM): {e}")
        return BlankDetectionResult(
            is_blank=False,
            method="none",
            reason=f"pre_detection_error: {e}",
        )


# ============================================================
# Post-detection (after VLM response)
# ============================================================

def detect_blank_post(vlm_text: str) -> BlankDetectionResult:
    """Post-detect blank page from VLM OCR result.

    Called when VLM returns a short response (< MIN_CONTENT_LENGTH).
    Distinguishes between:
    - Blank page (VLM says "blank page" or similar) → skip, no retry
    - Genuine error (VLM failed to process) → raise exception, retry

    Args:
        vlm_text: The text returned by VLM (already stripped of thinking tags)

    Returns:
        BlankDetectionResult with is_blank=True if VLM indicates blank page.
    """
    if not vlm_text:
        # Empty response - likely blank page
        return BlankDetectionResult(
            is_blank=True,
            method="post_vlm",
            vlm_text=vlm_text or "",
            reason="empty_vlm_response",
        )

    stripped = vlm_text.strip().lower()

    # Check for blank page keywords
    for keyword in BLANK_KEYWORDS:
        if keyword.lower() in stripped:
            return BlankDetectionResult(
                is_blank=True,
                method="post_vlm",
                vlm_text=vlm_text,
                reason=f"keyword_match:{keyword}",
            )

    # Short text without blank keywords → not blank (genuine error)
    return BlankDetectionResult(
        is_blank=False,
        method="none",
        vlm_text=vlm_text,
        reason="short_text_no_blank_keyword",
    )


# ============================================================
# Exceptions
# ============================================================

class BlankPageError(Exception):
    """Raised when a page is detected as blank.

    This is NOT a failure - it's a normal condition that should be handled
    by skipping the page (not incrementing consecutive failure count).
    """

    def __init__(self, message: str, detection_result: BlankDetectionResult):
        super().__init__(message)
        self.detection_result = detection_result


class OcrContentQualityError(Exception):
    """Raised when Ollama returns a 200 but the content is empty or too short.

    This is a GENUINE ERROR (model failure, not blank page).
    Should increment consecutive failure count and trigger retry.

    Minimum content length: 10 characters (post-strip).
    """
    MIN_CONTENT_LENGTH = 10
