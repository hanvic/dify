#!/usr/bin/env python3
"""
희망브리지 문서 메타데이터 자동 주입 스크립트 (idempotent)

index.db3(CP949)에서 서류철명/문서명/생산연도/작성자를 읽어
Dify Knowledge Base에 적재된 문서에 메타데이터를 주입합니다.

Usage:
    python3 scripts/inject_metadata.py                  # 미연결 문서만
    python3 scripts/inject_metadata.py --force          # 전수 재주입
    python3 scripts/inject_metadata.py --dry-run        # 실제 주입 없이 확인
    python3 scripts/inject_metadata.py --verify-only    # 현재 상태 검증만

재실행 안전(idempotent):
  - 이미 5개 필드가 모두 연결된 문서는 스킵 (--force 제외)
  - partial_update=true로 기존 값 보존
  - 배치 API로 호출 횟수 최소화
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).parent / "inject_metadata.log", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# ─── 환경 로드 ────────────────────────────────────────────────────────────
def load_env() -> dict[str, str]:
    """Load .env file from scripts directory."""
    env_path = Path(__file__).parent / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


ENV = load_env()
DIFY_BASE_URL = ENV.get("DIFY_BASE_URL", "http://localhost/v1")
DATASET_ID = ENV.get("DIFY_DATASET_ID", "20087ab8-8e76-4f75-bfc8-88a24f4fd73c")
API_TOKEN = ENV.get("DIFY_API_TOKEN", "")
INDEX_DB_PATH = Path(os.path.expanduser(ENV.get("INDEX_DB_PATH", "~/Downloads/희망브리지/희망브리지 문서전자화(2025.04) index.db3")))

# 필드 정의
METADATA_FIELDS = [
    {"name": "서류철명", "type": "string"},
    {"name": "문서명", "type": "string"},
    {"name": "생산연도", "type": "string"},
    {"name": "작성자", "type": "string"},
    {"name": "페이지번호", "type": "number"},
]

EXPECTED_FIELD_COUNT = len(METADATA_FIELDS)


# ─── API 헬퍼 ────────────────────────────────────────────────────────────
def api_request(
    method: str, path: str, body: dict | None = None, retries: int = 3, backoff: float = 1.0
) -> Any:
    """Make an API request with retry and rate-limit handling."""
    url = f"{DIFY_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None

    for attempt in range(retries):
        try:
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=60) as resp:
                resp_data = resp.read().decode()
                return json.loads(resp_data) if resp_data else None
        except HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            if e.code == 429:
                wait = backoff * (2**attempt)
                logger.warning(f"Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            elif e.code >= 500:
                wait = backoff * (2**attempt)
                logger.warning(f"Server error {e.code}, retry in {wait}s: {err_body[:200]}")
                time.sleep(wait)
                continue
            else:
                logger.error(f"API error {e.code} for {method} {path}: {err_body[:500]}")
                raise
        except (URLError, TimeoutError) as e:
            wait = backoff * (2**attempt)
            logger.warning(f"Network error, retry in {wait}s: {e}")
            time.sleep(wait)
            continue
    raise RuntimeError(f"API request failed after {retries} retries: {method} {path}")


# ─── 메타데이터 필드 관리 ────────────────────────────────────────────────
def get_metadata_fields() -> dict[str, str]:
    """Get existing metadata fields. Returns {name: id}."""
    resp = api_request("GET", f"/datasets/{DATASET_ID}/metadata")
    fields = {}
    for item in resp.get("doc_metadata", []):
        fields[item["name"]] = item["id"]
    return fields


def ensure_metadata_fields() -> dict[str, str]:
    """Ensure all required metadata fields exist. Returns {name: id}."""
    existing = get_metadata_fields()
    field_ids = {}
    for field_def in METADATA_FIELDS:
        name = field_def["name"]
        if name in existing:
            field_ids[name] = existing[name]
        else:
            resp = api_request(
                "POST",
                f"/datasets/{DATASET_ID}/metadata",
                {"name": name, "type": field_def["type"]},
            )
            field_ids[name] = resp["id"]
            logger.info(f"  필드 '{name}' 생성 완료 (id={resp['id']})")
    return field_ids


# ─── index.db3 로딩 ───────────────────────────────────────────────────────
def load_index_db() -> dict[str, dict]:
    """
    Load index.db3. Returns mapping: base_key -> metadata dict.
    
    base_key = "{folder}_{pdf_stem}" 형태.
    예: "갑근세 기타서류(적립금)_1994~1999_1992 운영 퇴직 적립금_1992"
    
    문서 명명 관례:
      file_path: 희망브리지...\\{서류철}_{서류철연도}\\{pdf_basename}
      doc_name:  {서류철}_{서류철연도}_{pdf_stem}_p{N}.jpeg
    """
    if not INDEX_DB_PATH.exists():
        logger.error(f"index.db3 not found: {INDEX_DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(INDEX_DB_PATH))
    conn.text_factory = lambda b: b.decode("cp949", "replace")
    cur = conn.cursor()
    cur.execute(
        "SELECT header_3, header_4, header_5, header_6, header_7, header_11 FROM index_info"
    )

    mapping: dict[str, dict] = {}
    for row in cur.fetchall():
        # text_factory 적용 시 str, 미적용 시 bytes 가능
        def decode_field(val) -> str:
            if val is None:
                return ""
            if isinstance(val, bytes):
                return val.decode("cp949", "replace").strip()
            return str(val).strip()

        서류철명 = decode_field(row[0])
        서류철연도 = decode_field(row[1])
        문서명 = decode_field(row[2])
        생산연도 = decode_field(row[3])
        작성자 = decode_field(row[4])
        file_path = decode_field(row[5])

        if not file_path:
            continue

        # file_path: 희망브리지 문서전자화(2025.04)\{folder}\{filename}.pdf
        parts = file_path.split("\\")
        if len(parts) < 3:
            continue

        folder = parts[1]  # e.g., "갑근세 기타서류(적립금)_1994~1999"
        pdf_filename = parts[2]  # e.g., "1992 운영 퇴직 적립금_1992.pdf"
        pdf_stem = pdf_filename[:-4] if pdf_filename.lower().endswith(".pdf") else pdf_filename

        # base_key: {folder}_{pdf_stem}
        base_key = f"{folder}_{pdf_stem}"
        # NFC 정규화 (macOS 호환)
        base_key = unicodedata.normalize("NFC", base_key)

        mapping[base_key] = {
            "서류철명": 서류철명,
            "문서명": 문서명,
            "생산연도": 생산연도,
            "작성자": 작성자,
        }

    conn.close()
    logger.info(f"index.db3에서 {len(mapping)}건의 메타데이터 로드 완료")
    return mapping


# ─── 문서명 파싱 ──────────────────────────────────────────────────────────
def parse_doc_name(doc_name: str) -> tuple[str, Optional[int]]:
    """
    Parse document name to extract base_key and page_number.
    Pattern: {base_key}_p{N}.jpeg
    Returns: (base_key, page_number)
    """
    name = unicodedata.normalize("NFC", doc_name)
    # 확장자 제거
    if name.lower().endswith((".jpeg", ".jpg", ".png")):
        name = name.rsplit(".", 1)[0]

    # _p{N} 패턴 추출
    match = re.search(r"_p(\d+)$", name)
    if match:
        page_num = int(match.group(1))
        base_key = name[: match.start()]
        return base_key, page_num

    return name, None


# ─── 문서 목록 가져오기 ──────────────────────────────────────────────────
def get_all_documents() -> list[dict]:
    """Fetch all documents from the dataset (paginated)."""
    documents = []
    page = 1
    limit = 100
    while True:
        resp = api_request(
            "GET", f"/datasets/{DATASET_ID}/documents?page={page}&limit={limit}"
        )
        data = resp.get("data", [])
        documents.extend(data)
        total = resp.get("total", 0)
        if len(documents) >= total or not data:
            break
        page += 1
        time.sleep(0.1)
    return documents


# ─── 메타데이터 주입 ─────────────────────────────────────────────────────
def inject_metadata(
    field_ids: dict[str, str],
    index_mapping: dict[str, dict],
    documents: list[dict],
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = 50,
) -> dict:
    """
    Inject metadata. Returns stats dict.
    """
    stats = {
        "total": len(documents),
        "skipped_has_meta": 0,
        "matched": 0,
        "unmatched": 0,
        "updated": 0,
        "errors": 0,
        "fallback": 0,
    }
    unmatched_list: list[dict] = []
    operations: list[dict] = []

    for doc in documents:
        doc_id = doc["id"]
        doc_name = doc["name"]

        # idempotency: 이미 메타데이터가 있으면 스킵 (force 아닌 경우)
        if not force:
            existing_meta = doc.get("doc_metadata", [])
            if existing_meta and len(existing_meta) >= EXPECTED_FIELD_COUNT:
                stats["skipped_has_meta"] += 1
                continue

        base_key, page_num = parse_doc_name(doc_name)
        meta = index_mapping.get(base_key)

        if not meta:
            # 폴백 시도: base_key에서 서류철명만이라도 추출
            fallback_meta = try_fallback_metadata(base_key, doc_name)
            if fallback_meta:
                meta = fallback_meta
                stats["fallback"] += 1
            else:
                stats["unmatched"] += 1
                unmatched_list.append({"doc_name": doc_name, "base_key": base_key, "doc_id": doc_id})
                continue

        stats["matched"] += 1

        # Build operation
        metadata_list = []
        for field_name, field_id in field_ids.items():
            if field_name == "페이지번호":
                value = page_num if page_num is not None else 0
            else:
                value = meta.get(field_name, "")
            metadata_list.append({"id": field_id, "name": field_name, "value": value})

        operations.append(
            {
                "document_id": doc_id,
                "metadata_list": metadata_list,
                "partial_update": True,
            }
        )

    # 미매칭 로그
    if unmatched_list:
        logger.warning(f"매핑 실패 {len(unmatched_list)}건:")
        for item in unmatched_list[:10]:
            logger.warning(f"  {item['doc_name']} → base_key={item['base_key']}")
        # 실패 목록 파일로 저장
        fail_path = Path(__file__).parent / "inject_metadata_unmatched.json"
        fail_path.write_text(json.dumps(unmatched_list, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"미매칭 목록 저장: {fail_path}")

    if dry_run:
        logger.info(f"[DRY-RUN] {len(operations)}건 주입 예정, {stats['skipped_has_meta']}건 스킵")
        return stats

    # 배치 실행
    for i in range(0, len(operations), batch_size):
        batch = operations[i : i + batch_size]
        try:
            api_request(
                "POST",
                f"/datasets/{DATASET_ID}/documents/metadata",
                {"operation_data": batch},
            )
            stats["updated"] += len(batch)
            logger.info(f"  배치 {i // batch_size + 1}/{(len(operations) - 1) // batch_size + 1}: {len(batch)}건 완료")
        except Exception as e:
            stats["errors"] += len(batch)
            logger.error(f"  배치 실패: {e}")
        time.sleep(0.3)  # Rate limiting

    return stats


def try_fallback_metadata(base_key: str, doc_name: str) -> Optional[dict]:
    """
    index.db3에 없는 문서에 대한 폴백 메타데이터 생성.
    
    문서명 관례: {서류철명}_{서류철연도}_{문서명}_{생산연도}[_{작성자}]_p{N}.jpeg
    base_key에서 서류철명과 관련 정보를 추출 시도.
    
    예: "준공식 행사_준공식 사진" → 서류철명="준공식 행사"
    """
    # base_key의 첫 번째 _ 앞이 서류철명일 수 있음
    # 하지만 서류철명 자체에 _가 포함될 수 있으므로 (예: "갑근세 기타서류(적립금)_1994~1999")
    # 연도 패턴으로 분리 시도
    
    # 패턴: {서류철}_{연도}_{나머지}
    year_match = re.search(r"^(.+?)_(\d{4}(?:~\d{4})?)\b", base_key)
    if year_match:
        folder_name = f"{year_match.group(1)}_{year_match.group(2)}"
        rest = base_key[len(folder_name) + 1:] if len(base_key) > len(folder_name) + 1 else ""
        return {
            "서류철명": year_match.group(1),
            "문서명": rest if rest else doc_name.rsplit("_p", 1)[0] if "_p" in doc_name else doc_name,
            "생산연도": "",
            "작성자": "",
        }

    # 그래도 안 되면 첫 _ 이전을 서류철명으로
    parts = base_key.split("_", 1)
    if len(parts) >= 2:
        return {
            "서류철명": parts[0],
            "문서명": parts[1] if parts[1] else doc_name,
            "생산연도": "",
            "작성자": "",
        }

    return None


# ─── 검증 ─────────────────────────────────────────────────────────────────
def verify_state() -> tuple[int, int]:
    """Verify current state. Returns (docs_with_meta, docs_without_meta)."""
    documents = get_all_documents()
    with_meta = sum(
        1 for d in documents if d.get("doc_metadata") and len(d["doc_metadata"]) >= EXPECTED_FIELD_COUNT
    )
    without_meta = len(documents) - with_meta
    return with_meta, without_meta


# ─── main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="희망브리지 문서 메타데이터 주입")
    parser.add_argument("--dry-run", action="store_true", help="실제 주입 없이 매핑만 확인")
    parser.add_argument("--force", action="store_true", help="이미 메타데이터 있는 문서도 재주입")
    parser.add_argument("--batch-size", type=int, default=50, help="배치 크기 (기본: 50)")
    parser.add_argument("--verify-only", action="store_true", help="주입 없이 현재 상태 확인만")
    args = parser.parse_args()

    if not API_TOKEN:
        logger.error("DIFY_API_TOKEN이 설정되지 않았습니다. scripts/.env를 확인하세요.")
        sys.exit(1)

    if args.verify_only:
        w, wo = verify_state()
        logger.info(f"현재 상태: 메타데이터 있음 {w}, 없음 {wo}, 합계 {w + wo}")
        return

    logger.info("=" * 60)
    logger.info("희망브리지 문서 메타데이터 주입 시작")
    logger.info(f"  모드: {'전수 재주입 (--force)' if args.force else '미연결만'}")
    logger.info(f"  DRY-RUN: {args.dry_run}")
    logger.info("=" * 60)

    # Step 1: 필드 확인
    logger.info("[1/4] 메타데이터 필드 확인...")
    field_ids = ensure_metadata_fields()
    logger.info(f"  필드: {list(field_ids.keys())}")

    # Step 2: index.db3 로드
    logger.info("[2/4] index.db3 로딩...")
    index_mapping = load_index_db()

    # Step 3: 문서 목록 조회
    logger.info("[3/4] Dify 문서 목록 조회...")
    documents = get_all_documents()
    logger.info(f"  총 {len(documents)}건")

    # Step 4: 주입
    logger.info("[4/4] 메타데이터 주입 중...")
    stats = inject_metadata(
        field_ids, index_mapping, documents,
        force=args.force, dry_run=args.dry_run, batch_size=args.batch_size,
    )

    # 결과 출력
    logger.info("")
    logger.info("=" * 60)
    logger.info("결과 요약:")
    logger.info(f"  전체 문서: {stats['total']}건")
    logger.info(f"  이미 메타 있어 스킵: {stats['skipped_has_meta']}건")
    logger.info(f"  매핑 성공: {stats['matched']}건 (폴백: {stats['fallback']}건)")
    logger.info(f"  매핑 실패: {stats['unmatched']}건")
    logger.info(f"  업데이트 완료: {stats['updated']}건")
    logger.info(f"  오류: {stats['errors']}건")
    logger.info("=" * 60)

    if not args.dry_run:
        w, wo = verify_state()
        logger.info(f"  [검증] 메타데이터 있음: {w}, 없음: {wo}")


if __name__ == "__main__":
    main()
