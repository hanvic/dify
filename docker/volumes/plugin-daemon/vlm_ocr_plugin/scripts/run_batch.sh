#!/usr/bin/env bash
set -euo pipefail

# run_batch.sh
# Batch wrapper that walks an image directory and feeds each JPEG/PNG into a
# Dify knowledge pipeline (VLM OCR -> chunker -> index).
# Processes images sequentially, retries failures, and records progress.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables if .env exists.
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
fi

# --- Configuration defaults ---
DIFY_API_BASE="${DIFY_API_BASE:-http://localhost/v1}"
DATASET_API_KEY="${DATASET_API_KEY:-}"
DATASET_ID="${DATASET_ID:-}"
START_NODE_ID="${START_NODE_ID:-}"
IS_PUBLISHED="${IS_PUBLISHED:-true}"
# Blocking mode is mandatory for batch processing.
RESPONSE_MODE="blocking"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
POLL_TIMEOUT="${POLL_TIMEOUT:-4200}"
IMAGE_DIR="${IMAGE_DIR:-${HOME}/Downloads/docu_conv_jpeg}"
HASHES_FILE="${HASHES_FILE:-}"
FAILED_LOG="${FAILED_LOG:-}"
MAX_RETRIES="${MAX_RETRIES:-3}"
UPLOAD_IMAGE_FILE_SIZE_LIMIT="${UPLOAD_IMAGE_FILE_SIZE_LIMIT:-$((10 * 1024 * 1024))}"
COMPRESSED_IMAGE_MAX_LONG_SIDE="${COMPRESSED_IMAGE_MAX_LONG_SIDE:-4096}"

# Global mutable variables used by helper functions.
FILE_ID=""
DOC_ID=""
INDEX_STATUS=""
DISPLAY_STATUS=""

# --- Helpers ---
log() {
  echo "[run_batch] $*"
}

error() {
  echo "[run_batch] 오류: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || error "$1이(가) 설치되어 있지 않습니다."
}

require_env() {
  local name=$1
  local value=${!name:-}
  if [[ -z "${value}" ]]; then
    error "환경 변수 ${name}이(가) 설정되어 있어야 합니다."
  fi
}

# Validate dependencies and required environment variables.
require_command curl
require_command jq
require_command python3
require_command sha256sum

require_env DATASET_API_KEY
require_env DATASET_ID
require_env START_NODE_ID

AUTH_HEADER="Authorization: Bearer ${DATASET_API_KEY}"

# Resolve default sidecar/paths inside the image directory.
if [[ -z "${HASHES_FILE}" ]]; then
  HASHES_FILE="${IMAGE_DIR}/processed_hashes.jsonl"
fi
if [[ -z "${FAILED_LOG}" ]]; then
  FAILED_LOG="${IMAGE_DIR}/failed_$(date +%Y%m%d_%H%M%S).log"
fi

mkdir -p "${IMAGE_DIR}" "$(dirname "${HASHES_FILE}")" "$(dirname "${FAILED_LOG}")"
: > "${FAILED_LOG}"

if [[ ! -f "${HASHES_FILE}" ]]; then
  touch "${HASHES_FILE}"
fi

is_processed() {
  local hash=$1
  if [[ ! -f "${HASHES_FILE}" ]]; then
    return 1
  fi
  if jq -e --arg h "${hash}" 'select(.hash == $h)' "${HASHES_FILE}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

write_hash() {
  local hash=$1 filename=$2
  printf '{"hash":"%s","filename":"%s","completed_at":"%s"}\n' \
    "${hash}" "${filename}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${HASHES_FILE}"
}

compress_image() {
  local input_path=$1 output_path=$2
  python3 "${SCRIPT_DIR}/compress_image.py" \
    --max-long-side "${COMPRESSED_IMAGE_MAX_LONG_SIDE}" \
    --size-limit "${UPLOAD_IMAGE_FILE_SIZE_LIMIT}" \
    "${input_path}" "${output_path}"
}

upload_file() {
  local file_path=$1 original_name=$2
  local response

  if ! response=$(curl -fsS -X POST "${DIFY_API_BASE}/datasets/pipeline/file-upload" \
    -H "${AUTH_HEADER}" \
    -F "file=@${file_path};filename=${original_name}" 2>&1); then
    log "파일 업로드 요청 실패: ${response}"
    return 1
  fi

  local code
  code=$(jq -r '.code // empty' <<< "${response}")
  if [[ -n "${code}" && "${code}" != "0" && "${code}" != "null" ]]; then
    log "파일 업로드 API 오류: ${response}"
    return 1
  fi

  FILE_ID=$(jq -r '.id // .data.id // empty' <<< "${response}")
  if [[ -z "${FILE_ID}" || "${FILE_ID}" == "null" ]]; then
    log "파일 ID를 추출할 수 없습니다: ${response}"
    return 1
  fi

  return 0
}

run_pipeline() {
  local file_id=$1 file_name=$2
  local payload response code

  payload=$(jq -n \
    --arg start_node_id "${START_NODE_ID}" \
    --arg is_published "${IS_PUBLISHED}" \
    --arg response_mode "${RESPONSE_MODE}" \
    --arg reference "${file_id}" \
    --arg name "${file_name}" \
    '{
      inputs: {},
      datasource_type: "local_file",
      datasource_info_list: [{reference: $reference, name: $name}],
      start_node_id: $start_node_id,
      is_published: ($is_published | test("true")),
      response_mode: $response_mode
    }')

  if ! response=$(curl -fsS -X POST "${DIFY_API_BASE}/datasets/${DATASET_ID}/pipeline/run" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    -d "${payload}" 2>&1); then
    log "파이프라인 실행 요청 실패: ${response}"
    return 1
  fi

  code=$(jq -r '.code // empty' <<< "${response}")
  if [[ -n "${code}" && "${code}" != "0" && "${code}" != "null" ]]; then
    log "파이프라인 실행 API 오류: ${response}"
    return 1
  fi

  return 0
}

fetch_document_status() {
  local file_name=$1
  local list_response code

  DOC_ID=""
  INDEX_STATUS=""
  DISPLAY_STATUS=""

  if ! list_response=$(curl -fsS -G "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents" \
    -H "${AUTH_HEADER}" \
    --data-urlencode "keyword=${file_name}" \
    --data-urlencode "limit=10" 2>&1); then
    log "문서 목록 조회 요청 실패: ${list_response}"
    return 1
  fi

  code=$(jq -r '.code // empty' <<< "${list_response}")
  if [[ -n "${code}" && "${code}" != "0" && "${code}" != "null" ]]; then
    log "문서 목록 API 오류: ${list_response}"
    return 1
  fi

  DOC_ID=$(jq -r --arg name "${file_name}" '.data // [] | .[] | select(.name == $name) | .id' <<< "${list_response}" | head -n1)
  INDEX_STATUS=$(jq -r --arg name "${file_name}" '.data // [] | .[] | select(.name == $name) | .indexing_status' <<< "${list_response}" | head -n1)
  DISPLAY_STATUS=$(jq -r --arg name "${file_name}" '.data // [] | .[] | select(.name == $name) | .display_status' <<< "${list_response}" | head -n1)

  return 0
}

poll_indexing() {
  local file_name=$1
  local deadline

  deadline=$(($(date +%s) + POLL_TIMEOUT))

  log "인덱싱 상태 확인 시작 (최대 ${POLL_TIMEOUT}초)"
  while [[ "$(date +%s)" -lt "${deadline}" ]]; do
    if fetch_document_status "${file_name}"; then
      if [[ -n "${DOC_ID}" && "${DOC_ID}" != "null" ]]; then
        log "문서 ${DOC_ID} 상태: ${INDEX_STATUS} (표시: ${DISPLAY_STATUS})"

        case "${INDEX_STATUS}" in
          completed)
            return 0
            ;;
          error|failed)
            log "문서 ${DOC_ID} 인덱싱 실패"
            return 1
            ;;
        esac
      fi
    fi

    sleep "${POLL_INTERVAL}"
  done

  log "인덱싱 상태 확인 시간 초과 (${POLL_TIMEOUT}초)"
  return 1
}

delete_document() {
  local doc_id=$1
  if [[ -z "${doc_id}" || "${doc_id}" == "null" ]]; then
    return 0
  fi

  log "실패한 문서 ${doc_id} 삭제 중"
  curl -fsS -X DELETE "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents/${doc_id}" \
    -H "${AUTH_HEADER}" >/dev/null 2>&1 || true
}

process_image() {
  local file_path=$1
  local file_name hash compressed_path try

  file_name=$(basename "${file_path}")
  log "처리 시작: ${file_name}"

  hash=$(sha256sum "${file_path}" | awk '{print $1}')
  if is_processed "${hash}"; then
    log "건너뜀 (이미 처리됨): ${file_name}"
    return 0
  fi

  compressed_path=$(mktemp -t vlmocr.XXXXXX.jpg)

  log "이미지 압축 중: ${file_name}"
  if ! compress_image "${file_path}" "${compressed_path}"; then
    log "이미지 압축 실패: ${file_name}"
    rm -f "${compressed_path}"
    echo "${file_path}" >> "${FAILED_LOG}"
    return 1
  fi

  for ((try = 1; try <= MAX_RETRIES; try++)); do
    log "시도 ${try}/${MAX_RETRIES}: ${file_name}"

    FILE_ID=""
    DOC_ID=""
    INDEX_STATUS=""
    DISPLAY_STATUS=""

    if upload_file "${compressed_path}" "${file_name}" \
      && run_pipeline "${FILE_ID}" "${file_name}" \
      && poll_indexing "${file_name}"; then
      write_hash "${hash}" "${file_name}"
      log "완료: ${file_name}"
      rm -f "${compressed_path}"
      return 0
    fi

    delete_document "${DOC_ID}"

    if [[ "${try}" -lt "${MAX_RETRIES}" ]]; then
      log "5초 후 재시도합니다..."
      sleep 5
    fi
  done

  log "최대 재시도 횟수 초과: ${file_name}"
  rm -f "${compressed_path}"
  echo "${file_path}" >> "${FAILED_LOG}"
  return 1
}

# --- Main ---
log "이미지 디렉터리: ${IMAGE_DIR}"
log "상태 파일: ${HASHES_FILE}"
log "실패 로그: ${FAILED_LOG}"

shopt -s nullglob nocaseglob
image_files=("${IMAGE_DIR}"/*.{jpg,jpeg,png})
shopt -u nullglob nocaseglob

# Sort files for deterministic ordering.
IFS=$'\n' image_files=($(sort <<< "${image_files[*]}"))
unset IFS

if [[ ${#image_files[@]} -eq 0 ]]; then
  log "처리할 이미지가 없습니다: ${IMAGE_DIR}"
  exit 0
fi

total=${#image_files[@]}
success=0
failed=0

log "총 ${total}개의 이미지를 처리합니다."

for file_path in "${image_files[@]}"; do
  if process_image "${file_path}"; then
    success=$((success + 1))
  else
    failed=$((failed + 1))
  fi
  log "진행 상황: $((success + failed))/${total} (성공 ${success}, 실패 ${failed})"
done

log "배치 처리 완료. 총 ${total}개 중 성공 ${success}개, 실패 ${failed}개."
if [[ -s "${FAILED_LOG}" ]]; then
  log "실패한 파일 목록: ${FAILED_LOG}"
fi

if [[ "${failed}" -gt 0 ]]; then
  exit 1
fi
exit 0
