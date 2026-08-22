# LAUNCH_AUDIT_REPORT.md

> 작성: 2026-08-02 15:33 KST

## 감사 결과 요약

| #   | 항목                    | 결과        | 비고                                                           |
| --- | ----------------------- | ----------- | -------------------------------------------------------------- |
| 1   | 청킹 설정               | **PASS**    | delimiter='\n\n---\n\n', max_chunk_length=6000, overlap=0 확인 |
| 2   | 페이지당 1조각          | **PASS**    | 최신 10건 전부 seg_count=1 (493~1580자)                        |
| 3   | 오류 문자열 0건         | **PASS**    | `Ollama 서버에서 오류` LIKE 0건                                |
| 4   | 문서/세그먼트 정합      | **PASS**    | docs=24, segs=24, 중복 이름 0건                                |
| 5   | 상태파일 정합           | **PASS**    | jsonl 24행 = DB 24건 (기동 후 증가 확인)                       |
| 6   | 수퍼바이저/키프얼라이브 | **PASS**    | keepalive가 25초 내 web 복구 확인, 이후 정상 복원              |
| 7   | citation/콘솔 경로      | **PARTIAL** | /signin=200, /agent=200, 파일 preview=세션 인증 필요(401)      |
| 8   | 리소스                  | **PASS**    | 디스크 25GB free, 모든 컨테이너 healthy, Ollama 200            |
| 9   | 검색 품질               | **PASS**    | 6/6 질의 유의미한 결과 반환 (score 0.45~0.60)                  |

## 상세 근거

### 1. 청킹 설정 (PASS)

```
docker exec dify-db_postgres-1 psql -U postgres -d dify -c "SELECT graph FROM workflows WHERE id='7ba20982-9f2c-47b8-8ddc-256f116e972d'"
```

결과: node 1784825060267의 tool_parameters:

- delimiter: "\n\n---\n\n"
- max_chunk_length: 6000
- chunk_overlap_length: 0

### 2. 페이지당 1조각 (PASS)

```sql
SELECT d.name, COUNT(ds.id) as seg_count FROM documents d LEFT JOIN document_segments ds ON ds.document_id=d.id ...
```

최신 10건 전부 `seg_count=1`, 내용 길이 473~1580자.

### 3. 오류 문자열 (PASS)

```sql
SELECT COUNT(*) FROM document_segments WHERE content LIKE '%Ollama 서버에서 오류%';
-- 결과: 0
```

### 4. 정합성 (PASS)

- completed 문서: 24건 (감사 시점)
- completed 세그먼트: 24건
- 중복 이름: 0건

### 5. 상태파일 (PASS)

- `processed_pages.jsonl`: 24행 (감사 시점)
- 삭제된 문서 키 잔존 여부: requality에서 893행 삭제 후 2행만 남긴 상태에서 출발. 현재 24행은 모두 실제 존재하는 문서와 매핑.

### 6. 키프얼라이브 테스트 (PASS)

```
kill 6490 (next-server PID)
→ HTTP 000 (다운)
→ 25초 후 HTTP 200 (자동 복구)
→ Final: HTTP 200 (정상)
```

배치 수퍼바이저: `bash -n scripts/batch_supervisor.sh` → SYNTAX OK

### 7. citation/콘솔 (PARTIAL PASS)

- `http://localhost/signin` → HTTP 200
- `http://localhost/agent/knnsy0VMJudIZGdu` → HTTP 200
- 파일 다운로드: 콘솔 세션 토큰 필요 (API 토큰으로 접근 불가). Dify 웹 UI에서 브라우저 세션으로 정상 접근 가능 (아키텍처상 정상).
- upload_files 레코드 확인: id=6cee81bf, storage=opendal, mime=image/jpeg, size=244511 bytes ✓

### 8. 리소스 (PASS)

```
df -h /: 25GB free
Containers: 13개 전부 Up (api/worker/nginx/redis/postgres/weaviate 등 healthy)
Ollama: /api/tags=200, /api/generate=200 (쿼터 정상)
```

### 9. 검색 품질 (PASS)

6개 한국어 질의 결과:
| 질의 | 결과수 | 최고 score | 내용 길이 |
|------|--------|-----------|----------|
| 원천징수 영수증 금액 | 1 | 0.6046 | 1499자 |
| 1994년 갑근세 납부 내역 | 2 | 0.4634 | 67/1499자 |
| 전국재해대책협의회 적립금 | 1 | 0.4816 | 67자 |
| 은행 거래내역 잔액 | 1 | 0.5115 | 1499자 |
| 세금 공제 항목 | 1 | 0.5084 | 1499자 |
| 급여 이체 내역 | 1 | 0.4967 | 1499자 |

판정: 현재 2건의 원본 문서에 대해 의미적으로 관련된 결과를 정확히 반환. 은행 거래내역 표가 1499자 온전하게 검색됨. 배치 진행으로 데이터 축적 시 품질 더 향상 예상.

## 수정한 결함

### Weaviate 고아 벡터 정리 (Critical Fix)

- **문제**: 이전 삭제(926건)된 문서의 벡터 5720개가 Weaviate에 잔존. retrieve API가 이 고아 벡터를 top-k에 먼저 반환 → DB 매칭 실패 → 빈 결과.
- **수정**: Weaviate batch delete API로 유효한 2개 index_node_id 외 전체 삭제.
  ```
  DELETE /v1/batch/objects (dryRun=true → 5720 matches)
  DELETE /v1/batch/objects (dryRun=false → 5720 successful, 0 failed)
  ```
- **검증**: 삭제 후 retrieve "원천징수" → score=0.6046, len=1499 정상 반환.

## 배치 기동 직후 실측 (5분간)

| 시점      | processed_pages | 문서 수 | 속도          |
| --------- | --------------- | ------- | ------------- |
| 기동 직후 | 2               | 2       | -             |
| +1분      | 8               | -       | -             |
| +2분      | 13              | -       | 17.0초/페이지 |
| +3분      | 17              | 18      | 14.0초/페이지 |
| +4분      | 17→?            | 18      | 13.4초/페이지 |
| +5분      | 24              | 24      | 14.2초/페이지 |

- **5분간 22페이지 처리** (4.4페이지/분)
- **오류 0건**, 스킵 0건
- **모든 문서 1세그먼트** 확인
- ETA: ~112시간 (~4.7일)

## 현재 실행 상태

| 프로세스                | PID   | 상태                        |
| ----------------------- | ----- | --------------------------- |
| batch_supervisor.sh     | 59577 | ✅ Running                  |
| run_batch_parallel.py   | 59614 | ✅ Running (concurrency=10) |
| web_keepalive.sh        | 49223 | ✅ Running                  |
| inject_metadata_loop.sh | 49465 | ✅ Running (600초 주기)     |

## 남은 위험

1. **Ollama 쿼터 소진**: 현재 정상이지만 cloud 모델 사용 중이라 쿼터 제한 발생 가능. 수퍼바이저가 429 감지 후 지수 백오프(30분→60분→120분)로 자동 재시도.
2. **디스크**: 현재 25GB free. 28000페이지 × ~250KB/image = ~7GB 추가 예상. 안전 마진 충분.
3. **메타데이터 주입 지연**: 600초 주기로 돌아가므로 새 문서에 메타데이터가 즉시 붙지 않음. 배치 완료 후 --force 한 번 돌리면 전부 반영.
4. **TOTAL_PAGES 불일치**: batch_supervisor.sh에 27754로 되어 있으나 실제 28403페이지. 진행률 표시만 약간 부정확(기능에 영향 없음).

## 사용자 확인 필수 항목

1. **citation preview**: 브라우저에서 Dify 콘솔 로그인 후 문서 미리보기 정상 작동하는지 육안 확인 필요.
2. **Ollama 쿼터 모니터링**: `cat scripts/batch_progress.txt`로 주기적 확인 권장.
3. **배치 중단**: `touch scripts/STOP_BATCH_SUPERVISOR` 로 안전 중단 가능.
