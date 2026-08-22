# Fail-Fast 수정 보고서

**작성일**: 2026-07-31 08:13 KST  
**플러그인 버전**: 0.1.1 → 0.1.2  
**사고 식별**: OCR 오류 문자열 13,566건 지식베이스 오염

---

## 1. 사고 장애 분석: 오류가 본문이 된 경로

### 정통 경로 (Root Cause Path)

```
Ollama Cloud → HTTP 429 (쿼터 초과)
  → _call_ollama() → raise ValueError("Ollama 서버에서 오류 응답을 반환했습니다.")
    → _invoke() → except ValueError as e: → yield self.create_text_message(str(e))
      → 파이프라인: 정상 텍스트 결과로 인식
        → chunker: "Ollama 서버에서 오류 응답을 반환했습니다." 한 줄을 세그먼트로 생성
          → embedding: 오류 문자열을 벡터화
            → 지식베이스: 문서 "completed", 세그먼트에 오류 문자열 저장
```

### 근본 원인

1. **플러그인**: `_invoke()`가 모든 ValueError/Exception을 catch하여 `create_text_message(str(e))`로 변환. Dify SDK 관점에서 이는 "정상 도구 결과"이므로 파이프라인이 계속 진행됨.
2. **배치 러너**: 문서가 "completed" 상태면 무조건 "성공"으로 기록. 세그먼트 내용을 검증하지 않음.
3. **연속 실패 중단 없음**: 14,000건이 동일 에러로 실패해도 배치가 멈추지 않음.

---

## 2. 수정 내용 (파일별)

### `tools/vlm_ocr.py` — 핵심 수정

| 변경 | 설명 |
|------|------|
| 커스텀 예외 3개 추가 | `OllamaRateLimitError`, `OllamaServerError`, `OcrContentQualityError` |
| `_call_ollama()` 429 처리 | `raise OllamaRateLimitError("[QUOTA_EXCEEDED]...")` — 상위 구별 가능 |
| `_call_ollama()` HTTP 에러 | `raise OllamaServerError(...)` — ValueError 대신 |
| `_call_ollama()` 본문 품질 가드 | 결과 < 10자 → `raise OcrContentQualityError(...)` |
| `_invoke()` try/except 제거 | 예외를 catch하지 않고 상위로 전파 → SDK가 문서를 error 상태로 처리 |

**본문 품질 가드 기준**: 10자 미만 (근거: 한국어 3~5음절, 영어 2~3단어 미만이면 유효한 OCR 결과일 수 없음. 빈 페이지도 최소 "빈 페이지" 등의 진단 결과를 포함해야 함.)

### `scripts/run_batch_parallel.py` — 배치 가드레일

| 변경 | 설명 |
|------|------|
| `PoisonedContentError` 추가 | 오염 감지 시 발생하는 예외 |
| `ERROR_CONTENT_PATTERNS` 목록 | 10개 알려진 오류 패턴 (한국/영어) |
| `_verify_document_segments()` | 문서 완성 후 세그먼트 내용 검증 |
| `MAX_CONSECUTIVE_FAILURES = 5` | 연속 5회 실패 시 배치 전진 즉시 중단 |
| 429 즉시 중단 | 재시도하지 않고 `request_abort("quota_exceeded_429")` |
| `summary_str()` | 진행 로그에 ✅/❌/⏭️ 건수 포함 |
| `abort_reason` 필드 | 중단 사유 기록 및 요약에 표시 |

### `manifest.yaml`

- `version: 0.1.1` → `version: 0.1.2`

### `tests/test_vlm_ocr.py` — TDD 테스트 (22개)

- `Test429RateLimit`: 429 → OllamaRateLimitError, _invoke에서도 전파
- `Test500ServerError`: 500 → OllamaServerError, _invoke에서도 전파
- `TestEmptyContent`: 빈/공백/짧은 → OcrContentQualityError
- `TestNormalOperation`: 정상 동작 회귀, 최소 유효 길이 경계
- `TestErrorNeverInOutput`: 에러 패턴이 정상 결과에 없음 확인

### `tests/test_batch_guardrails.py` — 배치 테스트 (9개)

- 오류 패턴 감지 (오염/정상/빈 세그먼트)
- 연속 실패 임계치
- 성공 시 카운터 리셋
- 429 즉시 중단
- 요약 문자열 포맷

---

## 3. 테스트 결과

```
======================== 46 passed, 7 warnings in 3.43s ========================
```

- `tests/test_vlm_ocr.py`: 22 passed ✅
- `tests/test_pdf_to_pages.py`: 15 passed ✅  
- `tests/test_batch_guardrails.py`: 9 passed ✅

---

## 4. 429 상태에서의 재현 검증 증거

### 환경
- Ollama Cloud: HTTP 429 활성 (victoryhan 계정 세션 한도 초과)
- 플러그인: v0.1.2 설치 확인됨

### 결과

```
Doc ID: c4796028-e701-46e2-af68-f5d92ff9a199
Status: error  ← ✅ 이전에는 "completed"였음
Error: An error occurred in the vlm_ocr/vlm_ocr/vlm_ocr, ... 
       error type: OllamaRateLimitError, 
       error details: [QUOTA_EXCEEDED] Ollama API 쿼터 초과 (HTTP 429)...

Segment count: 0  ← ✅ 오류 문자열이 임베딩되지 않음
```

**이전 동작 (v0.1.1)**:
- Status: `completed`, Segments: 1, Content: "Ollama 서버에서 오류 응답을 반환했습니다."

**수정 후 동작 (v0.1.2)**:
- Status: `error`, Segments: 0, Error 메시지에 "QUOTA_EXCEEDED" 식별자 포함

---

## 5. 배치 러너 가드레일 요약

| 가드레일 | 트리거 조건 | 동작 |
|----------|------------|------|
| 연속 실패 중단 | 5회 연속 실패 | `abort("consecutive_failures_N")` |
| 429 즉시 중단 | Dify API 429 | `abort("quota_exceeded_429")` — 재시도 없음 |
| 오염 감지 | 세그먼트에 에러 패턴 | `PoisonedContentError` → failed + abort 검토 |
| 빈 세그먼트 | completed인데 0개 | 실패 처리, 문서 삭제 |
| 진행 로그 | 매 5건 | `✅N ❌N ⏭️N (연속실패:N)` 형식 |

---

## 6. 남은 위험

| 위험 | 심각도 | 대응 방안 |
|------|--------|-----------|
| Ollama 쿼터가 해제되기 전까지 OCR 불가 | 높음 | 사용자 결정 필요 (업그레이드 또는 로컬 모델) |
| 다른 모델도 비슷한 오류 텍스트를 반환할 수 있음 | 중간 | 10자 품질 가드가 방어하지만, 더 긴 에러 메시지는 패턴 목록에 추가 필요 |
| 파이프라인 노드 구성 변경 시 플러그인 재연결 필요 | 낮음 | 파이프라인에서 vlm_ocr 도구 노드가 0.1.2를 참조하는지 확인 |
| 13,566건 오염 삭제 완료 후 재처리 필요 | 중간 | 다른 에이전트가 삭제 중. 쿼터 해결 후 배치 재개 |

---

## 결론: 이제 같은 사고가 발생할 수 있는가?

**아니오.**

근거:
1. **플러그인 레벨**: 모든 Ollama 오류가 이제 예외로 전파되어 문서가 `error` 상태가 됨. 오류 문자열이 정상 결과로 반환되는 경로가 완전히 제거됨.
2. **본문 품질 가드**: 200 OK여도 결과가 10자 미만이면 예외 발생. 모델이 예상치 못한 짧은 에러를 반환해도 방어됨.
3. **배치 러너 레벨**: 연속 5회 실패 시 배치 자동 중단. 14,000건 연쇄 오염 불가.
4. **429 전용**: 쿼터 초과 시 재시도 없이 즉시 중단 + 명확한 로그.
5. **세그먼트 검증**: 배치 러너가 완성된 문서의 세그먼트까지 오염 패턴 검사.
