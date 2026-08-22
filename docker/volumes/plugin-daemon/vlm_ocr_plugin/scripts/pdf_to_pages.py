#!/usr/bin/env python3
"""Extract a single page from a PDF as a compressed JPEG.

Usage:
    pdf_to_pages.py <pdf_path> <page_number> <output_path> [--max-long-side N] [--quality Q]

The script renders one page at a time to minimize disk usage.
Target: average ≤200KB per page image.
"""

import argparse
import io
import sys
from pathlib import Path
from typing import Final

import fitz  # PyMuPDF
from PIL import Image

# Compression targets per REVIEW.md: ≤200KB average
DEFAULT_MAX_LONG_SIDE: Final[int] = 2048
DEFAULT_QUALITY: Final[int] = 70
MIN_QUALITY: Final[int] = 40
TARGET_SIZE_BYTES: Final[int] = 250 * 1024  # 250KB hard cap per image
DPI: Final[int] = 200  # Good balance of quality vs size for scanned docs


def render_page_to_jpeg(
    pdf_path: Path,
    page_number: int,
    output_path: Path,
    max_long_side: int = DEFAULT_MAX_LONG_SIDE,
    quality: int = DEFAULT_QUALITY,
) -> int:
    """Render a single PDF page to compressed JPEG.

    Args:
        pdf_path: Path to the source PDF file.
        page_number: 0-based page index.
        output_path: Where to write the JPEG.
        max_long_side: Maximum pixels on the longer side.
        quality: Initial JPEG quality (will be reduced if file too large).

    Returns:
        Output file size in bytes.

    Raises:
        ValueError: If page_number is out of range.
        FileNotFoundError: If pdf_path doesn't exist.
    """
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    try:
        if page_number < 0 or page_number >= doc.page_count:
            raise ValueError(
                f"페이지 번호 {page_number}이(가) 범위를 벗어났습니다. "
                f"총 {doc.page_count}페이지."
            )

        page = doc[page_number]

        # Render at configured DPI
        mat = fitz.Matrix(DPI / 72, DPI / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # Convert to PIL Image for compression control
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()

    # Resize if longer side exceeds limit
    width, height = img.size
    long_side = max(width, height)
    if long_side > max_long_side:
        ratio = max_long_side / long_side
        new_size = (int(width * ratio), int(height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    # Save with adaptive quality to meet size target
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current_quality = quality
    while current_quality >= MIN_QUALITY:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=current_quality, optimize=True)
        size = buffer.tell()

        if size <= TARGET_SIZE_BYTES:
            output_path.write_bytes(buffer.getvalue())
            return size

        current_quality -= 10

    # Even at min quality, write it out
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=MIN_QUALITY, optimize=True)
    output_path.write_bytes(buffer.getvalue())
    return buffer.tell()


def get_page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF."""
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")
    doc = fitz.open(str(pdf_path))
    count = doc.page_count
    doc.close()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract a single page from a PDF as compressed JPEG."
    )
    parser.add_argument("pdf_path", help="Path to the source PDF.")
    parser.add_argument("page_number", type=int, help="0-based page index.")
    parser.add_argument("output_path", help="Path to write the output JPEG.")
    parser.add_argument(
        "--max-long-side",
        type=int,
        default=DEFAULT_MAX_LONG_SIDE,
        help=f"Max pixels on longer side (default: {DEFAULT_MAX_LONG_SIDE}).",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        help=f"Initial JPEG quality (default: {DEFAULT_QUALITY}).",
    )
    parser.add_argument(
        "--page-count",
        action="store_true",
        help="Only print page count and exit.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)

    if args.page_count:
        try:
            count = get_page_count(pdf_path)
            print(count)
            return 0
        except (FileNotFoundError, Exception) as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1

    output_path = Path(args.output_path)

    try:
        size = render_page_to_jpeg(
            pdf_path=pdf_path,
            page_number=args.page_number,
            output_path=output_path,
            max_long_side=args.max_long_side,
            quality=args.quality,
        )
        print(f"{size}")
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"예상치 못한 오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
