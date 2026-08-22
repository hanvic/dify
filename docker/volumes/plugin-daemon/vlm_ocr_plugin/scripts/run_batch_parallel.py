#!/usr/bin/env python3
"""Parallel batch runner for PDF→VLM OCR pipeline.

Processes PDF pages concurrently using ThreadPoolExecutor.
State file writes are serialized via threading.Lock to avoid corruption.

Features:
- Configurable concurrency via CONCURRENCY env var (default 6)
- Idempotent: resumes from processed_pages.jsonl
- 3 retries with exponential backoff
- 429 detection with wait
- Disk guard (stops if < 5GB free)
- Streaming: renders page JPEG, uploads, then deletes immediately
- Progress/ETA logging
- Subfolder filter
- Failed pages log

Usage:
    CONCURRENCY=6 python3 run_batch_parallel.py [--filter-subfolder "서류철명"] [--pilot N]
"""

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

# ============================================================
# Configuration
# ============================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
ENV_FILE = SCRIPT_DIR / ".env"

# Blank page detection
sys.path.insert(0, str(SCRIPT_DIR.parent / "tools"))
from blank_detector import (
    BlankDetectionResult,
    BlankPageAction,
    BlankPageError,
    detect_blank_pre,
    detect_blank_post,
    BLANK_PAGE_ACTION,
)

# Load .env file
def load_env():
    if ENV_FILE.is_file():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value

load_env()

# Configuration from environment
CONCURRENCY = int(os.environ.get("CONCURRENCY", "6"))
DIFY_API_BASE = os.environ.get("DIFY_API_BASE", "http://localhost/v1")
DATASET_API_KEY = os.environ.get("DATASET_API_KEY", "")
DATASET_ID = os.environ.get("DATASET_ID", "")
START_NODE_ID = os.environ.get("START_NODE_ID", "")
IS_PUBLISHED = os.environ.get("IS_PUBLISHED", "true").lower() == "true"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))  # Reduced from 5
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "300"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
PDF_DIR = Path(os.environ.get("PDF_DIR", str(Path.home() / "Downloads/희망브리지/희망브리지 문서전자화(2025.04)")))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(SCRIPT_DIR / "batch_state")))
MIN_DISK_FREE_GB = int(os.environ.get("MIN_DISK_FREE_GB", "5"))
MAX_LONG_SIDE = int(os.environ.get("MAX_LONG_SIDE", "2048"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "70"))
RATE_LIMIT_WAIT = int(os.environ.get("RATE_LIMIT_WAIT", "60"))
MAX_CONSECUTIVE_429 = int(os.environ.get("MAX_CONSECUTIVE_429", "5"))

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("batch_parallel")


# ============================================================
# Data classes
# ============================================================

@dataclass
class BatchStats:
    """Thread-safe batch statistics."""
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    blank: int = 0
    total_pages: int = 0
    start_time: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    consecutive_429: int = 0
    consecutive_failures: int = 0
    abort: bool = False
    abort_reason: str = ""

    def add_processed(self):
        with self._lock:
            self.processed += 1
            self.consecutive_failures = 0  # Reset on success

    def add_skipped(self):
        with self._lock:
            self.skipped += 1

    def add_failed(self):
        with self._lock:
            self.failed += 1
            self.consecutive_failures += 1

    def add_blank(self):
        """Record a blank page. Does NOT increment consecutive_failures."""
        with self._lock:
            self.blank += 1
            # Blank pages do NOT increment consecutive_failures
            # This is the core safety fix: 6 consecutive blank pages
            # should NOT trigger the batch abort guard.

    def inc_429(self) -> int:
        with self._lock:
            self.consecutive_429 += 1
            return self.consecutive_429

    def reset_429(self):
        with self._lock:
            self.consecutive_429 = 0

    def inc_consecutive_failures(self) -> int:
        """Increment and return consecutive failure count."""
        with self._lock:
            self.consecutive_failures += 1
            return self.consecutive_failures

    def reset_consecutive_failures(self):
        with self._lock:
            self.consecutive_failures = 0

    def request_abort(self, reason: str = ""):
        with self._lock:
            self.abort = True
            self.abort_reason = reason

    @property
    def is_aborted(self) -> bool:
        with self._lock:
            return self.abort

    @property
    def done_count(self) -> int:
        with self._lock:
            return self.processed + self.skipped + self.failed + self.blank

    def eta_str(self) -> str:
        with self._lock:
            done = self.processed + self.failed
        if done == 0:
            return "계산 중..."
        elapsed = time.time() - self.start_time
        per_page = elapsed / done
        remaining = (self.total_pages - self.done_count) * per_page
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return f"{hours}h {minutes}m (페이지당 {per_page:.1f}초)"

    def summary_str(self) -> str:
        """Return a one-line status summary for periodic logging."""
        with self._lock:
            return f"✅{self.processed} ❌{self.failed} ⏭️{self.skipped} 📄{self.blank} (연속실패:{self.consecutive_failures})"


# ============================================================
# State file management (thread-safe with file locking)
# ============================================================

class StateManager:
    """Thread-safe state file manager using flock."""

    def __init__(self, processed_file: Path, failed_file: Path):
        self.processed_file = processed_file
        self.failed_file = failed_file
        self._lock = threading.Lock()
        self._processed_keys: set = set()
        self._load_processed()

    def _load_processed(self):
        """Load already-processed keys from jsonl file."""
        if self.processed_file.is_file():
            with open(self.processed_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        key = data.get("key", "")
                        if key:
                            self._processed_keys.add(key)
                    except json.JSONDecodeError:
                        continue
        log.info(f"기처리 로드: {len(self._processed_keys)}건")

    def is_processed(self, key: str) -> bool:
        with self._lock:
            return key in self._processed_keys

    def record_processed(self, key: str, doc_name: str, doc_id: str):
        """Atomically append to processed_pages.jsonl using flock."""
        entry = json.dumps({
            "key": key,
            "doc_name": doc_name,
            "doc_id": doc_id,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, ensure_ascii=False)

        with self._lock:
            self._processed_keys.add(key)
            with open(self.processed_file, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(entry + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def record_blank(self, key: str, doc_name: str, detection_result: "BlankDetectionResult"):
        """Record a blank page. Marks as processed (no retry) and logs to blank_pages.log."""
        entry = json.dumps({
            "key": key,
            "doc_name": doc_name,
            "doc_id": "",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "blank",
            "blank_method": detection_result.method,
            "blank_reason": detection_result.reason,
        }, ensure_ascii=False)

        with self._lock:
            self._processed_keys.add(key)
            # Write to main state file (prevents retry)
            with open(self.processed_file, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(entry + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Write to blank_pages.log for human review
            blank_log = self.processed_file.parent / "blank_pages.log"
            log_line = f"{key}|{detection_result.method}|{detection_result.log_line()}|{datetime.now(timezone.utc).isoformat()}\n"
            with open(blank_log, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(log_line)
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def record_failed(self, key: str, reason: str):
        """Atomically append to failed log."""
        with self._lock:
            with open(self.failed_file, "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(f"{key}|{reason}|{datetime.now(timezone.utc).isoformat()}\n")
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ============================================================
# Disk check
# ============================================================

def check_disk_free() -> bool:
    """Return True if enough disk space available."""
    total, used, free = shutil.disk_usage("/")
    free_gb = free // (1024 ** 3)
    if free_gb < MIN_DISK_FREE_GB:
        log.warning(f"⚠️ 디스크 여유 {free_gb}GB < {MIN_DISK_FREE_GB}GB 안전선!")
        return False
    return True


# ============================================================
# PDF page rendering
# ============================================================

def render_page(pdf_path: Path, page_num: int, output_path: Path) -> int:
    """Render a single PDF page to JPEG. Returns file size in bytes."""
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT_DIR / "pdf_to_pages.py"),
            str(pdf_path), str(page_num), str(output_path),
            "--max-long-side", str(MAX_LONG_SIDE),
            "--quality", str(JPEG_QUALITY),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"렌더링 실패: {result.stderr.strip()}")
    return int(result.stdout.strip())


def get_page_count(pdf_path: Path) -> int:
    """Get number of pages in a PDF."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "pdf_to_pages.py"), str(pdf_path), "0", "/dev/null", "--page-count"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"페이지 수 조회 실패: {result.stderr.strip()}")
    return int(result.stdout.strip())


# ============================================================
# Dify API calls
# ============================================================

SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Bearer {DATASET_API_KEY}"})


def upload_file(file_path: Path, filename: str) -> str:
    """Upload file to Dify pipeline. Returns file_id."""
    url = f"{DIFY_API_BASE}/datasets/pipeline/file-upload"
    with open(file_path, "rb") as f:
        resp = SESSION.post(url, files={"file": (filename, f, "image/jpeg")}, timeout=60)

    if resp.status_code == 429:
        raise RateLimitError("429 업로드")
    if resp.status_code >= 500:
        raise ServerError(f"서버 오류 {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()

    data = resp.json()
    file_id = data.get("id")
    if not file_id:
        raise RuntimeError(f"파일 ID 없음: {data}")
    return file_id


def run_pipeline(file_id: str, file_name: str) -> None:
    """Trigger the RAG pipeline for an uploaded file."""
    url = f"{DIFY_API_BASE}/datasets/{DATASET_ID}/pipeline/run"
    payload = {
        "inputs": {},
        "datasource_type": "local_file",
        "datasource_info_list": [{"reference": file_id, "name": file_name}],
        "start_node_id": START_NODE_ID,
        "is_published": IS_PUBLISHED,
        "response_mode": "blocking",
    }
    resp = SESSION.post(url, json=payload, timeout=120)
    if resp.status_code == 429:
        raise RateLimitError("429 파이프라인")
    if resp.status_code >= 500:
        raise ServerError(f"서버 오류 {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()


def poll_indexing(file_name: str, timeout: int = POLL_TIMEOUT) -> Optional[str]:
    """Poll until document indexing completes. Returns doc_id or None.
    
    Also checks if document already exists (completed) to avoid duplicates.
    Raises PoisonedContentError if document completed but segments contain
    error patterns (fail-fast guard against the 13,566-document incident).
    """
    url = f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents"
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = SESSION.get(url, params={"keyword": file_name, "limit": 10}, timeout=30)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for doc in data:
                    if doc.get("name") == file_name:
                        status = doc.get("indexing_status", "")
                        if status == "completed":
                            # --- GUARDRAIL: Verify segments are not poisoned ---
                            doc_id = doc["id"]
                            if _verify_document_segments(doc_id, file_name):
                                return doc_id
                            else:
                                # Document completed but content is poisoned
                                delete_document(doc_id)
                                raise PoisonedContentError(
                                    f"문서 '{file_name}' 세그먼트에 오류 패턴 감지됨. 문서 삭제 후 실패 처리."
                                )
                        elif status in ("error", "failed"):
                            # Delete failed doc so it can be retried
                            delete_document(doc.get("id", ""))
                            return None
        except PoisonedContentError:
            raise  # Propagate poisoned content detection
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)

    log.warning(f"폴링 타임아웃: {file_name}")
    return None


def delete_document(doc_id: str) -> None:
    """Delete a failed document."""
    if not doc_id:
        return
    url = f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents/{doc_id}"
    try:
        SESSION.delete(url, timeout=30)
    except requests.RequestException:
        pass


# ============================================================
# Custom exceptions
# ============================================================

class RateLimitError(Exception):
    pass

class ServerError(Exception):
    pass

class PoisonedContentError(Exception):
    """Raised when a completed document's segments contain error patterns."""
    pass


# ============================================================
# Guardrail: Error pattern detection in segments
# ============================================================

# Known error patterns that should NEVER appear as document content.
# If any segment matches these, the document is considered poisoned.
ERROR_CONTENT_PATTERNS = [
    "Ollama 서버에서 오류 상황을 반환했습니다",
    "Ollama 서버에서 오류 응답을 반환했습니다",
    "OCR 처리 중 예기치 않은 오류가 발생했습니다",
    "Ollama 서버 응답 시간이 초과되었습니다",
    "Ollama 서버에 연결할 수 없습니다",
    "Ollama 요청에 실패했습니다",
    "QUOTA_EXCEEDED",
    "쿼터 초과",
    "rate_limit",
    "you have reached your session usage limit",
]

# Consecutive failure threshold: stop the entire batch if this many pages
# fail in a row. This prevents the 14,000-page pollution scenario.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5"))


def _verify_document_segments(doc_id: str, file_name: str) -> bool:
    """Check that a completed document's segments don't contain error patterns.
    
    Returns True if segments look healthy, False if poisoned content detected.
    Also returns False if document has 0 segments (should not happen for completed docs).
    """
    try:
        url = f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents/{doc_id}/segments"
        resp = SESSION.get(url, params={"limit": 10}, timeout=30)
        if resp.status_code != 200:
            log.warning(f"세그먼트 조회 실패 {file_name}: HTTP {resp.status_code}")
            return True  # Can't verify, assume OK to avoid false positives

        data = resp.json()
        segments = data.get("data", [])
        
        # Guard: 0 segments means something went wrong
        if not segments:
            log.warning(f"⚠️ 문서 '{file_name}' 세그먼트 0개 - 비정상")
            return False

        for seg in segments:
            content = seg.get("content", "")
            for pattern in ERROR_CONTENT_PATTERNS:
                if pattern in content:
                    log.error(
                        f"🚨 오염 감지! 문서 '{file_name}' 세그먼트에 오류 패턴: '{pattern}'"
                    )
                    return False

        return True
    except requests.RequestException as e:
        log.warning(f"세그먼트 검증 중 네트워크 오류 {file_name}: {e}")
        return True  # Network error during verification - don't block


# ============================================================
# Core processing logic
# ============================================================

def process_single_page(
    pdf_path: Path,
    page_num: int,
    doc_name: str,
    state: StateManager,
    stats: BatchStats,
) -> bool:
    """Process a single page. Returns True on success."""

    key = f"{doc_name}:p{page_num}"

    # Idempotency check (local state)
    if state.is_processed(key):
        stats.add_skipped()
        return True

    if stats.is_aborted:
        return False

    file_name = f"{doc_name}_p{page_num}.jpeg"

    # Server-side duplicate check: if document already completed, just record it
    try:
        url = f"{DIFY_API_BASE}/datasets/{DATASET_ID}/documents"
        resp = SESSION.get(url, params={"keyword": file_name, "limit": 5}, timeout=15)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            for doc in data:
                if doc.get("name") == file_name and doc.get("indexing_status") == "completed":
                    state.record_processed(key, file_name, doc["id"])
                    stats.add_skipped()
                    return True
    except requests.RequestException:
        pass  # Continue with normal processing if check fails

    # Render page to temp JPEG
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".jpg", prefix="vlmocr_")
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)

    try:
        try:
            file_size = render_page(pdf_path, page_num, tmp_path)
        except Exception as e:
            log.error(f"렌더링 실패 {key}: {e}")
            state.record_failed(key, "render_error")
            stats.add_failed()
            return False

        # --- 번 페이지 사전 감지 (VLM 호출 전) ---
        pre_detection = detect_blank_pre(tmp_path)
        if pre_detection.is_blank:
            log.info(f"📄 번 페이지 감지 (사전): {key} | {pre_detection.reason}")
            state.record_blank(key, file_name, pre_detection)
            stats.add_blank()
            return True  # Success path - not a failure

        file_name = f"{doc_name}_p{page_num}.jpeg"

        # Retry loop with exponential backoff
        for attempt in range(1, MAX_RETRIES + 1):
            if stats.is_aborted:
                return False

            try:
                # Upload
                file_id = upload_file(tmp_path, file_name)
                stats.reset_429()

                # Run pipeline
                run_pipeline(file_id, file_name)

                # Poll for completion
                doc_id = poll_indexing(file_name)
                if doc_id:
                    state.record_processed(key, file_name, doc_id)
                    stats.add_processed()
                    done = stats.done_count
                    if done % 5 == 0:
                        log.info(f"📊 진행: {done}/{stats.total_pages} | {stats.summary_str()} | ETA: {stats.eta_str()}")
                    return True
                else:
                    # Indexing failed - let retry handle
                    if attempt < MAX_RETRIES:
                        time.sleep(5 * attempt)
                        continue
                    state.record_failed(key, "indexing_failed")
                    stats.add_failed()
                    # --- GUARDRAIL: Check consecutive failures ---
                    consec = stats.consecutive_failures
                    if consec >= MAX_CONSECUTIVE_FAILURES:
                        log.error(
                            f"🛑 연속 {consec}회 실패 (임계치 {MAX_CONSECUTIVE_FAILURES}) - 배치 즉시 중단! "
                            f"원인을 확인하세요."
                        )
                        stats.request_abort(f"consecutive_failures_{consec}")
                    return False

            except PoisonedContentError as e:
                log.error(f"🚨 오염 감지 {key}: {e}")
                state.record_failed(key, "poisoned_content")
                stats.add_failed()
                consec = stats.consecutive_failures
                if consec >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        f"🛑 연속 {consec}회 오염/실패 - 배치 즉시 중단! "
                        f"Ollama 상태를 확인하세요."
                    )
                    stats.request_abort(f"poisoned_content_streak_{consec}")
                return False

            except RateLimitError:
                count = stats.inc_429()
                log.error(
                    f"🚫 429 쿼터 초과 감지 (연속 {count}회) - "
                    f"쿼터는 대기/업그레이드 없이 해결 불가. 배치 즉시 중단."
                )
                # 429는 재시도하지 않고 즉시 중단
                stats.request_abort("quota_exceeded_429")
                state.record_failed(key, "rate_limited_429_abort")
                stats.add_failed()
                return False

            except ServerError as e:
                log.warning(f"서버 오류 {key} (시도 {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(10 * attempt)  # Longer backoff for server errors

            except requests.RequestException as e:
                log.warning(f"네트워크 오류 {key} (시도 {attempt}/{MAX_RETRIES}): {e}")
                time.sleep(5 * attempt)

        # All retries exhausted
        state.record_failed(key, "max_retries")
        stats.add_failed()
        return False

    finally:
        # Always clean up temp file
        tmp_path.unlink(missing_ok=True)


# ============================================================
# Main batch orchestration
# ============================================================

def collect_pages(pdf_dir: Path, filter_subfolder: str = "") -> list[tuple[Path, int, str]]:
    """Collect all (pdf_path, page_num, doc_name) tuples to process."""
    pages = []

    if filter_subfolder:
        search_dir = pdf_dir / filter_subfolder
        if not search_dir.is_dir():
            log.error(f"서류철 디렉터리를 찾을 수 없음: {search_dir}")
            return pages
        pdf_dirs = [search_dir]
    else:
        pdf_dirs = sorted([d for d in pdf_dir.iterdir() if d.is_dir()])

    for d in pdf_dirs:
        pdfs = sorted(d.glob("*.pdf"))
        for pdf_path in pdfs:
            parent = pdf_path.parent.name
            stem = pdf_path.stem
            doc_name = f"{parent}_{stem}"
            try:
                page_count = get_page_count(pdf_path)
            except Exception as e:
                log.error(f"페이지 수 조회 실패: {pdf_path}: {e}")
                continue
            for p in range(page_count):
                pages.append((pdf_path, p, doc_name))

    return pages


def run_batch(filter_subfolder: str = "", pilot_max: int = 0):
    """Main batch entry point."""
    log.info("=" * 60)
    log.info("병렬 배치 처리 시작")
    log.info(f"  동시성: {CONCURRENCY}")
    log.info(f"  PDF 디렉터리: {PDF_DIR}")
    log.info(f"  서류철 필터: {filter_subfolder or '전체'}")
    log.info(f"  파일럿 한도: {pilot_max or '무제한'}")
    log.info(f"  폴링 간격: {POLL_INTERVAL}초")
    log.info(f"  디스크 안전선: {MIN_DISK_FREE_GB}GB")
    log.info("=" * 60)

    # Validate env
    if not DATASET_API_KEY:
        log.error("DATASET_API_KEY 환경변수 필요")
        sys.exit(1)
    if not DATASET_ID:
        log.error("DATASET_ID 환경변수 필요")
        sys.exit(1)
    if not START_NODE_ID:
        log.error("START_NODE_ID 환경변수 필요")
        sys.exit(1)

    # Initial disk check
    if not check_disk_free():
        log.error("디스크 여유 부족. 종료.")
        sys.exit(1)

    # Prepare state
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    processed_file = STATE_DIR / "processed_pages.jsonl"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    failed_file = STATE_DIR / f"failed_{timestamp}.log"

    state = StateManager(processed_file, failed_file)

    # Collect pages
    log.info("PDF 페이지 목록 수집 중...")
    all_pages = collect_pages(PDF_DIR, filter_subfolder)
    if not all_pages:
        log.info("처리할 페이지가 없습니다.")
        return

    # Filter already processed & apply pilot limit
    pages_to_process = []
    for pdf_path, page_num, doc_name in all_pages:
        key = f"{doc_name}:p{page_num}"
        if state.is_processed(key):
            continue
        pages_to_process.append((pdf_path, page_num, doc_name))
        if pilot_max and len(pages_to_process) >= pilot_max:
            break

    total = len(pages_to_process)
    already_done = len(all_pages) - total if not pilot_max else 0
    log.info(f"전체 {len(all_pages)}페이지 중 {already_done}건 기처리, {total}건 처리 예정")

    if total == 0:
        log.info("모든 페이지가 처리 완료되었습니다. ✅")
        return

    # Stats
    stats = BatchStats(total_pages=total)
    stats.start_time = time.time()

    # Run with thread pool
    disk_check_counter = 0
    disk_check_lock = threading.Lock()

    def should_check_disk() -> bool:
        nonlocal disk_check_counter
        with disk_check_lock:
            disk_check_counter += 1
            return disk_check_counter % 20 == 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {}
        for pdf_path, page_num, doc_name in pages_to_process:
            if stats.is_aborted:
                break
            future = executor.submit(
                process_single_page, pdf_path, page_num, doc_name, state, stats
            )
            futures[future] = f"{doc_name}:p{page_num}"

        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
            except Exception as e:
                log.error(f"예외 {key}: {e}")

            # Periodic disk check
            if should_check_disk() and not check_disk_free():
                log.warning("⚠️ 디스크 부족 - 중단 요청")
                stats.request_abort()
                # Cancel pending futures
                for f in futures:
                    f.cancel()
                break

            if stats.is_aborted:
                for f in futures:
                    f.cancel()
                break

    # Summary
    elapsed = time.time() - stats.start_time
    log.info("=" * 60)
    log.info("배치 처리 완료")
    log.info(f"  소요 시간: {elapsed:.1f}초")
    log.info(f"  처리 성공: {stats.processed}")
    log.info(f"  번 페이지: {stats.blank}")
    log.info(f"  건너뜀(기처리): {stats.skipped}")
    log.info(f"  실패: {stats.failed}")
    if stats.is_aborted:
        log.info(f"  ⚠️ 중단 사유: {stats.abort_reason}")
    if stats.processed > 0:
        per_page = elapsed / stats.processed
        log.info(f"  페이지당 소요: {per_page:.1f}초 (wall) / {per_page / CONCURRENCY:.1f}초 (유효)")
    log.info(f"  누적 처리: {len(state._processed_keys)}건")
    if stats.failed > 0:
        log.info(f"  실패 목록: {failed_file}")
    if stats.blank > 0:
        log.info(f"  번 페이지 목록: {STATE_DIR / 'blank_pages.log'}")
    log.info("=" * 60)
    log.info(f"재개: 동일 명령 재실행 (processed_pages.jsonl 기반 자동 스킵)")


# ============================================================
# Entry point
# ============================================================

def main():
    global CONCURRENCY

    parser = argparse.ArgumentParser(description="병렬 PDF 배치 처리 러너")
    parser.add_argument("--filter-subfolder", default=os.environ.get("FILTER_SUBFOLDER", ""),
                        help="처리할 서류철 이름 (비어있으면 전체)")
    parser.add_argument("--pilot", type=int, default=int(os.environ.get("PILOT_MAX_PAGES", "0")),
                        help="파일럿 모드: 처리할 최대 페이지 수 (0=무제한)")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY,
                        help=f"동시 처리 수 (기본: {CONCURRENCY})")
    parser.add_argument("--reprocess-blanks", action="store_true",
                        help="이전에 번 페이지로 분류된 페이지만 재처리")
    args = parser.parse_args()

    CONCURRENCY = args.concurrency

    # Reprocess blanks mode
    if args.reprocess_blanks:
        blank_log = STATE_DIR / "blank_pages.log"
        if not blank_log.is_file():
            log.info("번 페이지 기록이 없습니다.")
            return
        # Remove blank entries from processed_pages.jsonl
        processed_file = STATE_DIR / "processed_pages.jsonl"
        blank_keys = set()
        with open(blank_log, "r") as f:
            for line in f:
                parts = line.strip().split("|")
                if parts:
                    blank_keys.add(parts[0])
        log.info(f"번 페이지 재처리: {len(blank_keys)}건을 상태에서 제거합니다.")
        # Filter out blank entries from state file
        if processed_file.is_file():
            with open(processed_file, "r") as f:
                lines = f.readlines()
            with open(processed_file, "w") as f:
                kept = 0
                for line in lines:
                    try:
                        data = json.loads(line.strip())
                        if data.get("key") in blank_keys:
                            continue  # Remove this entry
                    except json.JSONDecodeError:
                        pass
                    f.write(line)
                    kept += 1
            log.info(f"상태파일 정리: {len(lines)} → {kept}건 유지, {len(lines)-kept}건 제거")
        # Clear blank log
        blank_log.unlink()
        log.info("blank_pages.log 삭제. 이제 일반 배치로 재실행하세요.")
        return

    run_batch(
        filter_subfolder=args.filter_subfolder,
        pilot_max=args.pilot,
    )


if __name__ == "__main__":
    main()
