#!/bin/bash
# scripts/web_keepalive.sh
# ─────────────────────────────────────────────────────────────────
# Web 프로세스 keepalive: localhost:3000이 죽으면 자동 재기동
#
# 기동:
#   nohup bash scripts/web_keepalive.sh > scripts/web_keepalive.log 2>&1 &
#   disown
#   echo $! > scripts/web_keepalive.pid
#
# 중지:
#   touch scripts/STOP_WEB_KEEPALIVE
#   또는: kill $(cat scripts/web_keepalive.pid)
#
# 로그: scripts/web_keepalive.log
# ─────────────────────────────────────────────────────────────────

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="${PROJECT_DIR}/web"

STOP_FLAG="${SCRIPT_DIR}/STOP_WEB_KEEPALIVE"
PID_FILE="${SCRIPT_DIR}/web_keepalive.pid"
LOG_FILE="${SCRIPT_DIR}/web_keepalive.log"

CHECK_URL="http://localhost:3000/signin"
CHECK_INTERVAL=30       # 검사 주기 (초)
RESTART_COOLDOWN=10     # 재기동 후 안정화 대기 (초)
FLAP_WINDOW=300         # 플래핑 감지 윈도우 (초, 5분)
FLAP_THRESHOLD=3        # 윈도우 내 재기동 횟수 임계치

# 재기동 타임스탬프 배열
declare -a RESTART_TIMES=()

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

check_stop_flag() {
    if [ -f "$STOP_FLAG" ]; then
        log "STOP_WEB_KEEPALIVE 플래그 감지. 종료."
        rm -f "$STOP_FLAG"
        rm -f "$PID_FILE"
        exit 0
    fi
}

is_web_alive() {
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        --connect-timeout 5 --max-time 10 \
        "$CHECK_URL" 2>/dev/null || echo "000")
    
    # 200, 301, 302, 307, 308 모두 정상으로 간주
    if [[ "$http_code" =~ ^(200|301|302|307|308)$ ]]; then
        return 0
    fi
    return 1
}

restart_web() {
    log "🔄 Web 프로세스 재기동 중..."
    
    # 기존 next 프로세스 정리 (port 3000에 바인딩된 것)
    local existing_pid
    existing_pid=$(lsof -ti:3000 2>/dev/null || true)
    if [ -n "$existing_pid" ]; then
        log "  기존 프로세스 종료 (PID: $existing_pid)"
        kill "$existing_pid" 2>/dev/null || true
        sleep 2
        # 아직 살아있으면 강제 종료
        if kill -0 "$existing_pid" 2>/dev/null; then
            kill -9 "$existing_pid" 2>/dev/null || true
        fi
    fi
    
    # next start 기동 (nohup + background)
    cd "$WEB_DIR"
    nohup ./node_modules/.bin/next start -p 3000 >> "${SCRIPT_DIR}/web_next.log" 2>&1 &
    local next_pid=$!
    disown "$next_pid" 2>/dev/null || true
    
    log "  next start 기동됨 (PID: $next_pid)"
    
    # 안정화 대기
    sleep "$RESTART_COOLDOWN"
    
    # 기동 확인
    if is_web_alive; then
        log "  ✅ Web 재기동 성공 (HTTP 정상)"
    else
        log "  ⚠️ Web 재기동 후에도 응답 없음 - 다음 주기에 재시도"
    fi
    
    # 재기동 시간 기록
    RESTART_TIMES+=("$(date +%s)")
}

check_flapping() {
    local now
    now=$(date +%s)
    local window_start=$((now - FLAP_WINDOW))
    
    # 윈도우 밖 타임스탬프 제거
    local new_times=()
    for ts in "${RESTART_TIMES[@]:-}"; do
        if [ -n "$ts" ] && [ "$ts" -ge "$window_start" ]; then
            new_times+=("$ts")
        fi
    done
    RESTART_TIMES=("${new_times[@]:-}")
    
    local count=${#RESTART_TIMES[@]}
    if [ "$count" -ge "$FLAP_THRESHOLD" ]; then
        log "🚨 경고: ${FLAP_WINDOW}초 내 ${count}회 재기동 - 플래핑 감지!"
        log "   근본 원인 조사 필요. Web 프로세스가 반복적으로 죽고 있습니다."
        # 경고만 남기고 계속 모니터링 (완전 중단은 하지 않음)
        return 1
    fi
    return 0
}

# ── 메인 ──

echo $$ > "$PID_FILE"
rm -f "$STOP_FLAG"

log "═══════════════════════════════════════════════════════════"
log "Web Keepalive 시작 (PID=$$)"
log "  감시 URL: $CHECK_URL"
log "  검사 주기: ${CHECK_INTERVAL}초"
log "  플래핑 임계: ${FLAP_WINDOW}초 내 ${FLAP_THRESHOLD}회"
log "═══════════════════════════════════════════════════════════"

# 초기 상태 확인
if is_web_alive; then
    log "✅ 초기 상태: Web 정상"
else
    log "⚠️ 초기 상태: Web 응답 없음 - 즉시 재기동"
    restart_web
fi

while true; do
    check_stop_flag
    sleep "$CHECK_INTERVAL"
    check_stop_flag
    
    if ! is_web_alive; then
        log "❌ Web 응답 없음 - 재기동 시도"
        restart_web
        check_flapping || true
    fi
done
