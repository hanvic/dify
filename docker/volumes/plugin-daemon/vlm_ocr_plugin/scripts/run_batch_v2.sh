#!/usr/bin/env bash
set -euo pipefail

# run_batch_v2.sh
# PDF→페이지→VLM OCR 배치 러너 (스트리밍 방식)
# PDF를 1페이지씩 렌더링 → 압축 JPEG → Dify 파이프라인 업로드 → 즉시 삭제
# 디스크 안전 가드, 중단/재개, 429 감지 포함.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
fi

# --- Configuration ---
DIFY_API_BASE="${DIFY_API_BASE:-http://localhost/v1}"
DATASET_API_KEY="${DATASET_API_KEY:-}"
DATASET_ID="${DATASET_ID:-}"
START_NODE_ID="${START_NODE_ID:-}"
IS_PUBLISHED="${IS_PUBLISHED:-true}"
RESPONSE_MODE="blocking"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
POLL_TIMEOUT="${POLL_TIMEOUT:-4200}"
MAX_RETRIES="${MAX_RETRIES:-3}"

# PDF source directory
PDF_DIR="${PDF_DIR:-${HOME}/Downloads/희망브리지/희망브리지 문서전자화(2025.04)}"
# State tracking
STATE_DIR="${STATE_DIR:-${SCRIPT_DIR}/batch_state}"
PROCESSED_FILE="${STATE_DIR}/processed_pages.jsonl"
FAILED_LOG="${STATE_DIR}/failed_$(date +%Y%m%d_%H%M%S).log"
BATCH_LOG="${STATE_DIR}/batch_run_$(date +%Y%m%d_%H%M%S).log"

# Disk safety
MIN_DISK_FREE_GB="${MIN_DISK_FREE_GB:-5}"
DISK_CHECK_INTERVAL="${DISK_CHECK_INTERVAL:-10}"

# Rate limit handling
MAX_CONSECUTIVE_429="${MAX_CONSECUTIVE_429:-5}"
RATE_LIMIT_WAIT="${RATE_LIMIT_WAIT:-60}"

# Image compression (per REVIEW: ≤200KB average)
MAX_LONG_SIDE="${MAX_LONG_SIDE:-2048}"
JPEG_QUALITY="${JPEG_QUALITY:-70}"

# Pilot mode: limit pages for testing
PILOT_MAX_PAGES="${PILOT_MAX_PAGES:-0}"  # 0 = no limit

# Filter: specific subfolder (서류철) to process
FILTER_SUBFOLDER="${FILTER_SUBFOLDER:-}"  # empty = all

# --- Global state ---
TOTAL_PROCESSED=0
TOTAL_FAILED=0
TOTAL_SKIPPED=0
CONSECUTIVE_429=0
FILE_ID=""
DOC_ID=""
INDEX_STATUS=""

# --- Helpers ---
log() {
  local msg="[$(date '+%H:%M:%S')] $*"
  echo "${msg}"
  echo "${msg}" >> "${BATCH_LOG}"
}

error_exit() {
  log "❌ 치명적 오류: $*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || error_exit "$1이(가) 설치되어 있지 않습니다."
}

require_env() {
  local name=$1
  if [[ -z "${!name:-}" ]]; then
    error_exit "환경 변수 ${name}이(가) 필요합니다."
  fi
}

# Disk safety check
check_disk_free() {
  local free_gb
  free_gb=$(df -g / | awk 'NR==2 {print $4}')
  if [[ "${free_gb}" -lt "${MIN_DISK_FREE_GB}" ]]; then
    log "⚠️  디스크 여유 ${free_gb}GB < ${MIN_DISK_FREE_GB}GB 안전선. 배치를 중단합니다."
    log "재개하려면 디스크 공간을 확보한 후 동일 명령을 다시 실행하세요."
    return 1
  fi
  return 0
}

# Check if a page was already processed
is_page_processed() {
  local key=$1
  if [[ ! -f "${PROCESSED_FILE}" ]]; then
    return 1
  fi
  grep -qF "\"key\":\"${key}\"" "${PROCESSED_FILE}" 2>/dev/null
}

# Record processed page
record_processed() {
  local key=$1 doc_name=$2 doc_id=$3
  printf '{"key":"%s","doc_name":"%s","doc_id":"%s","ts":"%s"}\n' \
    "${key}" "${doc_name}" "${doc_id}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${PROCESSED_FILE}"
}

# Record failed page
record_failed() {
  local key=$1 reason=$2
  echo "${key}|${reason}" >> "${FAILED_LOG}"
}

# Upload file to Dify pipeline
upload_file() {
  local file_path=$1 original_name=$2
  local response http_code body

  FILE_ID=""
  
  # Use -w to capture HTTP status separately
  response=$(curl -sS -w "\n%{http_code}" -X POST "${DIFY_API_BASE}/datasets/pipeline/file-upload" \
    -H "Authorization: Bearer ${DATASET_API_KEY}" \
    -F "file=@${file_path};filename=${original_name}" 2>&1) || {
    log "업로드 네트워크 오류"
    return 1
  }

  http_code=$(echo "${response}" | tail -1)
  body=$(echo "${response}" | sed '$d')

  if [[ "${http_code}" == "429" ]]; then
    log "⚠️  429 Rate Limited"
    return 2  # Special code for rate limit
  fi

  if [[ "${http_code}" != "200" && "${http_code}" != "201" ]]; then
    log "업로드 실패 HTTP ${http_code}: ${body}"
    return 1
  fi

  FILE_ID=$(echo "${body}" | jq -r '.id // empty' 2>/dev/null)
  if [[ -z "${FILE_ID}" || "${FILE_ID}" == "null" ]]; then
    log "파일 ID 추출 실패: ${body}"
    return 1
  fi
  return 0
}

# Run pipeline
run_pipeline() {
  local file_id=$1 file_name=$2
  local payload response http_code body

  local is_pub_bool="true"
  if [[ "${IS_PUBLISHED}" != "true" ]]; then
    is_pub_bool="false"
  fi

  payload=$(cat <<EOF
{"inputs":{},"datasource_type":"local_file","datasource_info_list":[{"reference":"${file_id}","name":"${file_name}"}],"start_node_id":"${START_NODE_ID}","is_published":${is_pub_bool},"response_mode":"${RESPONSE_MODE}"}
EOF
)

  response=$(curl -sS -w "\n%{http_code}" -X POST "${DIFY_API_BASE}/datasets/${DATASET_ID}/pipeline/run" \
    -H "Authorization: Bearer ${DATASET_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "${payload}" 2>&1) || {
    log "파이프라인 실행 네트워크 오류"
    return 1
  }

  http_code=$(echo "${response}" | tail -1)
  body=$(echo "${response}" | sed '$d')

  if [[ "${http_code}" == "429" ]]; then
    log "⚠️  429 Rate Limited (pipeline)"
    return 2
  fi

  if [[ "${http_code}" != "200" && "${http_code}" != "201" ]]; then
    log "파이프라인 실행 실패 HTTP ${http_code}: ${body}"
    return 1
  fi

  return 0
}

# Poll document indexing status
poll_indexing() {
  local file_name=$1
  local deadline response status

  deadline=$(($(date +%s) + POLL_TIMEOUT))
  DOC_ID=""

  while [[ "$(date +%s)" -lt "${deadline}" ]]; do
    response=$(curl -sS -G "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents" \
      -H "Authorization: Bearer ${DATASET_API_KEY}" \
      --data-urlencode "keyword=${file_name}" \
      --data-urlencode "limit=5" 2>&1) || {
      sleep "${POLL_INTERVAL}"
      continue
    }

    # Extract doc_id and status using jq
    DOC_ID=$(echo "${response}" | jq -r --arg name "${file_name}" '.data // [] | .[] | select(.name == $name) | .id' 2>/dev/null | head -1)
    status=$(echo "${response}" | jq -r --arg name "${file_name}" '.data // [] | .[] | select(.name == $name) | .indexing_status' 2>/dev/null | head -1)

    if [[ -n "${DOC_ID}" && "${DOC_ID}" != "null" ]]; then
      case "${status}" in
        completed)
          return 0
          ;;
        error|failed)
          log "문서 인덱싱 실패: ${file_name} (${DOC_ID})"
          return 1
          ;;
      esac
    fi

    sleep "${POLL_INTERVAL}"
  done

  log "폴링 타임아웃: ${file_name}"
  return 1
}

# Delete failed document
delete_document() {
  local doc_id=$1
  [[ -z "${doc_id}" ]] && return 0
  curl -sS -X DELETE "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents/${doc_id}" \
    -H "Authorization: Bearer ${DATASET_API_KEY}" >/dev/null 2>&1 || true
}

# Process a single page
process_page() {
  local pdf_path=$1 page_num=$2 doc_name=$3
  local key tmp_jpeg jpeg_size try upload_rc

  key="${doc_name}:p${page_num}"

  # Skip if already processed
  if is_page_processed "${key}"; then
    TOTAL_SKIPPED=$((TOTAL_SKIPPED + 1))
    return 0
  fi

  # Render page to temporary JPEG
  tmp_jpeg=$(mktemp -t vlmocr_page.XXXXXX.jpg)
  
  jpeg_size=$(python3 "${SCRIPT_DIR}/pdf_to_pages.py" \
    "${pdf_path}" "${page_num}" "${tmp_jpeg}" \
    --max-long-side "${MAX_LONG_SIDE}" --quality "${JPEG_QUALITY}" 2>&1) || {
    log "페이지 렌더링 실패: ${key}"
    rm -f "${tmp_jpeg}"
    record_failed "${key}" "render_error"
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
    return 1
  }

  log "  렌더링 완료: ${key} (${jpeg_size} bytes)"

  # Upload and run pipeline with retries
  for ((try = 1; try <= MAX_RETRIES; try++)); do
    FILE_ID=""
    DOC_ID=""

    upload_rc=0
    upload_file "${tmp_jpeg}" "${doc_name}_p${page_num}.jpeg" || upload_rc=$?

    if [[ "${upload_rc}" -eq 2 ]]; then
      # Rate limited
      CONSECUTIVE_429=$((CONSECUTIVE_429 + 1))
      if [[ "${CONSECUTIVE_429}" -ge "${MAX_CONSECUTIVE_429}" ]]; then
        log "❌ 연속 ${MAX_CONSECUTIVE_429}회 429 — 배치 중단"
        rm -f "${tmp_jpeg}"
        return 2
      fi
      log "429 대기 ${RATE_LIMIT_WAIT}초... (연속 ${CONSECUTIVE_429}/${MAX_CONSECUTIVE_429})"
      sleep "${RATE_LIMIT_WAIT}"
      continue
    elif [[ "${upload_rc}" -ne 0 ]]; then
      sleep 5
      continue
    fi

    # Reset 429 counter on success
    CONSECUTIVE_429=0

    # Run pipeline
    local pipe_rc=0
    run_pipeline "${FILE_ID}" "${doc_name}_p${page_num}.jpeg" || pipe_rc=$?
    
    if [[ "${pipe_rc}" -eq 2 ]]; then
      CONSECUTIVE_429=$((CONSECUTIVE_429 + 1))
      if [[ "${CONSECUTIVE_429}" -ge "${MAX_CONSECUTIVE_429}" ]]; then
        log "❌ 연속 429 — 배치 중단"
        rm -f "${tmp_jpeg}"
        return 2
      fi
      sleep "${RATE_LIMIT_WAIT}"
      continue
    elif [[ "${pipe_rc}" -ne 0 ]]; then
      delete_document "${DOC_ID}"
      sleep 5
      continue
    fi

    # Poll for completion
    if poll_indexing "${doc_name}_p${page_num}.jpeg"; then
      record_processed "${key}" "${doc_name}_p${page_num}.jpeg" "${DOC_ID}"
      TOTAL_PROCESSED=$((TOTAL_PROCESSED + 1))
      rm -f "${tmp_jpeg}"
      return 0
    fi

    delete_document "${DOC_ID}"
    sleep 5
  done

  log "최대 재시도 초과: ${key}"
  rm -f "${tmp_jpeg}"
  record_failed "${key}" "max_retries"
  TOTAL_FAILED=$((TOTAL_FAILED + 1))
  return 1
}

# --- Main ---
require_command curl
require_command python3
require_command jq
require_env DATASET_API_KEY
require_env DATASET_ID
require_env START_NODE_ID

mkdir -p "${STATE_DIR}"
touch "${PROCESSED_FILE}" "${FAILED_LOG}"
: > "${FAILED_LOG}"

AUTH_HEADER="Authorization: Bearer ${DATASET_API_KEY}"

log "============================================"
log "PDF 배치 처리 시작"
log "PDF 디렉터리: ${PDF_DIR}"
log "상태 디렉터리: ${STATE_DIR}"
log "디스크 안전선: ${MIN_DISK_FREE_GB}GB"
log "파일럿 한도: ${PILOT_MAX_PAGES} (0=무제한)"
log "서류철 필터: ${FILTER_SUBFOLDER:-전체}"
log "============================================"

# Initial disk check
check_disk_free || exit 1

# Find all PDF files
mapfile -t pdf_files < <(
  if [[ -n "${FILTER_SUBFOLDER}" ]]; then
    find "${PDF_DIR}/${FILTER_SUBFOLDER}" -name "*.pdf" -type f 2>/dev/null | sort
  else
    find "${PDF_DIR}" -name "*.pdf" -type f 2>/dev/null | sort
  fi
)

if [[ ${#pdf_files[@]} -eq 0 ]]; then
  log "처리할 PDF 파일이 없습니다."
  exit 0
fi

log "PDF 파일 수: ${#pdf_files[@]}"

pages_done=0

for pdf_path in "${pdf_files[@]}"; do
  # Get filename without extension for document naming
  pdf_basename=$(basename "${pdf_path}" .pdf)
  # Get parent folder name (서류철명)
  parent_dir=$(basename "$(dirname "${pdf_path}")")
  doc_name="${parent_dir}_${pdf_basename}"

  # Get page count
  page_count=$(python3 "${SCRIPT_DIR}/pdf_to_pages.py" "${pdf_path}" 0 /dev/null --page-count 2>/dev/null) || {
    log "페이지 수 조회 실패: ${pdf_path}"
    record_failed "${doc_name}:all" "page_count_error"
    continue
  }

  log "📄 ${doc_name} (${page_count}페이지)"

  for ((page = 0; page < page_count; page++)); do
    # Pilot limit check
    if [[ "${PILOT_MAX_PAGES}" -gt 0 && "${pages_done}" -ge "${PILOT_MAX_PAGES}" ]]; then
      log "✅ 파일럿 한도 ${PILOT_MAX_PAGES}페이지 도달. 종료."
      break 2
    fi

    # Disk check every N pages
    if [[ $((pages_done % DISK_CHECK_INTERVAL)) -eq 0 && "${pages_done}" -gt 0 ]]; then
      check_disk_free || {
        log "디스크 부족으로 중단. 재개: 동일 명령 재실행."
        break 2
      }
    fi

    process_page "${pdf_path}" "${page}" "${doc_name}"
    page_rc=$?
    if [[ "${page_rc}" -eq 2 ]]; then
      log "Rate limit 초과로 중단."
      break 2
    fi

    pages_done=$((pages_done + 1))
  done
done

log "============================================"
log "배치 처리 완료"
log "  처리 성공: ${TOTAL_PROCESSED}"
log "  건너뜀(기처리): ${TOTAL_SKIPPED}"
log "  실패: ${TOTAL_FAILED}"
log "  총 진행: ${pages_done}페이지"
log "============================================"

if [[ "${TOTAL_FAILED}" -gt 0 ]]; then
  log "실패 목록: ${FAILED_LOG}"
fi

# Print resumption info
already_done=$(wc -l < "${PROCESSED_FILE}" | tr -d ' ')
log ""
log "📊 누적 처리량: ${already_done}페이지"
log "📋 재개 명령: 동일 명령 재실행 (processed_pages.jsonl 기반 자동 스킵)"
