#!/usr/bin/env python3
"""Calibrate blank page detection thresholds using real data.

Renders random pages from PDFs, computes image statistics,
and cross-references with index.db3's block_text to label them.

Usage:
    python3 calibrate_blank_threshold.py [--samples N] [--seed S]
"""

import argparse
import io
import json
import os
import random
import sqlite3
import statistics
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageFilter, ImageStat

# Paths
PDF_DIR = Path(os.environ.get(
    "PDF_DIR",
    str(Path.home() / "Downloads/희망브리지/희망브리지 문서전자화(2025.04)")
))
DB3_PATH = Path(os.environ.get(
    "DB3_PATH",
    str(Path.home() / "Downloads/희망브리지/희망브리지 문서전자화(2025.04) index.db3")
))

DPI = 200
MAX_LONG_SIDE = 2048


def render_page(pdf_path: Path, page_num: int) -> Image.Image:
    """Render a single PDF page to PIL Image."""
    doc = fitz.open(str(pdf_path))
    page = doc[page_num]
    mat = fitz.Matrix(DPI / 72, DPI / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()

    # Resize if needed
    w, h = img.size
    long_side = max(w, h)
    if long_side > MAX_LONG_SIDE:
        ratio = MAX_LONG_SIDE / long_side
        img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
    return img


def compute_metrics(img: Image.Image) -> dict:
    """Compute blank-page detection metrics from a PIL Image."""
    # Convert to grayscale
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)

    # 1. Standard deviation of pixel values
    stddev = stat.stddev[0]

    # 2. Mean pixel value
    mean_val = stat.mean[0]

    # 3. Edge density (Sobel-like via PIL FIND_EDGES)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    edge_mean = edge_stat.mean[0]

    # 4. Dark pixel ratio (pixels < 128 in grayscale)
    total_pixels = gray.size[0] * gray.size[1]
    dark_pixels = sum(1 for p in gray.getdata() if p < 128)
    dark_ratio = dark_pixels / total_pixels

    # 5. Content region: binarize and count non-white area
    # Using Otsu-like threshold at 200 (generous for scanned pages)
    binary_dark = sum(1 for p in gray.getdata() if p < 200)
    content_ratio = binary_dark / total_pixels

    return {
        "stddev": round(stddev, 2),
        "mean": round(mean_val, 2),
        "edge_mean": round(edge_mean, 2),
        "dark_ratio": round(dark_ratio, 4),
        "content_ratio": round(content_ratio, 4),
    }


def load_db3_labels() -> dict:
    """Load page-level text lengths from index.db3.

    Returns dict: (subfolder_name, pdf_stem, page_num) -> text_length
    """
    if not DB3_PATH.is_file():
        print(f"WARNING: DB3 not found at {DB3_PATH}", file=sys.stderr)
        return {}

    labels = {}
    try:
        conn = sqlite3.connect(str(DB3_PATH))
        conn.text_factory = lambda b: b.decode("cp949", errors="replace")
        cursor = conn.cursor()

        # Discover tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [t[0] for t in cursor.fetchall()]

        if "page_info" in tables:
            cursor.execute("SELECT * FROM page_info LIMIT 1")
            columns = [d[0] for d in cursor.description]
            print(f"page_info columns: {columns}", file=sys.stderr)

            cursor.execute("SELECT * FROM page_info")
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # Try to extract relevant fields
                block_text = str(row_dict.get("block_text", "") or "")
                text_len = len(block_text.strip())

                # Build key from available columns
                file_name = str(row_dict.get("file_name", "") or row_dict.get("filename", "") or "")
                page_no = row_dict.get("page_no", row_dict.get("page_num", 0))

                if file_name:
                    labels[(file_name, int(page_no) if page_no else 0)] = text_len
        else:
            print(f"Available tables: {tables}", file=sys.stderr)

        conn.close()
    except Exception as e:
        print(f"DB3 read error: {e}", file=sys.stderr)

    return labels


def collect_pdf_pages() -> list[tuple[Path, int, str]]:
    """Collect all PDF pages for sampling."""
    pages = []
    for subdir in sorted(PDF_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        for pdf_path in sorted(subdir.glob("*.pdf")):
            try:
                doc = fitz.open(str(pdf_path))
                n_pages = doc.page_count
                doc.close()
                for p in range(n_pages):
                    pages.append((pdf_path, p, f"{subdir.name}/{pdf_path.stem}"))
            except Exception:
                continue
    return pages


def main():
    parser = argparse.ArgumentParser(description="Calibrate blank page threshold")
    parser.add_argument("--samples", type=int, default=500,
                        help="Number of random pages to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", default="calibration_results.json",
                        help="Output JSON file")
    args = parser.parse_args()

    random.seed(args.seed)

    print("Collecting PDF pages...", file=sys.stderr)
    all_pages = collect_pdf_pages()
    print(f"Total pages available: {len(all_pages)}", file=sys.stderr)

    # Sample
    n_samples = min(args.samples, len(all_pages))
    sampled = random.sample(all_pages, n_samples)

    print(f"Loading DB3 labels...", file=sys.stderr)
    labels = load_db3_labels()
    print(f"Labels loaded: {len(labels)} entries", file=sys.stderr)

    results = []
    for i, (pdf_path, page_num, name) in enumerate(sampled):
        if (i + 1) % 50 == 0:
            print(f"Processing {i+1}/{n_samples}...", file=sys.stderr)
        try:
            img = render_page(pdf_path, page_num)
            metrics = compute_metrics(img)

            # Try to get label from db3
            # Label: text length from block_text
            label_key_1 = (pdf_path.name, page_num)
            label_key_2 = (pdf_path.stem, page_num)
            text_len = labels.get(label_key_1) or labels.get(label_key_2) or -1

            entry = {
                "name": name,
                "page": page_num,
                "pdf": str(pdf_path.name),
                "text_len": text_len,
                **metrics,
            }
            results.append(entry)
        except Exception as e:
            print(f"Error processing {name} p{page_num}: {e}", file=sys.stderr)

    # Analyze
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"CALIBRATION RESULTS ({len(results)} samples)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    # Separate by label
    labeled = [r for r in results if r["text_len"] >= 0]
    blank_by_label = [r for r in labeled if r["text_len"] < 10]
    content_by_label = [r for r in labeled if r["text_len"] >= 10]

    print(f"\nLabeled samples: {len(labeled)}", file=sys.stderr)
    print(f"  Blank (text<10): {len(blank_by_label)}", file=sys.stderr)
    print(f"  Content (text>=10): {len(content_by_label)}", file=sys.stderr)

    # Regardless of labels, analyze the distribution
    all_stddevs = [r["stddev"] for r in results]
    all_edges = [r["edge_mean"] for r in results]
    all_content = [r["content_ratio"] for r in results]

    print(f"\nAll samples statistics:", file=sys.stderr)
    print(f"  stddev: min={min(all_stddevs):.2f}, max={max(all_stddevs):.2f}, "
          f"median={statistics.median(all_stddevs):.2f}, p5={sorted(all_stddevs)[len(all_stddevs)//20]:.2f}", file=sys.stderr)
    print(f"  edge_mean: min={min(all_edges):.2f}, max={max(all_edges):.2f}, "
          f"median={statistics.median(all_edges):.2f}, p5={sorted(all_edges)[len(all_edges)//20]:.2f}", file=sys.stderr)
    print(f"  content_ratio: min={min(all_content):.4f}, max={max(all_content):.4f}, "
          f"median={statistics.median(all_content):.4f}, p5={sorted(all_content)[len(all_content)//20]:.4f}", file=sys.stderr)

    if blank_by_label:
        blank_stddevs = [r["stddev"] for r in blank_by_label]
        blank_edges = [r["edge_mean"] for r in blank_by_label]
        blank_content = [r["content_ratio"] for r in blank_by_label]
        print(f"\nBlank pages (label text<10):", file=sys.stderr)
        print(f"  stddev: min={min(blank_stddevs):.2f}, max={max(blank_stddevs):.2f}, "
              f"median={statistics.median(blank_stddevs):.2f}", file=sys.stderr)
        print(f"  edge_mean: min={min(blank_edges):.2f}, max={max(blank_edges):.2f}, "
              f"median={statistics.median(blank_edges):.2f}", file=sys.stderr)
        print(f"  content_ratio: min={min(blank_content):.4f}, max={max(blank_content):.4f}, "
              f"median={statistics.median(blank_content):.4f}", file=sys.stderr)

    if content_by_label:
        content_stddevs = [r["stddev"] for r in content_by_label]
        content_edges = [r["edge_mean"] for r in content_by_label]
        content_contents = [r["content_ratio"] for r in content_by_label]
        print(f"\nContent pages (label text>=10):", file=sys.stderr)
        print(f"  stddev: min={min(content_stddevs):.2f}, max={max(content_stddevs):.2f}, "
              f"median={statistics.median(content_stddevs):.2f}", file=sys.stderr)
        print(f"  edge_mean: min={min(content_edges):.2f}, max={max(content_edges):.2f}, "
              f"median={statistics.median(content_edges):.2f}", file=sys.stderr)
        print(f"  content_ratio: min={min(content_contents):.4f}, max={max(content_contents):.4f}, "
              f"median={statistics.median(content_contents):.4f}", file=sys.stderr)

    # Find conservative threshold
    # Goal: almost 0 false positives (content pages classified as blank)
    # Strategy: threshold must be BELOW the minimum of content pages
    if content_by_label:
        # Conservative: blank threshold = min(content) * 0.5 or slightly below min(content)
        min_content_stddev = min(content_stddevs)
        min_content_edge = min(content_edges)
        min_content_ratio = min(content_contents)

        # Proposed threshold: must satisfy ALL conditions to be blank
        proposed = {
            "stddev_threshold": round(min_content_stddev * 0.7, 2),
            "edge_mean_threshold": round(min_content_edge * 0.7, 2),
            "content_ratio_threshold": round(min_content_ratio * 0.5, 4),
        }
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"PROPOSED THRESHOLDS (conservative):", file=sys.stderr)
        print(f"  A page is BLANK if ALL of:", file=sys.stderr)
        print(f"    stddev < {proposed['stddev_threshold']}", file=sys.stderr)
        print(f"    edge_mean < {proposed['edge_mean_threshold']}", file=sys.stderr)
        print(f"    content_ratio < {proposed['content_ratio_threshold']}", file=sys.stderr)
        print(f"  Derived from min(content_pages) * safety_factor", file=sys.stderr)

        # Evaluate
        fp = 0  # false positive: content classified as blank
        tp = 0  # true positive: blank classified as blank
        fn = 0  # false negative: blank classified as content

        for r in blank_by_label:
            if (r["stddev"] < proposed["stddev_threshold"] and
                r["edge_mean"] < proposed["edge_mean_threshold"] and
                r["content_ratio"] < proposed["content_ratio_threshold"]):
                tp += 1
            else:
                fn += 1

        for r in content_by_label:
            if (r["stddev"] < proposed["stddev_threshold"] and
                r["edge_mean"] < proposed["edge_mean_threshold"] and
                r["content_ratio"] < proposed["content_ratio_threshold"]):
                fp += 1

        print(f"\n  Evaluation:", file=sys.stderr)
        print(f"    True Positives (blank correctly detected): {tp}/{len(blank_by_label)}", file=sys.stderr)
        print(f"    False Negatives (blank missed): {fn}/{len(blank_by_label)}", file=sys.stderr)
        print(f"    False Positives (content wrongly blank): {fp}/{len(content_by_label)}", file=sys.stderr)
        print(f"    FP rate: {fp/len(content_by_label)*100:.2f}% (MUST be ~0%)", file=sys.stderr)

    # Save results
    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump({
            "samples": results,
            "proposed_thresholds": proposed if content_by_label else {},
            "statistics": {
                "total_sampled": len(results),
                "labeled": len(labeled),
                "blank_by_label": len(blank_by_label),
                "content_by_label": len(content_by_label),
            },
        }, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
