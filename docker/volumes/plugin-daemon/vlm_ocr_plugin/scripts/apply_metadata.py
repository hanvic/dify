#!/usr/bin/env python3
"""
apply_metadata.py — 희망브리지 RAG 챗봇 문서에 index.db3 메타데이터를 일괄 적용

사용법:
    source .env
    python3 apply_metadata.py [--dry-run] [--batch-size 50]

동작:
1. Dify 데이터셋의 모든 문서 목록을 가져옴
2. 문서 이름 패턴에서 서류철명/문서명/생산연도/작성자/페이지번호를 파싱
3. 메타데이터가 없는 문서에 대해 batch로 메타데이터 업데이트 API 호출

문서명 패턴: {서류철명}_{연도범위}_{문서명}_{생산연도}[_{작성자}]_p{페이지}.jpeg
예: 갑근세 기타서류(적립금)_1994~1999_1994 원천징수 영수철_1994_전국재해대책협의회_p29.jpeg
"""
import argparse
import json
import logging
import os
import re
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── Configuration ──────────────────────────────────────────────────────────
DIFY_API_BASE = os.environ.get("DIFY_API_BASE", "http://localhost/v1")
DATASET_API_KEY = os.environ.get("DATASET_API_KEY", "")
DATASET_ID = os.environ.get("DATASET_ID", "20087ab8-8e76-4f75-bfc8-88a24f4fd73c")

# Metadata field IDs (from dataset metadata schema)
METADATA_FIELDS = {
    "서류철명": "960c277c-67b0-49cf-9a48-844f40fb0b3f",
    "문서명": "202fefb2-a0b7-4505-9086-86d62b7ea42f",
    "생산연도": "73516d1f-80c6-40f2-b5b7-e7100d4e917a",
    "작성자": "82e365be-a2a0-4a1c-a131-17881b4e9302",
    "페이지번호": "0435a2f1-ec34-4a69-8e1c-15360a6a8ed4",
}

SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Bearer {DATASET_API_KEY}"})


# ─── Document name parsing ──────────────────────────────────────────────────
def parse_document_name(name: str) -> dict | None:
    """
    Parse metadata from Dify document name.
    
    Patterns:
    1. {서류철명}_{연도범위}_{문서명}_{생산연도}[_{작성자}]_p{페이지}.jpeg
       예: 갑근세 기타서류(적립금)_1994~1999_1994 원천징수 영수철_1994_전국재해대책협의회_p29.jpeg
    2. {서류철명}_{연도범위 서류철세부}_{문서명}_{생산연도}_p{페이지}.jpeg
       예: 회계장부_2002~2004 기타장부_금전출납부 1 복지자금_p0.jpeg
    3. {문서명}_{번호}.jpeg (no year range)
       예: 준공식 행사_1.jpeg
    """
    # Remove extension
    base = re.sub(r'\.(jpeg|jpg|png|pdf)$', '', name, flags=re.IGNORECASE)
    
    # Extract page number from the end: _p{N}
    page_match = re.search(r'_p(\d+)$', base)
    if page_match:
        page_no = int(page_match.group(1))
        base = base[:page_match.start()]
    else:
        # Pattern 3: simple number suffix like 준공식 행사_1
        num_match = re.search(r'_(\d+)( .+)?$', base)
        if num_match:
            page_no = int(num_match.group(1))
            base = base[:num_match.start()]
            # For this pattern, the entire base is the document name
            return {
                "서류철명": "",
                "문서명": base,
                "생산연도": "",
                "작성자": "",
                "페이지번호": page_no,
            }
        return None
    
    # Now base = {서류철명}_{연도범위[ 서류철세부]}_{문서명}_{생산연도}[_{작성자}]
    # Try to find year range pattern: YYYY~YYYY (possibly followed by space+text)
    range_match = re.search(r'^(.+?)_(\d{4}~\d{4})([ ][^_]+)?_(.+)$', base)
    
    if not range_match:
        # Fallback: treat whole base as document name
        return {
            "서류철명": "",
            "문서명": base,
            "생산연도": "",
            "작성자": "",
            "페이지번호": page_no,
        }
    
    서류철명 = range_match.group(1)
    서류철세부 = (range_match.group(3) or "").strip()
    if 서류철세부:
        서류철명 = f"{서류철명} {서류철세부}"
    remainder = range_match.group(4)
    
    # remainder = {문서명}_{생산연도}[_{작성자}]
    # Strategy: scan for 4-digit year from right
    segments = remainder.split('_')
    year_idx = None
    for i in range(len(segments) - 1, -1, -1):
        if re.match(r'^\d{4}$', segments[i]):
            year_idx = i
            break
    
    if year_idx is None:
        # No year found
        문서명 = remainder
        생산연도 = ""
        작성자 = ""
    elif year_idx < len(segments) - 1:
        # Content after year = author
        작성자 = '_'.join(segments[year_idx + 1:])
        생산연도 = segments[year_idx]
        문서명 = '_'.join(segments[:year_idx])
    else:
        생산연도 = segments[year_idx]
        문서명 = '_'.join(segments[:year_idx])
        작성자 = ""
    
    return {
        "서류철명": 서류철명,
        "문서명": 문서명,
        "생산연도": 생산연도,
        "작성자": 작성자,
        "페이지번호": page_no,
    }


# ─── Dify API interactions ──────────────────────────────────────────────────
def get_all_documents() -> list[dict]:
    """Fetch all documents from the dataset."""
    documents = []
    page = 1
    limit = 100
    
    while True:
        resp = SESSION.get(
            f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents",
            params={"page": page, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("data", [])
        documents.extend(docs)
        
        if not data.get("has_more", False):
            break
        page += 1
        time.sleep(0.2)  # Rate limiting
    
    return documents


def get_documents_with_metadata(doc_ids: list[str]) -> dict[str, list]:
    """Check which documents already have metadata. Returns {doc_id: metadata_list}."""
    result = {}
    # Fetch individual documents to check metadata
    for doc_id in doc_ids:
        try:
            resp = SESSION.get(
                f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents/{doc_id}",
                timeout=30,
            )
            if resp.status_code == 200:
                doc = resp.json()
                meta = doc.get("doc_metadata", [])
                if isinstance(meta, list):
                    result[doc_id] = meta
                else:
                    result[doc_id] = []
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"Failed to get metadata for {doc_id}: {e}")
            result[doc_id] = []
    return result


def update_metadata_batch(operations: list[dict]) -> bool:
    """Update metadata for multiple documents in one API call."""
    payload = {"operation_data": operations}
    
    resp = SESSION.post(
        f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents/metadata",
        json=payload,
        timeout=60,
    )
    
    if resp.status_code == 200:
        return True
    else:
        log.error(f"Metadata update failed: {resp.status_code} - {resp.text[:200]}")
        return False


def build_metadata_operation(doc_id: str, parsed: dict) -> dict:
    """Build a metadata operation for a single document."""
    metadata_list = []
    for field_name, field_id in METADATA_FIELDS.items():
        value = parsed.get(field_name, "")
        if field_name == "페이지번호":
            value = parsed.get("페이지번호", 0)
        metadata_list.append({
            "id": field_id,
            "name": field_name,
            "value": value,
        })
    
    return {
        "document_id": doc_id,
        "metadata_list": metadata_list,
        "partial_update": False,
    }


# ─── Main logic ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Apply metadata from document names to Dify dataset")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without applying")
    parser.add_argument("--batch-size", type=int, default=50, help="Documents per API call")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip documents that already have metadata")
    parser.add_argument("--force", action="store_true", help="Update all documents even if they have metadata")
    args = parser.parse_args()
    
    if not DATASET_API_KEY:
        log.error("DATASET_API_KEY environment variable required")
        sys.exit(1)
    
    log.info(f"Dataset: {DATASET_ID}")
    log.info(f"API Base: {DIFY_API_BASE}")
    log.info(f"Batch size: {args.batch_size}")
    log.info(f"Dry run: {args.dry_run}")
    
    # 1. Get all documents
    log.info("Fetching document list...")
    documents = get_all_documents()
    log.info(f"Total documents: {len(documents)}")
    
    # 2. Parse metadata from names and build operations
    operations = []
    parse_failures = []
    
    for doc in documents:
        doc_id = doc["id"]
        doc_name = doc["name"]
        
        parsed = parse_document_name(doc_name)
        if parsed is None:
            parse_failures.append(doc_name)
            continue
        
        operations.append((doc_id, doc_name, parsed))
    
    log.info(f"Successfully parsed: {len(operations)}")
    if parse_failures:
        log.warning(f"Parse failures: {len(parse_failures)}")
        for name in parse_failures[:5]:
            log.warning(f"  - {name}")
    
    # 3. Filter out documents that already have metadata (sample check)
    if not args.force and args.skip_existing:
        # Check first batch to see metadata status
        sample_ids = [op[0] for op in operations[:10]]
        meta_status = get_documents_with_metadata(sample_ids)
        has_meta_count = sum(1 for v in meta_status.values() if len(v) > 0)
        log.info(f"Sample metadata check: {has_meta_count}/{len(sample_ids)} already have metadata")
        
        if has_meta_count == len(sample_ids):
            log.info("All sampled documents already have metadata. Use --force to override.")
            # Still proceed but check individually per batch
    
    # 4. Apply metadata in batches
    if args.dry_run:
        log.info("=== DRY RUN - No changes will be made ===")
        for doc_id, doc_name, parsed in operations[:10]:
            log.info(f"  Would update: {doc_name[:60]}")
            log.info(f"    서류철명={parsed['서류철명']}, 문서명={parsed['문서명']}, "
                     f"생산연도={parsed['생산연도']}, 작성자={parsed['작성자']}, "
                     f"페이지번호={parsed['페이지번호']}")
        if len(operations) > 10:
            log.info(f"  ... and {len(operations) - 10} more")
        return
    
    # Process in batches
    total_updated = 0
    total_skipped = 0
    total_failed = 0
    
    for i in range(0, len(operations), args.batch_size):
        batch = operations[i:i + args.batch_size]
        
        # Check which need updating (if not --force)
        batch_ops = []
        if not args.force:
            batch_ids = [op[0] for op in batch]
            meta_status = get_documents_with_metadata(batch_ids)
            
            for doc_id, doc_name, parsed in batch:
                existing = meta_status.get(doc_id, [])
                if len(existing) > 0:
                    total_skipped += 1
                    continue
                batch_ops.append(build_metadata_operation(doc_id, parsed))
        else:
            batch_ops = [build_metadata_operation(doc_id, parsed) for doc_id, _, parsed in batch]
        
        if not batch_ops:
            log.info(f"Batch {i//args.batch_size + 1}: all {len(batch)} docs already have metadata, skipping")
            continue
        
        log.info(f"Batch {i//args.batch_size + 1}: updating {len(batch_ops)} documents...")
        
        success = update_metadata_batch(batch_ops)
        if success:
            total_updated += len(batch_ops)
            log.info(f"  ✓ Updated {len(batch_ops)} documents")
        else:
            total_failed += len(batch_ops)
            log.error(f"  ✗ Failed to update batch")
        
        time.sleep(0.5)  # Rate limiting between batches
    
    # Summary
    log.info("=" * 60)
    log.info(f"COMPLETE: updated={total_updated}, skipped={total_skipped}, failed={total_failed}")
    log.info(f"Parse failures: {len(parse_failures)}")


if __name__ == "__main__":
    main()
