# 운영 안내 — 희망브리지 문서전자화 RAG 챗봇

작성: 2026-07-31. 이 문서는 **지금 무엇이 돌아가고 있고, 어떻게 멈추고 재개하는지**만 담습니다.
설계 배경은 `planning/STATE_BRIEF.md`, `PLAN.md`, `REVIEW.md`를 보세요.

---

## 1. 지금 돌아가는 것

**배치는 중단된 상태입니다.** 2026-07-31 08:30 시점, Ollama Cloud 쿼터 초과로 더 진행할 수 없습니다.

| # | 프로세스 | 상태 |
|---|---|---|
| 1 | 페이지 OCR 배치 (`run_batch_parallel.py`) | **중단됨** — 재개는 쿼터 해결 후 |
| 2 | 메타데이터 자동 주입 루프 | **중단됨** |
| 3 | web 프론트 (호스트 `next start` + nginx 프록시) | 실행 중 |

### 2026-07-31 사고 기록 — 오류 문자열 13,566건 임베딩

Ollama Cloud 무료 티어 세션 사용량 한도(429)에 걸린 뒤, 플러그인이 그 오류를 **예외로 올리지 않고
`Ollama 서버에서 오류 응답을 반환했습니다.` 문자열을 OCR 결과로 반환**했습니다. 그 문자열이 청킹·임베딩되어
13,566건의 쓰레기 문서가 적재됐습니다. 배치 로그의 페이지당 속도가 11초 → 1.0초로 "빨라진" 것이 이 증상이었습니다.

조치 완료:
- 플러그인 v0.1.2 — 모든 Ollama 오류를 예외로 전파, 본문 10자 미만도 예외 처리
- 배치 러너 — 오류 결과를 성공으로 기록하지 않고, 연속 5회 실패 시 전진 중단, 429는 재시도 없이 즉시 중단
- 오염 문서 13,566건 삭제(Dify API 경유로 벡터까지 정리), 상태파일 14,448행 → 890행으로 재조정하여 재처리 대상 복원
- 재현 검증: 429 상태에서 2페이지 시도 → 성공 0 / 실패 2, 오류 문자열 세그먼트 0건, 상태파일 증가 없음

현재 적재: 문서 923건(정상 917 / 오류 5 / 대기 1), 세그먼트 7,303개, 오류 문자열 0건.

---

| # | 프로세스 | 실행 명령 | 로그 | 중지 |
|---|---|---|---|---|
| 1 | **페이지 OCR 배치** | `python3 run_batch_parallel.py --concurrency 10` | `scripts/batch_state/batch_run_parallel_full.log` | `pkill -f run_batch_parallel.py` |
| 2 | **메타데이터 자동 주입 루프** (10분 주기) | `scripts/inject_metadata_loop.sh` | `scripts/batch_state/` 내 metadata 로그 | `touch scripts/STOP_METADATA_LOOP` |
| 3 | **web 프론트** | 호스트 프로덕션 빌드 + `next start -p 3000`, nginx가 `host.docker.internal:3000`으로 프록시 | `/tmp/difybuild/host_web_prod2.log` | `pkill -f "next start"` |

작업 루트: `~/Documents/proj/dify/docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts`

### web 프론트가 이 구성인 이유

`docker compose build web`이 두 번 연속 같은 지점에서 실패했습니다 — 컨테이너 안에서
`unofficial-builds.nodejs.org`의 musl용 node 바이너리를 내려받다 타임아웃(로그: `/tmp/difybuild/web_build2.log`,
`web_build3.log`). `.npmrc`로 fetch 타임아웃을 늘려도 같은 지점에서 멈췄습니다(해당 다운로드는 pnpm
fetcher가 아니라 패키지 install 스크립트가 자체 타임아웃으로 수행). 그래서 호스트에서
`pnpm build`(성공) 후 `next start`로 서빙하는 방식으로 전환했습니다.

`next start`는 프로덕션 빌드를 그대로 서빙합니다. dev 서버가 아니므로 안정적입니다.
검증: `/signin` 200, `/agent/knnsy0VMJudIZGdu` 200, 보호 경로 307(정상 로그인 리다이렉트),
`/_next/static/chunks/*.js` 200, 그리고 빌드 산출물에 `loadingPreview`(citation 수정 코드) 포함 확인.

### web 재기동

```bash
cd ~/Documents/proj/dify/web
nohup ./node_modules/.bin/next start -p 3000 > /tmp/difybuild/host_web_prod2.log 2>&1 &
```

코드를 다시 수정했으면 `pnpm build` 후 위 명령으로 재기동하세요.

---

## 2. 진행률 확인

```bash
cd ~/Documents/proj/dify/docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts

# 처리 완료 페이지 수 (전체 27,754)
wc -l < batch_state/processed_pages.jsonl

# 실시간 로그
tail -f batch_state/batch_run_parallel_full.log

# 데이터셋 적재 현황
cd ~/Documents/proj/dify/docker && docker compose exec -T db_postgres psql -U postgres -d dify -t -A -F'|' -c \
"SELECT count(*) docs, sum((indexing_status='completed')::int) ok, sum((indexing_status='error')::int) err
 FROM documents WHERE dataset_id='20087ab8-8e76-4f75-bfc8-88a24f4fd73c';"
```

## 3. 중단과 재개

```bash
# 중단 (진행분은 processed_pages.jsonl에 남아 안전)
pkill -f run_batch_parallel.py

# 재개 — 같은 명령 그대로. 이미 처리한 페이지는 자동 스킵
cd ~/Documents/proj/dify/docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts
nohup python3 run_batch_parallel.py --concurrency 10 >> batch_state/batch_run_parallel_full.log 2>&1 &
```

동시성은 `--concurrency` 로 조정합니다. 10이 실측 기준 최적 부근이며, Ollama Cloud 응답이
느려지거나 429가 보이면 6으로 낮추세요.

## 4. web 프론트를 컨테이너로 되돌리기

현재는 호스트 `next start`에 nginx를 붙인 구성입니다. 컨테이너 구성으로 되돌리려면:

```bash
cd ~/Documents/proj/dify/docker
cp nginx/conf.d/default.conf.original nginx/conf.d/default.conf
docker compose restart nginx
pkill -f "next start"
```

단, 되돌리면 **citation 미리보기 수정이 사라집니다**(공식 이미지에는 없는 코드).
컨테이너로 영구 전환하려면 커스텀 이미지 빌드가 필요하고, 그러려면 위에서 실패한
musl node 바이너리 다운로드 문제를 먼저 해결해야 합니다(네트워크 상태가 좋을 때 재시도하거나
해당 tarball을 미리 캐시).

## 5. citation 미리보기 확인 절차

1. `http://localhost/signin` 로그인
2. `http://localhost/agent/knnsy0VMJudIZGdu` ("희망사다리") 접속
3. 적재된 문서에 답이 있는 질문 입력 (예: 특정 연도의 원천세·금전출납부 관련)
4. 답변 하단 citation 태그 클릭 → 팝업에 페이지 이미지 썸네일과 다운로드 링크

## 6. 되돌리기 정보

| 변경 | 원래 값 | 되돌리는 방법 |
|---|---|---|
| `docker/.env` `CELERY_WORKER_AMOUNT` | 4 (현재 10) | 값 수정 후 `docker compose up -d --no-deps worker` |
| `docker/nginx/conf.d/default.conf` | `proxy_pass http://web:3000` | `default.conf.original` 복사 후 nginx 재시작 |
| `docker-compose.override.yaml` | plugin_daemon 포트만 | api/worker의 citation bind mount 항목 삭제 |

## 7. 남은 일

- 배치 완주 (전체 27,754 페이지). 실측 약 13초/페이지, 동시성 10 기준 **약 100시간**.
- `indexing_status='error'` 문서 재처리 (Ollama 일시 오류로 소수 발생).
- web 커스텀 이미지 전환 (§4).
- `chunk_structure` 폴백 커밋 2개 push / PR — 사용자 판단 대기.
