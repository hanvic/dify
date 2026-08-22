# 운영 매뉴얼 (OPERATIONS.md)

> 최종 갱신: 2026-08-02 15:15 KST

## 현재 상태 요약

| 구성요소             | 상태                        | 비고                                                      |
| -------------------- | --------------------------- | --------------------------------------------------------- |
| Web (localhost:3000) | ✅ 정상                     | keepalive 감시 중                                         |
| 메타데이터 주입 루프 | ✅ 가동 중                  | 10분 주기, 928건 완료                                     |
| 배치 수퍼바이저      | ⏸️ 대기                     | 스크립트 준비됨, 미기동 (다른 에이전트 작업 완료 후 기동) |
| 배치 러너            | ⏸️ 대기                     | 문서 재조정 작업 완료 후 수퍼바이저가 관리                |
| 전체 진행            | ~928 / 27,754 페이지 (3.3%) |                                                           |

---

## 1. Web Keepalive

### 개요

`scripts/web_keepalive.sh`가 30초마다 `http://localhost:3000/signin`을 확인하고, 응답이 없으면 `next start`를 자동 재기동합니다.

### 기동

```bash
cd ~/Documents/proj/dify
nohup bash scripts/web_keepalive.sh > scripts/web_keepalive.log 2>&1 &
disown
echo $! > scripts/web_keepalive.pid
```

### 중지

```bash
touch scripts/STOP_WEB_KEEPALIVE
# 또는
kill $(cat scripts/web_keepalive.pid)
```

### 상태 확인

```bash
tail -20 scripts/web_keepalive.log
```

### 플래핑 경고

5분 내 3회 이상 재기동 시 로그에 🚨 경고가 남습니다. 근본 원인(메모리 부족, 빌드 오류 등)을 확인해야 합니다.

---

## 2. 배치 수퍼바이저

### 개요

`scripts/batch_supervisor.sh`가 `run_batch_parallel.py`를 실행하고, 429/쿼터 소진으로 중단되면 자동 재시도합니다.

### 대기 전략

- 초기 대기: 30분
- 지수 백오프: 2배 (30분 → 60분 → 120분)
- 상한: 2시간
- 대기 중 10분마다 Ollama 프로빙 → 성공 시 조기 재시도
- 연속 5회 0진행이면 중단

### 기동 (다른 에이전트 작업 완료 확인 후)

```bash
cd ~/Documents/proj/dify
nohup bash scripts/batch_supervisor.sh > scripts/batch_supervisor.log 2>&1 &
disown
echo $! > scripts/batch_supervisor.pid
```

### 중지

```bash
touch scripts/STOP_BATCH_SUPERVISOR
# 또는
kill $(cat scripts/batch_supervisor.pid)
```

### 진행률 확인

```bash
cat scripts/batch_progress.txt
```

### 상세 로그

```bash
tail -50 scripts/batch_supervisor.log
```

---

## 3. 메타데이터 주입 루프

### 개요

`scripts/inject_metadata_loop.sh`가 10분마다 `inject_metadata.py`를 실행하여 신규 문서에 메타데이터를 주입합니다. 삭제된 문서를 만나도 죽지 않고 계속합니다.

### 기동

```bash
cd ~/Documents/proj/dify
nohup bash scripts/inject_metadata_loop.sh > scripts/inject_metadata_loop.log 2>&1 &
disown
echo $! > scripts/inject_metadata_loop.pid
```

### 중지

```bash
touch scripts/STOP_METADATA_LOOP
# 또는
kill $(cat scripts/inject_metadata_loop.pid)
```

### 상태 확인

```bash
tail -20 scripts/inject_metadata_loop.log
```

---

## 4. 장애 시나리오별 대응

### Web이 502일 때

1. keepalive가 자동 재기동합니다.
2. 플래핑이 발생하면 로그를 확인하세요.
3. 수동 확인: `curl -sI http://localhost:3000/signin`

### 배치가 429로 멈췄을 때

1. 수퍼바이저가 자동으로 프로빙 → 대기 → 재시도합니다.
2. 진행률: `cat scripts/batch_progress.txt`
3. 수동 프로빙: `curl -s http://localhost:11434/api/tags`

### 디스크 부족

- 수퍼바이저와 배치 러너 모두 5GB 미만이면 자동 중단합니다.
- 확인: `df -h /`

---

## 5. nginx 구성

- `docker/nginx/conf.d/default.conf`: `host.docker.internal:3000` 을 프록시 (호스트의 next start)
- 원본 백업: `docker/nginx/conf.d/default.conf.original`

---

## 6. 환경 변수

| 변수            | 위치                  | 용도                   |
| --------------- | --------------------- | ---------------------- |
| DATASET_API_KEY | `scripts/.env`        | Dify 데이터셋 API 인증 |
| DIFY_DATASET_ID | `scripts/.env`        | 대상 데이터셋 ID       |
| OLLAMA_BASE_URL | 기본: localhost:11434 | Ollama API 엔드포인트  |
| CONCURRENCY     | 기본: 6               | 배치 동시성            |
