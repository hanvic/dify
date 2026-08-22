#!/usr/bin/env python3
"""
과분할 문서 삭제 스크립트 (Dify Knowledge API 사용)

- 새 설정(세그먼트 1개)으로 적재된 2건을 제외한 926건을 삭제
- oversplit_docs_backup.csv에서 대상 로드
- 진행률 출력, 재시도 로직, 중단 후 재개 가능
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
BACKUP_CSV = SCRIPT_DIR / "oversplit_docs_backup.csv"
PROGRESS_FILE = SCRIPT_DIR / "oversplit_cleanup_progress.txt"

# Rate limiting / retry
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2
REQUEST_DELAY = 0.05
BATCH_REPORT_INTERVAL = 50

load_dotenv(ENV_PATH)

API_BASE = os.getenv("DIFY_BASE_URL", "http://localhost/v1").rstrip("/")
API_TOKEN = os.getenv("DIFY_API_TOKEN", "")
DATASET_ID = os.getenv("DIFY_DATASET_ID", "")

if not API_TOKEN or not DATASET_ID:
    print("ERROR: DIFY_API_TOKEN/DIFY_DATASET_ID가 .env에 설정되지 않았습니다.")
    sys.exit(1)


def load_completed_ids() -> set:
    completed = set()
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    completed.add(line)
    return completed


def save_completed_id(doc_id: str):
    with open(PROGRESS_FILE, "a") as f:
        f.write(doc_id + "\n")


def load_target_docs() -> list[tuple[str, str]]:
    """백업 CSV에서 삭제 대상 문서 목록 로드 (id, name). 헤더 건너뜀."""
    docs = []
    with open(BACKUP_CSV, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # 헤더 건너뜀
        for row in reader:
            if len(row) >= 2:
                docs.append((row[0], row[1]))
    return docs


def delete_document(doc_id: str) -> bool:
    url = f"{API_BASE}/datasets/{DATASET_ID}/documents/{doc_id}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.delete(url, headers=headers, timeout=30)
            if resp.status_code == 204:
                return True
            elif resp.status_code == 404:
                return True
            elif resp.status_code == 429:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                print(f"  ⏳ Rate limit. {wait}초 대기 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            elif resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                print(f"  ⚠ 서버 오류 {resp.status_code}. {wait}초 대기 ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            else:
                print(f"  ✗ {doc_id}: HTTP {resp.status_code} - {resp.text[:200]}")
                return False
        except requests.exceptions.Timeout:
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            time.sleep(wait)
            continue
        except requests.exceptions.ConnectionError as e:
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            time.sleep(wait)
            continue

    print(f"  ✗ {doc_id}: {MAX_RETRIES}회 재시도 실패")
    return False


def main():
    print("=" * 70)
    print("과분할 문서 삭제 스크립트 (Dify Knowledge API)")
    print("=" * 70)

    if not BACKUP_CSV.exists():
        print(f"ERROR: 백업 파일이 없습니다: {BACKUP_CSV}")
        sys.exit(1)

    all_docs = load_target_docs()
    total = len(all_docs)
    print(f"총 삭제 대상: {total}건")

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

        processed = success_count - already_done + fail_count
        if processed % BATCH_REPORT_INTERVAL == 0:
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            remaining = (total - success_count - fail_count) / rate if rate > 0 else 0
            print(
                f"[진행] {success_count}/{total} 삭제 완료 | "
                f"실패: {fail_count} | "
                f"속도: {rate:.1f}건/초 | "
                f"남은 시간: {remaining:.0f}초"
            )

        time.sleep(REQUEST_DELAY)

    elapsed = time.time() - start_time
    print("=" * 70)
    print("삭제 완료 요약")
    print("=" * 70)
    print(f"총 대상: {total}건")
    print(f"성공: {success_count}건")
    print(f"실패: {fail_count}건")
    print(f"소요 시간: {elapsed:.1f}초")

    if fail_count > 0:
        print(f"\n⚠ 실패 {fail_count}건. 스크립트를 다시 실행하면 이어서 처리합니다.")
        sys.exit(1)
    else:
        print("\n✓ 모든 과분할 문서가 성공적으로 삭제되었습니다.")


if __name__ == "__main__":
    main()
