#!/bin/bash
# scripts/inject_metadata_loop.sh
# 10분마다 inject_metadata.py를 실행하여 신규 문서에 메타데이터 자동 주입
#
# 기동:
#   nohup bash scripts/inject_metadata_loop.sh > scripts/inject_metadata_loop.log 2>&1 &
#   echo $! > scripts/inject_metadata_loop.pid
#
# 중지:
#   kill $(cat scripts/inject_metadata_loop.pid)
#   또는: touch scripts/STOP_METADATA_LOOP  (graceful stop)
#
# 로그: scripts/inject_metadata_loop.log

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STOP_FLAG="${SCRIPT_DIR}/STOP_METADATA_LOOP"
PID_FILE="${SCRIPT_DIR}/inject_metadata_loop.pid"
INTERVAL=600  # 10분 (초)

# PID 파일 기록
echo $$ > "$PID_FILE"

echo "[$(date)] 메타데이터 자동 주입 루프 시작 (PID=$$, 간격=${INTERVAL}초)"

# 이전 STOP 플래그 제거
rm -f "$STOP_FLAG"

while true; do
    # 정지 플래그 체크
    if [ -f "$STOP_FLAG" ]; then
        echo "[$(date)] STOP_METADATA_LOOP 플래그 감지. 루프 종료."
        rm -f "$STOP_FLAG"
        rm -f "$PID_FILE"
        exit 0
    fi

    echo "[$(date)] inject_metadata.py 실행 중..."
    # 예외 톨러런스: 삭제된 문서 등으로 인한 오류를 무시하고 계속 실행
    set +e
    python3 "${SCRIPT_DIR}/inject_metadata.py" 2>&1
    exit_code=$?
    set -e
    if [ $exit_code -ne 0 ]; then
        echo "[$(date)] ⚠️ inject_metadata.py 비정상 종료 (exit=$exit_code). 다음 주기에 재시도."
    else
        echo "[$(date)] 실행 완료."
    fi
    echo "[$(date)] ${INTERVAL}초 대기..."
    
    # 대기 중에도 정지 플래그 체크 (10초 간격)
    for ((i=0; i<INTERVAL; i+=10)); do
        if [ -f "$STOP_FLAG" ]; then
            echo "[$(date)] STOP_METADATA_LOOP 플래그 감지. 루프 종료."
            rm -f "$STOP_FLAG"
            rm -f "$PID_FILE"
            exit 0
        fi
        sleep 10
    done
done
