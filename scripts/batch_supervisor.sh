#!/bin/bash
# scripts/batch_supervisor.sh
# ─────────────────────────────────────────────────────────────────
# 배치 수퍼바이저: run_batch_parallel.py를 감시하고 429/쿼터 소진 후
# 지수적 백오프로 자동 재시도합니다.
#
# 대기 전략 근거:
#   - Ollama Cloud 무료 티어 쿼터 회복 관찰치: ~15시간
#   - 초기 대기 30분 → 60분 → 120분 (2배 증가, 상한 120분)
#   - 30+60+120 = 210분(3.5시간)이면 3회 시도. 회복 전에 소진되지 않으며
#     쿼터 회복 시점에 근접하면 프로빙이 성공하여 즉시 재시도.
#   - 연속 0-페이지 완료 횟수 5회이면 무의미한 반복 중단.
#
# 기동:
#   nohup bash scripts/batch_supervisor.sh > scripts/batch_supervisor.log 2>&1 &
#   echo $! > scripts/batch_supervisor.pid
#
# 중지:
#   touch scripts/STOP_BATCH_SUPERVISOR
#   또는: kill $(cat scripts/batch_supervisor.pid)
#
# 진행률 확인:
#   cat scripts/batch_progress.txt
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BATCH_RUNNER="${PROJECT_DIR}/docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts/run_batch_parallel.py"
STATE_DIR="${PROJECT_DIR}/docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts/batch_state"
PROCESSED_FILE="${STATE_DIR}/processed_pages.jsonl"

STOP_FLAG="${SCRIPT_DIR}/STOP_BATCH_SUPERVISOR"
PID_FILE="${SCRIPT_DIR}/batch_supervisor.pid"
PROGRESS_FILE="${SCRIPT_DIR}/batch_progress.txt"
LOG_FILE="${SCRIPT_DIR}/batch_supervisor.log"

# ── 설정 ──
INITIAL_WAIT=1800      # 30분 (초)
MAX_WAIT=7200          # 2시간 (초)
BACKOFF_FACTOR=2       # 지수 백오프 배수
MAX_ZERO_PROGRESS=5    # 연속 0페이지 진행 시 중단
TOTAL_PAGES=27754      # 전체 페이지 수
MIN_DISK_FREE_GB=5     # 최소 디스크 여유 (GB)

# Ollama 프로빙 설정
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:cloud}"
PROBE_TIMEOUT=30       # 프로빙 타임아웃 (초)

# ── 함수 ──

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_stop_flag() {
    if [ -f "$STOP_FLAG" ]; then
        log "STOP_BATCH_SUPERVISOR 플래그 감지. 종료."
        rm -f "$STOP_FLAG"
        rm -f "$PID_FILE"
        exit 0
    fi
}

check_disk() {
    local free_gb
    free_gb=$(df -g / | awk 'NR==2 {print $4}')
    if [ "$free_gb" -lt "$MIN_DISK_FREE_GB" ]; then
        log "❌ 디스크 여유 ${free_gb}GB < ${MIN_DISK_FREE_GB}GB. 시작 불가. 종료."
        update_progress "STOPPED" "디스크 여유 부족 (${free_gb}GB)"
        exit 1
    fi
}

get_processed_count() {
    if [ -f "$PROCESSED_FILE" ]; then
        wc -l < "$PROCESSED_FILE" | tr -d ' '
    else
        echo "0"
    fi
}

probe_ollama() {
    # Ollama에 가벼운 /api/tags 호출로 서비스 가용성 확인
    # 실패하면 /api/generate로 짧은 요청 시도
    log "🔍 Ollama 프로빙 중... (${OLLAMA_BASE_URL})"
    
    # 1단계: /api/tags로 서비스 자체 확인
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 10 --max-time "$PROBE_TIMEOUT" \
        "${OLLAMA_BASE_URL}/api/tags" 2>/dev/null || echo "000")
    
    if [ "$http_code" = "000" ] || [ "$http_code" -ge 500 ]; then
        log "⚠️ Ollama 서비스 접근 불가 (HTTP $http_code)"
        return 1
    fi
    
    # 2단계: 짧은 generate 호출로 쿼터 확인 (429 감지)
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 10 --max-time "$PROBE_TIMEOUT" \
        -X POST "${OLLAMA_BASE_URL}/api/generate" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"${OLLAMA_MODEL}\",\"prompt\":\"hi\",\"stream\":false,\"options\":{\"num_predict\":1}}" \
        2>/dev/null || echo "000")
    
    if [ "$http_code" = "429" ]; then
        log "⚠️ Ollama 429 - 쿼터 아직 미회복"
        return 1
    elif [ "$http_code" = "000" ] || [ "$http_code" -ge 500 ]; then
        log "⚠️ Ollama 프로빙 실패 (HTTP $http_code)"
        return 1
    fi
    
    log "✅ Ollama 프로빙 성공 (HTTP $http_code) - 쿼터 사용 가능"
    return 0
}

update_progress() {
    local status="$1"
    local detail="${2:-}"
    local processed
    processed=$(get_processed_count)
    local elapsed_sec=$(($(date +%s) - START_EPOCH))
    local elapsed_h=$((elapsed_sec / 3600))
    local elapsed_m=$(((elapsed_sec % 3600) / 60))
    
    local eta="계산 불가"
    if [ "$processed" -gt "$INITIAL_PROCESSED" ] && [ "$processed" -gt 0 ]; then
        local done_this_session=$((processed - INITIAL_PROCESSED))
        if [ "$done_this_session" -gt 0 ]; then
            local sec_per_page=$((elapsed_sec / done_this_session))
            local remaining_sec=$(((TOTAL_PAGES - processed) * sec_per_page))
            local eta_h=$((remaining_sec / 3600))
            local eta_m=$(((remaining_sec % 3600) / 60))
            eta="${eta_h}h ${eta_m}m"
        fi
    fi
    
    printf "[%s] %s | 처리됨: %d/%d (%.1f%%) | 경과: %dh%dm | ETA: %s | %s\n" \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$status" \
        "$processed" "$TOTAL_PAGES" \
        "$(echo "scale=1; $processed * 100 / $TOTAL_PAGES" | bc)" \
        "$elapsed_h" "$elapsed_m" \
        "$eta" \
        "$detail" > "$PROGRESS_FILE"
}

wait_with_check() {
    # 지정 시간 대기하되, 60초마다 stop 플래그 확인
    local wait_sec=$1
    local waited=0
    log "⏳ ${wait_sec}초 ($(( wait_sec / 60 ))분) 대기..."
    while [ $waited -lt $wait_sec ]; do
        check_stop_flag
        sleep 60
        waited=$((waited + 60))
        # 매 10분마다 프로빙 시도 (조기 재시작 가능)
        if [ $((waited % 600)) -eq 0 ] && [ $waited -lt $wait_sec ]; then
            if probe_ollama; then
                log "🎉 대기 중 프로빙 성공! 조기 재시도."
                return 0
            fi
        fi
    done
}

# ── 메인 로직 ──

# PID 기록
echo $$ > "$PID_FILE"
rm -f "$STOP_FLAG"

START_EPOCH=$(date +%s)
INITIAL_PROCESSED=$(get_processed_count)

log "═══════════════════════════════════════════════════════════"
log "배치 수퍼바이저 시작 (PID=$$)"
log "  배치 러너: $BATCH_RUNNER"
log "  초기 대기: ${INITIAL_WAIT}초, 상한: ${MAX_WAIT}초"
log "  백오프 배수: ${BACKOFF_FACTOR}x"
log "  연속 0진행 한계: ${MAX_ZERO_PROGRESS}회"
log "  시작 시점 처리됨: ${INITIAL_PROCESSED}/${TOTAL_PAGES}"
log "═══════════════════════════════════════════════════════════"

# 디스크 확인
check_disk

current_wait=$INITIAL_WAIT
zero_progress_count=0
attempt=0

while true; do
    check_stop_flag
    
    attempt=$((attempt + 1))
    log "──── 시도 #${attempt} ────"
    
    # 재시도 전 디스크 확인
    check_disk
    
    # 프로빙으로 쿼터 확인 (첫 시도는 건너뜀)
    if [ $attempt -gt 1 ]; then
        if ! probe_ollama; then
            log "프로빙 실패. ${current_wait}초 추가 대기 후 재시도."
            update_progress "WAITING" "Ollama 쿼터 미회복, 대기 중"
            wait_with_check "$current_wait"
            # 백오프 증가
            current_wait=$((current_wait * BACKOFF_FACTOR))
            if [ $current_wait -gt $MAX_WAIT ]; then
                current_wait=$MAX_WAIT
            fi
            continue
        fi
    fi
    
    # 시작 전 페이지 수 기록
    local_before=$(get_processed_count)
    
    # 배치 러너 실행
    log "🚀 배치 러너 실행 중..."
    update_progress "RUNNING" "시도 #${attempt}"
    
    # 배치 러너는 429로 중단되면 non-zero exit
    set +e
    python3 "$BATCH_RUNNER" 2>&1 | tee -a "${SCRIPT_DIR}/batch_runner_output.log"
    exit_code=$?
    set -e
    
    local_after=$(get_processed_count)
    pages_done=$((local_after - local_before))
    
    log "배치 러너 종료 (exit=$exit_code, 이번 세션 처리: ${pages_done}페이지)"
    
    # 정상 종료 (모든 페이지 완료)
    # exit=0 이라도 전체 페이지를 끝낸 것이 아니면 완료가 아니다.
    # 배치 러너는 연속 실패로 조기 중단해도 exit=0 을 돌려주므로
    # 반드시 처리량으로 완료를 판정해야 한다. (2026-08-02 수하자 조기 종료 사고)
    if [ $exit_code -eq 0 ] && [ "$local_after" -ge "$TOTAL_PAGES" ]; then
        log "✅ 배치 러너 정상 완료! (${local_after}/${TOTAL_PAGES})"
        update_progress "COMPLETED" "전체 처리 완료"
        rm -f "$PID_FILE"
        exit 0
    fi

    if [ $exit_code -eq 0 ]; then
        log "⚠️ exit=0 이지만 미완료 (${local_after}/${TOTAL_PAGES}) - 조기 중단으로 간주하고 재개 대기"
    fi
    
    # 진행 있었는지 확인
    if [ "$pages_done" -gt 0 ]; then
        zero_progress_count=0
        current_wait=$INITIAL_WAIT  # 진행이 있으면 대기 리셋
        log "✅ ${pages_done}페이지 진행 확인. 대기 리셋."
    else
        zero_progress_count=$((zero_progress_count + 1))
        log "⚠️ 0페이지 진행 (연속 ${zero_progress_count}/${MAX_ZERO_PROGRESS})"
        
        if [ $zero_progress_count -ge $MAX_ZERO_PROGRESS ]; then
            log "❌ 연속 ${MAX_ZERO_PROGRESS}회 0진행. 무의미한 반복 중단."
            update_progress "HALTED" "연속 ${MAX_ZERO_PROGRESS}회 0진행으로 중단"
            rm -f "$PID_FILE"
            exit 2
        fi
    fi
    
    update_progress "WAITING" "429/쿼터 소진, ${current_wait}초 후 재시도 예정"
    
    # 대기
    wait_with_check "$current_wait"
    
    # 백오프 증가
    current_wait=$((current_wait * BACKOFF_FACTOR))
    if [ $current_wait -gt $MAX_WAIT ]; then
        current_wait=$MAX_WAIT
    fi
done
