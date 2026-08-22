#!/usr/bin/env python3
"""
오염 문서 삭제 스크립트 (Dify Knowledge API 사용)
- 모든 세그먼트가 "Ollama 서버에서 오류 응답을 반환했습니다."인 문서를 삭제
- 진행률 출력, 재시도 로직, 중단 후 재개 가능
- 삭제 완료된 doc_id는 progress 파일에 기록하여 중복 삭제 방지
"""

import csv
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# ─── 설정 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"
BACKUP_CSV = SCRIPT_DIR / "polluted_docs_backup.csv"
PROGRESS_FILE = SCRIPT_DIR / "cleanup_progress.txt"

# Rate limiting / retry
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2  # seconds
REQUEST_DELAY = 0.05  # 50ms between requests to avoid overwhelming the server
BATCH_REPORT_INTERVAL = 100  # 매 100건마다 진행률 보고

load_dotenv(ENV_PATH)

API_BASE = os.getenv("DIFY_BASE_URL", "http://localhost/v1").rstrip("/")
API_TOKEN = os.getenv("DIFY_API_TOKEN", "")
DATASET_ID = os.getenv("DIFY_DATASET_ID", "")

if not API_TOKEN:
    print("ERROR: DIFY_API_TOKEN이 .env에 설정되지 않았습니다.")
    sys.exit(1)

if not DATASET_ID:
    print("ERROR: DIFY_DATASET_ID가 .env에 설정되지 않았습니다.")
    sys.exit(1)


# ─── 유틸리티 ─────────────────────────────────────────────────────────────────
def load_completed_ids() -> set:
    """이미 삭제 완료된 document_id 목록 로드"""
    completed = set()
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    completed.add(line)
    return completed


def save_completed_id(doc_id: str):
    """삭제 완료된 document_id를 progress 파일에 추가"""
    with open(PROGRESS_FILE, "a") as f:
        f.write(doc_id + "\n")


def load_target_docs() -> list[tuple[str, str]]:
    """백업 CSV에서 삭제 대상 문서 목록 로드 (id, name)"""
    docs = []
    with open(BACKUP_CSV, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                docs.append((row[0], row[1]))
    return docs


def delete_document(doc_id: str) -> bool:
    """Dify API로 문서 삭제. 성공 시 True, 실패 시 False"""
    url = f"{API_BASE}/datasets/{DATASET_ID}/documents/{doc_id}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.delete(url, headers=headers, timeout=30)

            if resp.status_code == 204:
                return True
            elif resp.status_code == 404:
                # 이미 삭제된 문서
                print(f"  ⚠ {doc_id}: 404 (이미 삭제됨)")
                return True
            elif resp.status_code == 429:
                # Rate limit
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                print(f"  ⏳ Rate limit (429). {wait}초 대기 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            elif resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                print(f"  ⚠ 서버 오류 {resp.status_code}. {wait}초 대기 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            else:
                print(f"  ✗ {doc_id}: HTTP {resp.status_code} - {resp.text[:200]}")
                return False

        except requests.exceptions.Timeout:
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            print(f"  ⏳ 타임아웃. {wait}초 대기 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            continue
        except requests.exceptions.ConnectionError as e:
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            print(f"  ⚠ 연결 오류: {e}. {wait}초 대기 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

    print(f"  ✗ {doc_id}: {MAX_RETRIES}회 재시도 실패")
    return False


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("오염 문서 삭제 스크립트 (Dify Knowledge API)")
    print("=" * 70)

    if not BACKUP_CSV.exists():
        print(f"ERROR: 백업 파일이 없습니다: {BACKUP_CSV}")
        sys.exit(1)

    # 대상 로드
    all_docs = load_target_docs()
    total = len(all_docs)
    print(f"총 삭제 대상: {total}건")

    # 이미 완료된 건 로드
    completed = load_completed_ids()
    already_done = len(completed)
    print(f"이미 완료된 건: {already_done}건")
    print(f"남은 작업: {total - already_done}건")
    print("-" * 70)

    success_count = already_done
    fail_count = 0
    start_time = time.time()

    for idx, (doc_id, doc_name) in enumerate(all_docs, 1):
        if doc_id in completed:
            continue

        result = delete_document(doc_id)

        if result:
            save_completed_id(doc_id)
            completed.add(doc_id)
            success_count += 1
        else:
            fail_count += 1

        # 진행률 보고
        if (success_count - already_done + fail_count) % BATCH_REPORT_INTERVAL == 0:
            elapsed = time.time() - start_time
            rate = (success_count - already_done + fail_count) / elapsed if elapsed > 0 else 0
            remaining = (total - success_count - fail_count) / rate if rate > 0 else 0
            print(
                f"[진행] {success_count}/{total} 삭제 완료 | "
                f"실패: {fail_count} | "
                f"속도: {rate:.1f}건/초 | "
                f"예상 남은 시간: {remaining:.0f}초"
            )

        time.sleep(REQUEST_DELAY)

    # 최종 보고
    elapsed = time.time() - start_time
    print("=" * 70)
    print("삭제 완료 요약")
    print("=" * 70)
    print(f"총 대상: {total}건")
    print(f"성공: {success_count}건")
    print(f"실패: {fail_count}건")
    print(f"소요 시간: {elapsed:.1f}초")
    if elapsed > 0:
        print(f"평균 속도: {(success_count - already_done) / elapsed:.1f}건/초")

    if fail_count > 0:
        print(f"\n⚠ 실패 {fail_count}건이 있습니다. 스크립트를 다시 실행하면 남은 건을 이어서 처리합니다.")
        sys.exit(1)
    else:
        print("\n✓ 모든 오염 문서가 성공적으로 삭제되었습니다.")


if __name__ == "__main__":
    main()
