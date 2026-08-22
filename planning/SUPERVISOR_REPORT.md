# 수퍼바이저 동작 보고서 (SUPERVISOR_REPORT.md)

> 작성: 2026-08-02 15:16 KST

---

## 1. 설계 개요

### 문제

- 배치 러너가 Ollama Cloud 429로 중단 → 사람이 다시 시작해야 함
- Web 프로세스가 죽으면 502 → 사람이 다시 시작해야 함
- 메타데이터 루프가 중단 상태로 방치

### 해결

3개의 자동 복구 메커니즘:

1. **batch_supervisor.sh**: 배치를 감시하고 429 후 지수 백오프 재시도
2. **web_keepalive.sh**: Web 프로세스 상시 감시 및 자동 재기동
3. **inject_metadata_loop.sh 보완**: 삭제된 문서 만나도 죽지 않는 톨러런스

---

## 2. 대기 전략 숫자와 근거

| 매개변수           | 값             | 근거                                                                |
| ------------------ | -------------- | ------------------------------------------------------------------- |
| 초기 대기          | 30분 (1800초)  | 단시간 일시 초과는 30분 내 해제 가능성 있음                         |
| 백오프 배수        | 2x             | 표준 지수 백오프. 30→60→120분으로 3회에 3.5시간                     |
| 대기 상한          | 2시간 (7200초) | 15시간 회복 관찰 → 2시간 주기 프로빙이면 8회차(~최대 16시간)에 포착 |
| 조기 재시도 프로빙 | 10분마다       | 대기 중에도 쿼터 회복 즉시 감지                                     |
| 0진행 한계         | 5회 연속       | 5×(30분~2시간) = 최소 2.5시간~10시간 관찰 후 무의미 판단            |
| 디스크 가드        | 5GB            | PDF→JPEG 렌더링 + 임시 파일 오버헤드 고려                           |

### 대기 시퀀스 예시

```
시도1: 즉시 실행 → 429 중단
대기: 30분 (10분마다 프로빙)
시도2: 프로빙 성공 → 실행 → 429 중단
대기: 60분 (10분마다 프로빙)
시도3: 프로빙 성공 → 실행 → 정상 진행...
```

---

## 3. Ollama 프로빙 메커니즘

1. **1단계**: `GET /api/tags` — 서비스 자체 가용성 확인
2. **2단계**: `POST /api/generate` (num_predict=1) — 실제 쿼터 소비 최소화하면서 429 여부 확인

프로빙 성공 시에만 배치 러너를 시작하여 불필요한 실패를 방지합니다.

---

## 4. 테스트 결과

```
============================= test session starts ==============================
collected 20 items

tests/test_batch_supervisor.py::TestExponentialBackoff::test_initial_wait PASSED
tests/test_batch_supervisor.py::TestExponentialBackoff::test_backoff_doubles PASSED
tests/test_batch_supervisor.py::TestExponentialBackoff::test_backoff_doubles_again PASSED
tests/test_batch_supervisor.py::TestExponentialBackoff::test_cap_at_max_wait PASSED
tests/test_batch_supervisor.py::TestExponentialBackoff::test_reset_after_progress PASSED
tests/test_batch_supervisor.py::TestZeroProgressDetection::test_halt_after_max_zero_progress PASSED
tests/test_batch_supervisor.py::TestZeroProgressDetection::test_reset_on_progress PASSED
tests/test_batch_supervisor.py::TestZeroProgressDetection::test_single_zero_does_not_halt PASSED
tests/test_batch_supervisor.py::TestDiskGuard::test_enough_disk PASSED
tests/test_batch_supervisor.py::TestDiskGuard::test_exact_threshold PASSED
tests/test_batch_supervisor.py::TestDiskGuard::test_below_threshold PASSED
tests/test_batch_supervisor.py::TestDiskGuard::test_zero_disk PASSED
tests/test_batch_supervisor.py::TestBatchSupervisorScript::test_script_exists_and_executable PASSED
tests/test_batch_supervisor.py::TestBatchSupervisorScript::test_bash_syntax_check PASSED
tests/test_batch_supervisor.py::TestWebKeepaliveScript::test_script_exists_and_executable PASSED
tests/test_batch_supervisor.py::TestWebKeepaliveScript::test_bash_syntax_check PASSED
tests/test_batch_supervisor.py::TestInjectMetadataLoop::test_bash_syntax_check PASSED
tests/test_batch_supervisor.py::TestInjectMetadataLoop::test_has_error_tolerance PASSED
tests/test_batch_supervisor.py::TestDryRunSimulation::test_fake_429_scenario PASSED
tests/test_batch_supervisor.py::TestDryRunSimulation::test_recovery_after_progress PASSED

============================== 20 passed in 0.06s ==============================
```

---

## 5. 기동·중지·진행률 확인 명령

### 배치 수퍼바이저 기동 (다른 에이전트 작업 완료 후)

```bash
cd ~/Documents/proj/dify && nohup bash scripts/batch_supervisor.sh > scripts/batch_supervisor.log 2>&1 & disown; echo $! > scripts/batch_supervisor.pid
```

### 배치 수퍼바이저 중지

```bash
touch ~/Documents/proj/dify/scripts/STOP_BATCH_SUPERVISOR
```

### 진행률 확인

```bash
cat ~/Documents/proj/dify/scripts/batch_progress.txt
```

### Web Keepalive 기동

```bash
cd ~/Documents/proj/dify && nohup bash scripts/web_keepalive.sh > scripts/web_keepalive.log 2>&1 & disown; echo $! > scripts/web_keepalive.pid
```

### Web Keepalive 중지

```bash
touch ~/Documents/proj/dify/scripts/STOP_WEB_KEEPALIVE
```

---

## 6. Web Keepalive 동작 검증

```
[2026-08-02 15:15:36] Web Keepalive 시작 (PID=49223)
[2026-08-02 15:15:36]   감시 URL: http://localhost:3000/signin
[2026-08-02 15:15:36]   검사 주기: 30초
[2026-08-02 15:15:36]   플래핑 임계: 300초 내 3회
[2026-08-02 15:15:36] ✅ 초기 상태: Web 정상
```

- 초기 상태에서 Web 정상 응답 확인됨 (HTTP 200/302)
- 30초마다 자동 검사 진행 중
- 터미널 닫아도 nohup/disown으로 생존

---

## 7. 메타데이터 루프 재개 확인

```
[2026-08-02 15:15:53] 실행 완료.
[2026-08-02 15:15:53] 600초 대기...
```

- 928건 처리 완료, 오류 0건으로 정상 작동 중
- `set +e` 톨러런스 추가로 삭제된 문서 만나도 계속 실행

---

## 8. 한계 및 알려진 제약

1. **Ollama 프로빙의 쿼터 소비**: `/api/generate` 호출이 극소량이라도 쿼터를 사용할 수 있음. 하지만 `num_predict=1`이므로 실질적 영향 무시 가능.

2. **Web 재기동 시 빌드 필요**: `next start`는 `.next/` 빌드 산출물이 있어야 동작. 빌드가 깨지면 keepalive가 반복 실패 → 플래핑 경고 발생.

3. **호스트 재부팅 시 수동 재기동 필요**: launchd를 사용하지 않으므로 macOS 재부팅 시 모든 keepalive/supervisor를 수동으로 다시 기동해야 함.

4. **배치 수퍼바이저 미기동 상태**: 다른 에이전트의 문서 재조정 작업 완료를 확인한 후 기동해야 함. 현재는 스크립트만 생성됨.

5. **진행률 파일**: 수퍼바이저가 기동된 후에만 `batch_progress.txt`가 생성됨.

---

## 9. 산출물 목록

| 파일                              | 용도                                    |
| --------------------------------- | --------------------------------------- |
| `scripts/batch_supervisor.sh`     | 배치 수퍼바이저 (자동 재시도)           |
| `scripts/web_keepalive.sh`        | Web 프로세스 감시 및 자동 재기동        |
| `scripts/inject_metadata_loop.sh` | 보완됨 (예외 톨러런스)                  |
| `tests/test_batch_supervisor.py`  | 수퍼바이저 로직 단위 테스트 (20개 통과) |
| `planning/OPERATIONS.md`          | 운영 매뉴얼                             |
| `planning/SUPERVISOR_REPORT.md`   | 본 보고서                               |
