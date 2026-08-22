#!/usr/bin/env bash
set -euo pipefail

# run_pipeline.sh
# Uploads an image file to a Dify knowledge pipeline and triggers a pipeline run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables if .env exists.
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  # shellcheck source=/dev/null
  set -a
  # shellcheck source=/dev/null
  source "${SCRIPT_DIR}/.env"
  set +a
fi

DIFY_API_BASE="${DIFY_API_BASE:-http://localhost/v1}"
DATASET_API_KEY="${DATASET_API_KEY:-}"
DATASET_ID="${DATASET_ID:-}"
START_NODE_ID="${START_NODE_ID:-}"
IS_PUBLISHED="${IS_PUBLISHED:-true}"
RESPONSE_MODE="${RESPONSE_MODE:-blocking}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"

log() {
  echo "[run_pipeline] $*"
}

error() {
  echo "[run_pipeline] ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || error "$1 is required but not installed."
}

require_command curl
require_command jq

if [[ $# -lt 1 ]]; then
  error "Usage: $0 <image-file-path>"
fi

FILE_PATH="$1"
if [[ ! -f "${FILE_PATH}" ]]; then
  error "File not found: ${FILE_PATH}"
fi

FILE_NAME="$(basename "${FILE_PATH}")"
FILE_EXT="${FILE_NAME##*.}"
FILE_EXT_LOWER="$(echo "${FILE_EXT}" | tr '[:upper:]' '[:lower:]')"
case "${FILE_EXT_LOWER}" in
  jpg|jpeg|png) ;;
  *) error "Only JPEG and PNG images are supported, got: ${FILE_EXT}" ;;
esac

if [[ -z "${DATASET_API_KEY}" ]]; then
  error "DATASET_API_KEY environment variable is required."
fi
if [[ -z "${DATASET_ID}" ]]; then
  error "DATASET_ID environment variable is required."
fi
if [[ -z "${START_NODE_ID}" ]]; then
  error "START_NODE_ID environment variable is required."
fi

AUTH_HEADER="Authorization: Bearer ${DATASET_API_KEY}"

# Upload the image to the pipeline file upload endpoint.
log "Uploading ${FILE_NAME} to ${DIFY_API_BASE}/datasets/pipeline/file-upload"
UPLOAD_RESPONSE="$(curl -fsS -X POST "${DIFY_API_BASE}/datasets/pipeline/file-upload" \
  -H "${AUTH_HEADER}" \
  -F "file=@${FILE_PATH}" 2>&1)"

UPLOAD_CODE="$(echo "${UPLOAD_RESPONSE}" | jq -r '.code // empty')"
if [[ -n "${UPLOAD_CODE}" && "${UPLOAD_CODE}" != "0" && "${UPLOAD_CODE}" != "null" ]]; then
  error "File upload failed: ${UPLOAD_RESPONSE}"
fi

FILE_ID="$(echo "${UPLOAD_RESPONSE}" | jq -r '.id // .data.id // empty')"
if [[ -z "${FILE_ID}" || "${FILE_ID}" == "null" ]]; then
  error "Could not extract file ID from upload response: ${UPLOAD_RESPONSE}"
fi
log "Uploaded file ID: ${FILE_ID}"

# Build the pipeline run payload.
RUN_PAYLOAD="$(jq -n \
  --arg start_node_id "${START_NODE_ID}" \
  --arg is_published "${IS_PUBLISHED}" \
  --arg response_mode "${RESPONSE_MODE}" \
  --arg reference "${FILE_ID}" \
  --arg name "${FILE_NAME}" \
  '{
    inputs: {},
    datasource_type: "local_file",
    datasource_info_list: [{reference: $reference, name: $name}],
    start_node_id: $start_node_id,
    is_published: ($is_published | test("true")),
    response_mode: $response_mode
  }')"

log "Running pipeline for dataset ${DATASET_ID}"
if [[ "${RESPONSE_MODE}" == "streaming" ]]; then
  curl -fsS -X POST "${DIFY_API_BASE}/datasets/${DATASET_ID}/pipeline/run" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    -d "${RUN_PAYLOAD}"
  echo
  log "Streaming response printed above."
else
  RUN_RESPONSE="$(curl -fsS -X POST "${DIFY_API_BASE}/datasets/${DATASET_ID}/pipeline/run" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    -d "${RUN_PAYLOAD}" 2>&1)"

  RUN_CODE="$(echo "${RUN_RESPONSE}" | jq -r '.code // empty')"
  if [[ -n "${RUN_CODE}" && "${RUN_CODE}" != "0" && "${RUN_CODE}" != "null" ]]; then
    error "Pipeline run failed: ${RUN_RESPONSE}"
  fi

  log "Pipeline run response:"
  echo "${RUN_RESPONSE}" | jq .
fi

# Poll document indexing status until completed or failed.
log "Polling document indexing status (timeout ${POLL_TIMEOUT}s)"
DEADLINE="$(($(date +%s) + POLL_TIMEOUT))"
while [[ "$(date +%s)" -lt "${DEADLINE}" ]]; do
  LIST_RESPONSE="$(curl -fsS -G "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents" \
    -H "${AUTH_HEADER}" \
    --data-urlencode "keyword=${FILE_NAME}" \
    --data-urlencode "limit=10" 2>&1)"

  LIST_CODE="$(echo "${LIST_RESPONSE}" | jq -r '.code // empty')"
  if [[ -n "${LIST_CODE}" && "${LIST_CODE}" != "0" && "${LIST_CODE}" != "null" ]]; then
    error "Failed to list documents: ${LIST_RESPONSE}"
  fi

  DOC_ID="$(echo "${LIST_RESPONSE}" | jq -r --arg name "${FILE_NAME}" '.data // [] | .[] | select(.name == $name) | .id' | head -n1)"
  INDEX_STATUS="$(echo "${LIST_RESPONSE}" | jq -r --arg name "${FILE_NAME}" '.data // [] | .[] | select(.name == $name) | .indexing_status' | head -n1)"
  DISPLAY_STATUS="$(echo "${LIST_RESPONSE}" | jq -r --arg name "${FILE_NAME}" '.data // [] | .[] | select(.name == $name) | .display_status' | head -n1)"

  if [[ -n "${DOC_ID}" && "${DOC_ID}" != "null" ]]; then
    log "Document ${DOC_ID} status: ${INDEX_STATUS} (display: ${DISPLAY_STATUS})"

    case "${INDEX_STATUS}" in
      completed)
        log "Indexing completed."
        exit 0
        ;;
      error|failed)
        error "Indexing failed for document ${DOC_ID}."
        ;;
    esac
  fi

  sleep "${POLL_INTERVAL}"
done

error "Timed out waiting for indexing to complete."
