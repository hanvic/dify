#!/usr/bin/env bash
set -euo pipefail

# install_plugin.sh
# Packages the VLM OCR plugin inside the Dify plugin daemon container and
# installs it through the plugin daemon management API.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load environment variables if .env exists.
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  # shellcheck source=/dev/null
  set -a
  # shellcheck source=/dev/null
  source "${SCRIPT_DIR}/.env"
  set +a
fi

PLUGIN_DAEMON_URL="${PLUGIN_DAEMON_URL:-http://host.docker.internal:5002}"
PLUGIN_DAEMON_KEY="${PLUGIN_DAEMON_KEY:-}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
DB_HOST="${DB_HOST:-db_postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USERNAME="${DB_USERNAME:-postgres}"
DB_PASSWORD="${DB_PASSWORD:-difyai123456}"
DB_DATABASE="${DB_DATABASE:-dify}"

CONTAINER_NAME="plugin_daemon"
DB_SERVICE_NAME="db_postgres"
TMP_CONTAINER_DIR="/tmp/vlm_ocr_plugin"
PKG_NAME="vlm_ocr_plugin.difypkg"

log() {
  echo "[install_plugin] $*"
}

error() {
  echo "[install_plugin] ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || error "$1 is required but not installed."
}

require_command docker
require_command curl
require_command jq

# Determine docker compose command.
if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker compose)
elif docker-compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE=(docker-compose)
else
  error "docker compose or docker-compose is required."
fi

# Optionally scope the compose project.
if [[ -n "${COMPOSE_PROJECT_NAME}" ]]; then
  DOCKER_COMPOSE+=(-p "${COMPOSE_PROJECT_NAME}")
fi

# Verify the plugin daemon container is running.
if ! "${DOCKER_COMPOSE[@]}" ps -q "${CONTAINER_NAME}" | grep -q .; then
  error "Plugin daemon container '${CONTAINER_NAME}' is not running. Start Dify first."
fi

# Resolve tenant ID from the main application database.
resolve_tenant_id() {
  local tenant_id
  tenant_id="$("${DOCKER_COMPOSE[@]}" exec -T "${DB_SERVICE_NAME}" psql -U "${DB_USERNAME}" -d "${DB_DATABASE}" -tA -c "SELECT id FROM tenants ORDER BY created_at ASC LIMIT 1;" 2>/dev/null | tr -d '[:space:]')"
  if [[ -z "${tenant_id}" ]]; then
    error "Could not resolve a tenant ID from ${DB_DATABASE}.tenants. Check DB credentials."
  fi
  echo "${tenant_id}"
}

TENANT_ID="${TENANT_ID:-$(resolve_tenant_id)}"
log "Using tenant ID: ${TENANT_ID}"

# Remove stale temporary directory inside the container and copy the source.
log "Copying plugin source to container ${CONTAINER_NAME}:${TMP_CONTAINER_DIR}"
"${DOCKER_COMPOSE[@]}" exec -T "${CONTAINER_NAME}" rm -rf "${TMP_CONTAINER_DIR}" || true
"${DOCKER_COMPOSE[@]}" cp "${PLUGIN_ROOT}/." "${CONTAINER_NAME}:${TMP_CONTAINER_DIR}/"

# Clean up development artifacts that the packager may not exclude via .difyignore.
log "Cleaning up development artifacts before packaging"
"${DOCKER_COMPOSE[@]}" exec -T "${CONTAINER_NAME}" bash -c "
  cd '${TMP_CONTAINER_DIR}' || exit 1
  rm -rf scripts __pycache__ .git .DS_Store
  rm -f .env .env.*
  rm -f DECISIONS.md PROMPT_DESIGN.md PIPELINE_SETUP_GUIDE.md
  rm -f *.difypkg
  find . -type f \( -name '*.pyc' -o -name '*.pem' -o -name '*.key' -o -name '*.crt' \) -delete
  find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
"

# Package the plugin inside the container.
log "Running plugin packager"
"${DOCKER_COMPOSE[@]}" exec -T --workdir /tmp "${CONTAINER_NAME}" /app/commandline plugin package "${TMP_CONTAINER_DIR}"

# The packager writes the .difypkg in the working directory using the source directory name.
PKG_CONTAINER_PATH="/tmp/${PKG_NAME}"

# Verify the package does not contain leftover development artifacts.
log "Verifying package contents"
"${DOCKER_COMPOSE[@]}" exec -T "${CONTAINER_NAME}" bash -c "
  cd '${TMP_CONTAINER_DIR}' || exit 1
  if ! unzip -l '${PKG_NAME}' | grep -qE 'scripts/|DECISIONS\.md|PROMPT_DESIGN\.md|PIPELINE_SETUP_GUIDE\.md'; then
    echo '[install_plugin] Package verification passed: no scripts/ or development docs found.'
  else
    echo '[install_plugin] ERROR: Package still contains excluded development artifacts.' >&2
    unzip -l '${PKG_NAME}'
    exit 1
  fi
"

# Copy the packaged file back to the host.
PKG_HOST_PATH="${PLUGIN_ROOT}/${PKG_NAME}"
log "Copying package to host: ${PKG_HOST_PATH}"
"${DOCKER_COMPOSE[@]}" cp "${CONTAINER_NAME}:${PKG_CONTAINER_PATH}" "${PKG_HOST_PATH}"

# Clean up the temporary directory inside the container.
"${DOCKER_COMPOSE[@]}" exec -T "${CONTAINER_NAME}" rm -rf "${TMP_CONTAINER_DIR}" || true

# Upload the package to the plugin daemon management API.
log "Uploading package to plugin daemon (${PLUGIN_DAEMON_URL})"
UPLOAD_RESPONSE="$(curl -fsS -X POST "${PLUGIN_DAEMON_URL}/plugin/${TENANT_ID}/management/install/upload/package" \
  ${PLUGIN_DAEMON_KEY:+-H "X-Api-Key: ${PLUGIN_DAEMON_KEY}"} \
  -F "verify_signature=false" \
  -F "dify_pkg=@${PKG_HOST_PATH};type=application/octet-stream" 2>&1)"

UPLOAD_CODE="$(echo "${UPLOAD_RESPONSE}" | jq -r '.code // empty')"
if [[ "${UPLOAD_CODE}" != "0" && "${UPLOAD_CODE}" != "" ]]; then
  error "Package upload failed: ${UPLOAD_RESPONSE}"
fi

PLUGIN_IDENTIFIER="$(echo "${UPLOAD_RESPONSE}" | jq -r '.data.unique_identifier')"
if [[ -z "${PLUGIN_IDENTIFIER}" || "${PLUGIN_IDENTIFIER}" == "null" ]]; then
  error "Could not extract plugin unique identifier from upload response: ${UPLOAD_RESPONSE}"
fi
log "Uploaded package identifier: ${PLUGIN_IDENTIFIER}"

# Install the plugin from the identifier.
INSTALL_PAYLOAD="$(jq -n \
  --arg id "${PLUGIN_IDENTIFIER}" \
  --arg source "Package" \
  '{plugin_unique_identifiers:[$id],source:$source,metas:[{plugin_unique_identifier:$id}]}')"

log "Starting plugin installation"
INSTALL_RESPONSE="$(curl -fsS -X POST "${PLUGIN_DAEMON_URL}/plugin/${TENANT_ID}/management/install/identifiers" \
  ${PLUGIN_DAEMON_KEY:+-H "X-Api-Key: ${PLUGIN_DAEMON_KEY}"} \
  -H "Content-Type: application/json" \
  -d "${INSTALL_PAYLOAD}" 2>&1)"

INSTALL_CODE="$(echo "${INSTALL_RESPONSE}" | jq -r '.code // empty')"
if [[ "${INSTALL_CODE}" != "0" && "${INSTALL_CODE}" != "" ]]; then
  error "Plugin install request failed: ${INSTALL_RESPONSE}"
fi

TASK_ID="$(echo "${INSTALL_RESPONSE}" | jq -r '.data.task_id')"
if [[ -z "${TASK_ID}" || "${TASK_ID}" == "null" ]]; then
  error "Could not extract install task ID: ${INSTALL_RESPONSE}"
fi
log "Install task ID: ${TASK_ID}"

# Poll the install task until it completes or fails.
MAX_ATTEMPTS=60
SLEEP_SECONDS=5
for ((i = 1; i <= MAX_ATTEMPTS; i++)); do
  TASK_RESPONSE="$(curl -fsS "${PLUGIN_DAEMON_URL}/plugin/${TENANT_ID}/management/install/tasks/${TASK_ID}" \
    ${PLUGIN_DAEMON_KEY:+-H "X-Api-Key: ${PLUGIN_DAEMON_KEY}"} 2>&1)"

  TASK_CODE="$(echo "${TASK_RESPONSE}" | jq -r '.code // empty')"
  if [[ "${TASK_CODE}" != "0" && "${TASK_CODE}" != "" ]]; then
    error "Failed to fetch install task status: ${TASK_RESPONSE}"
  fi

  TASK_STATUS="$(echo "${TASK_RESPONSE}" | jq -r '.data.status')"
  log "Install task status: ${TASK_STATUS}"

  case "${TASK_STATUS}" in
    success)
      log "Plugin installed successfully."
      echo "${TASK_RESPONSE}" | jq .
      exit 0
      ;;
    failed)
      error "Plugin installation failed: ${TASK_RESPONSE}"
      ;;
  esac

  sleep "${SLEEP_SECONDS}"
done

error "Timed out waiting for plugin installation to complete."
