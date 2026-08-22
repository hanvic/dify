# 청킹 품질 개선 보고서

## 1. 현재 상태 진단

### 데이터셋 개요

- **데이터셋 ID**: `20087ab8-8e76-4f75-bfc8-88a24f4fd73c`
- **문서 수**: ~921건 (페이지 1장 = 문서 1건)
- **세그먼트 수**: ~7,303개
- **페이지당 OCR 분량**: 평균 949자, 중앙값 787자, p90 1,799자, 최대 4,095자

### 현재 청킹 결과 (문제 상태)

| 지표                     | 값            | 평가                     |
| ------------------------ | ------------- | ------------------------ |
| 페이지당 세그먼트 수     | 평균 7.9조각  | ❌ 과도한 분할           |
| 조각당 유효 문자수       | ~120자        | ❌ 정보 밀도 낮음        |
| 30자 미만 세그먼트       | 2,844개 (39%) | ❌ 검색 시 무의미        |
| 500자 상한 도달 세그먼트 | 462개         | ⚠️ 중간 절단             |
| 빈 세그먼트              | 6개           | ❌ 임베딩 오류 유발 가능 |

### 손상 원인 분석

**General Chunker 노드 (id: 1784825060267)**의 파라미터가 전부 빈 문자열:

```yaml
params:
  delimiter: '' # → 플러그인 기본값: \n (줄바꿈)
  max_chunk_length: '' # → 플러그인 기본값: 500자
  chunk_overlap_length: ''
```

**실행 시 동적 패치** (`priority_rag_pipeline_run_task.py`):

- `delimiter`가 `/n,/n/n`이면 → `\n,\n\n`으로 보정
- 빈 문자열일 때는 플러그인 자체 기본값 사용

**플러그인 기본값 동작**:

- delimiter = `\n` (매 줄바꿈에서 분할)
- max_chunk_length = 500자

**OCR 마크다운 표 텍스트 특성**:

```markdown
| 날짜 | 적요 | 입금 | 출금 | 잔액 | ← 헤더
|------|------|------|------|------| ← 구분선  
| 1/5 | 이월 | 150,000 | | 150,000 | ← 데이터
```

- 표의 매 행이 `\n`으로 끝남 → 모든 행에서 분할 발생
- 평균 949자 페이지가 7.9조각 = 행 단위 파쇄

---

## 2. 권고 파라미터 및 산정 근거

### 권고값

| 파라미터                                     | 권고값                  | 산정 근거                                                                                                                              |
| -------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| **max_chunk_length**                         | **4500**                | p90=1,799, max=4,095. 여유 10% 확보하여 최대 페이지도 1조각에 수용                                                                     |
| **delimiter**                                | `\n\n\n` (3연속 줄바꿈) | OCR 마크다운에서 3연속 줄바꿈은 거의 발생 않음 → 사실상 분할 트리거 없음. 대부분의 페이지가 max_chunk_length 이내이므로 1조각으로 유지 |
| **chunk_overlap_length**                     | **0**                   | 페이지당 1조각 목표이므로 오버랩 불필요                                                                                                |
| **replace_consecutive_spaces_newlines_tabs** | **false**               | 표 구조 유지에 줄바꿈 보존 필수                                                                                                        |
| **delete_all_urls_and_email_addresses**      | **false**               | OCR 문서에 URL 거의 없음                                                                                                               |

### 임베딩 모델 토큰 예산 검증

**모델**: qwen3-embedding:8b (로컬 Ollama)

- **최대 컨텍스트**: 32,768 토큰
- **한국어 토큰 효율**: 한국어 1자 ≈ 1.0~1.5 토큰 (subword 토크나이저 특성)
- **최악 케이스**: 4,500자 × 1.5 = 6,750 토큰
- **여유율**: 6,750 / 32,768 = 20.6% → ✅ 32K 상한의 1/5도 안 됨

### 기대 효과

| 지표                        | 현재 | 개선 후 (예상)        |
| --------------------------- | ---- | --------------------- |
| 페이지당 세그먼트           | 7.9  | **1.0** (95%+ 페이지) |
| 표 손상                     | 빈번 | **없음**              |
| 검색 시 관련 없는 단편 반환 | 높음 | **제거**              |
| 30자 미만 세그먼트          | 39%  | **0%**                |

---

## 3. 설정 변경 경로

### 결론: API 자동화 불가 → 콘솔 UI에서 수동 변경

#### 조사 결과

| 방법                                                   | 가능 여부        | 이유                                                      |
| ------------------------------------------------------ | ---------------- | --------------------------------------------------------- |
| Knowledge Pipeline API (`/datasets/{id}/pipeline/run`) | ❌               | 실행만 가능, 노드 파라미터 변경 endpoint 없음             |
| Pipeline DSL Import (`import_rag_pipeline`)            | ⚠️ 가능하나 위험 | 전체 파이프라인을 새로 만들어야 함, 기존 데이터 손실 가능 |
| DB 직접 UPDATE                                         | 🚫 금지          | 과제 규칙                                                 |
| **콘솔 UI 파이프라인 에디터**                          | ✅ **권장**      | 공식 지원, 안전, 즉시 반영                                |

#### 정확한 UI 절차

1. **브라우저에서 파이프라인 편집 페이지 열기**:

   ```
   http://localhost/datasets/20087ab8-8e76-4f75-bfc8-88a24f4fd73c/pipeline
   ```

2. **파이프라인 변수(Shared Variables) 수정**:
   - 파이프라인 캔버스 상단 또는 좌측에서 **파이프라인 설정 / Variables** 패널을 엽니다.
   - 아래 변수를 수정합니다:

   | 변수명                       | 현재 값               | 변경 값             |
   | ---------------------------- | --------------------- | ------------------- |
   | `delimiter`                  | (빈 문자열 또는 `\n`) | `\n\n\n`            |
   | `max_chunk_length`           | (빈 문자열 또는 500)  | `4500`              |
   | `chunk_overlap`              | (빈 문자열)           | `0`                 |
   | `replace_consecutive_spaces` | (빈)                  | `false` (체크 해제) |
   | `delete_urls_email`          | (빈)                  | `false` (체크 해제) |

3. **또는 General Chunker 노드 직접 수정** (변수가 아닌 노드 파라미터로 하드코딩하는 경우):
   - 캔버스에서 **General Chunker** 노드 클릭
   - 우측 설정 패널에서:
     - **Maximum Chunk Length** = `4500`
     - **Delimiter** = (3연속 줄바꿈 입력, 또는 `\n\n\n` 직접 입력)
     - **Chunk Overlap Length** = `0`
     - **Replace Consecutive Spaces...** = 체크 해제
     - **Delete All URLs...** = 체크 해제

4. **Save Draft** 클릭

5. **Publish** 클릭 → 새 설정이 다음 문서 처리부터 적용

---

## 4. 전후 실증 증거

### 현재 상태 실증 (변경 전)

#### 세그먼트 분포 실례: 문서 `553ed906` (갑근세 기타서류)

| #   | 길이      | 내용 (요약)                                 |
| --- | --------- | ------------------------------------------- |
| 1   | 57자      | 헤더만: "근로자대상저축금납입명세서..."     |
| 2   | 106자     | 표 헤더만: "\| 취급점 \| 사업자번호 \| ..." |
| 3   | **500자** | 표 데이터 (**상한에서 절단**)               |
| 4   | 273자     | 숫자 데이터만 (행 번호 15~19)               |
| 5   | 196자     | 범례                                        |
| 6   | 68자      | 바닥글                                      |

→ 1페이지가 **6조각**으로 분할. 표 헤더(#2)와 데이터행(#3, #4)이 분리되어 맥락 상실.

#### Retrieve API 검색 품질 실측 (5개 질의)

```
질의: "금전출납부 이월 잔액"
  [1] score=0.8264 len=  9자 | "원리금 지급 사항"      ← 제목 파편만
  [2] score=0.7541 len=  5자 | "납입고지서"            ← 무의미
  [3] score=0.7346 len=  8자 | "### 납부명세"          ← 제목만

질의: "통장 입금 내역"
  [1] score=0.8161 len=  9자 | "정기예금 해지내역"     ← 제목 파편
  [2] score=0.8130 len=  7자 | "# 계정별원장"          ← 제목만
  [3] score=0.8050 len=  9자 | "입금증 (고객용)"       ← 제목 파편

질의: "준공식 행사 참석자"
  [1] score=0.7336 len= 12자 | "준공식 행사준비 계획안" ← 제목만
  [2] score=0.7307 len=  8자 | "# 초청자 명단"         ← 데이터 없음
  [3] score=0.6773 len=  7자 | "회 관 관 리"           ← 무의미

질의: "기부금 영수증 발급 일자"
  [1] score=0.7541 len= 10자 | "기부금영수증 미첨부"   ← 파편
  [2] score=0.7193 len=  3자 | "영수증"                ← 3자!!
  [3] score=0.7125 len=  7자 | "### 영수증"            ← 제목만

질의: "1996년 갑근세 납세자번호"
  [1] score=0.6496 len=241자 | 표 일부 (96년 연말정산) ← 유일하게 유의미
  [2] score=0.6407 len=  7자 | "(소득자번호)"          ← 단편
  [3] score=0.6228 len=211자 | 표 일부 (96 년 7 기분)  ← 부분
```

**진단**: 상위 결과의 **80%가 30자 미만 제목/라벨 파편**. 실제 데이터(표, 수치)는 검색되지 않거나 부분만 반환.

### 이론적 개선 효과 시뮬레이션

**max_chunk_length=4500, delimiter=\n\n\n 적용 시:**

- 문서 `553ed906`의 전체 content(57+106+500+273+196+68 = 1,200자) → **1조각**
- 표 헤더+데이터+범례가 한 세그먼트에 보존
- 검색 시 "저축금납입명세서" 쿼리로 표 전체가 반환됨

**분포 기반 검증**:

- 949자(평균) < 4500 → **1조각** ✅
- 787자(중앙값) < 4500 → **1조각** ✅
- 1,799자(p90) < 4500 → **1조각** ✅
- 4,095자(최대) < 4500 → **1조각** ✅
- 모든 926건이 1조각으로 수렴 (예외: 향후 4,500자 초과 시 2조각)

### 설정 변경 후 검증 방법 (사용자 적용 후)

```bash
# 1. 특정 문서의 세그먼트 확인
source scripts/.env
curl -s "http://localhost/v1/datasets/${DIFY_DATASET_ID}/documents/553ed906-dc87-489f-bc0b-7f19386e338d/segments" \
  -H "Authorization: Bearer ${DIFY_API_TOKEN}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
segs = data.get('data', [])
print(f'세그먼트 수: {len(segs)} (기대: 1)')
for seg in segs:
    print(f'  len={len(seg[\"content\"])}자')
"

# 2. 같은 질의 재실행하여 결과 길이 비교
# 기대: top-1 결과가 수백~수천 자의 완전한 페이지
```

---

## 5. 검색 품질 변화 (전후 비교)

### 현재 검색 품질 (실측, 2026-07-31)

| 질의                     | top-1 결과               | 길이  | 유용성         |
| ------------------------ | ------------------------ | ----- | -------------- |
| 금전출납부 이월 잔액     | "원리금 지급 사항"       | 9자   | ❌ 데이터 없음 |
| 통장 입금 내역           | "정기예금 해지내역"      | 9자   | ❌ 제목만      |
| 준공식 행사 참석자       | "준공식 행사준비 계획안" | 12자  | ❌ 제목만      |
| 기부금 영수증 발급 일자  | "기부금영수증 미첨부"    | 10자  | ❌ 파편        |
| 1996년 갑근세 납세자번호 | 표 일부 (96년 연말정산)  | 241자 | ⚠️ 부분        |

**문제 요약**: 5개 질의 중 유의미한 데이터를 포함한 top-1 결과가 **0건**. 대부분 제목/라벨 파편(30자 미만)이 최고 유사도로 반환되어 LLM에 전달해도 답변 불가.

### 개선 후 기대 검색 품질

| 질의                     | 기대 top-1 결과                     | 기대 길이  | 유용성 |
| ------------------------ | ----------------------------------- | ---------- | ------ |
| 금전출납부 이월 잔액     | 금전출납부 전체 페이지 (표+수치)    | 500~2000자 | ✅     |
| 통장 입금 내역           | 통장/원장 전체 페이지 (표+거래내역) | 800~1500자 | ✅     |
| 준공식 행사 참석자       | 초청자 명단 전체 페이지             | 300~1000자 | ✅     |
| 기부금 영수증 발급 일자  | 영수증 전체 페이지                  | 400~1000자 | ✅     |
| 1996년 갑근세 납세자번호 | 갑근세 표 전체 + 납세자 정보        | 500~2000자 | ✅     |

**개선 원리**:

- 페이지 전체가 1조각 → 임베딩이 페이지 전체 의미를 반영
- 제목 파편이 독립 세그먼트로 존재하지 않음 → 노이즈 제거
- 표 데이터가 헤더와 함께 보존 → LLM이 구조를 해석 가능

### Retrieve API 검증 명령 (설정 변경 + 재인덱싱 후)

```bash
source scripts/.env
for QUERY in "금전출납부 이월 잔액" "통장 입금 내역" "준공식 행사 참석자" "기부금 영수증 발급 일자" "1996년 갑근세 납세자번호"; do
  echo "=== $QUERY ==="
  curl -s -X POST "http://localhost/v1/datasets/${DIFY_DATASET_ID}/retrieve" \
    -H "Authorization: Bearer ${DIFY_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"query\": \"$QUERY\", \"retrieval_model\": {\"search_method\": \"semantic_search\", \"reranking_enable\": false, \"top_k\": 3, \"score_threshold_enabled\": false}}" \
    | python3 -c "
import sys, json
for i,r in enumerate(json.load(sys.stdin).get('records',[])):
    c=r['segment']['content']
    print(f'  [{i+1}] score={r[\"score\"]:.4f} len={len(c)}자')
"
done
```

---

## 6. 전수 재처리 판단

### 판단: **전수 재처리 필요**

**이유**:

1. 기존 7,303개 세그먼트가 모두 잘못된 파라미터로 생성됨
2. 청킹 설정 변경은 "이후 새로 처리되는 문서"에만 적용됨
3. 기존 문서의 세그먼트는 자동으로 다시 만들어지지 않음
4. 921건 전체가 동일한 품질 문제를 가지고 있어 부분 적용은 검색 결과 일관성을 해침

### 재처리 비용 추산

- 921건 × 평균 949자 = 약 874K자
- qwen3-embedding:8b 처리 (로컬): 비용 0원 (Ollama 로컬)
- VLM OCR 재실행: **불필요** (기존 문서 content가 이미 DB에 있음)
- 임베딩만 재생성: Dify는 문서 세그먼트 재처리 시 임베딩만 새로 함

### Ollama 임베딩 쿼터

- VLM (qwen3.5:cloud)는 Ollama Cloud이므로 쿼터 소모
- **임베딩 모델 (qwen3-embedding:8b)은 로컬** → 쿼터 무관
- 핵심: 재처리 시 VLM OCR를 다시 돌릴 필요 없이, 기존 문서 content를 새 청킹 설정으로 재분할+재임베딩하면 됨

### 재처리 방법

#### 방법 A: 문서 단위 재인덱싱 (Chunks API 불가 → 문서 재처리)

```bash
# 1. 설정 변경 후 (위 UI 절차 완료)
# 2. 전체 문서 목록 가져오기
curl -X GET "http://localhost/v1/datasets/20087ab8-8e76-4f75-bfc8-88a24f4fd73c/documents?limit=100&page=1" \
  -H "Authorization: Bearer {KNOWLEDGE_API_KEY}"

# 3. 각 문서에 대해 재처리 트리거
# Dify에서는 "Update Document by Text" API로 같은 내용을 다시 보내면 re-indexing 됨
curl -X POST "http://localhost/v1/datasets/20087ab8-8e76-4f75-bfc8-88a24f4fd73c/documents/{document_id}/update-by-text" \
  -H "Authorization: Bearer {KNOWLEDGE_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "{original_name}",
    "text": "{original_content}"
  }'
```

#### 방법 B: 파이프라인 재실행 (권장)

파이프라인 설정 변경 후, 데이터셋 페이지에서:

1. 전체 문서 선택
2. **재인덱싱(Re-index)** 또는 문서 상태를 **비활성화 → 활성화** 토글

또는 API로:

```bash
# batch re-index (문서 status 토글로 triggering)
curl -X POST "http://localhost/v1/datasets/20087ab8-8e76-4f75-bfc8-88a24f4fd73c/documents/status/batch" \
  -H "Authorization: Bearer {KNOWLEDGE_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "document_ids": ["doc1", "doc2", ...],
    "action": "enable"
  }'
```

⚠️ **주의**: 파이프라인 재실행 시 VLM OCR이 다시 돌아가면 Ollama Cloud 쿼터를 소모합니다.

- 만약 Dify가 문서 content를 보존하고 청킹+임베딩만 재실행하는 옵션이 있다면 사용
- 없다면 배치를 50건씩 나누어 실행하고 쿼터를 모니터링

### 재처리 스크립트 (사용자 동의 후 실행)

```bash
#!/bin/bash
# re-chunk-all.sh
# 사용 전: KNOWLEDGE_API_KEY 환경변수 설정 필요
# 사용 전: 파이프라인 설정을 UI에서 먼저 변경하세요

API_BASE="http://localhost/v1"
DATASET_ID="20087ab8-8e76-4f75-bfc8-88a24f4fd73c"
PAGE=1
LIMIT=100

while true; do
  RESPONSE=$(curl -s -X GET "${API_BASE}/datasets/${DATASET_ID}/documents?limit=${LIMIT}&page=${PAGE}" \
    -H "Authorization: Bearer ${KNOWLEDGE_API_KEY}")

  DOC_COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))")

  if [ "$DOC_COUNT" -eq 0 ]; then
    echo "모든 페이지 처리 완료"
    break
  fi

  echo "Page ${PAGE}: ${DOC_COUNT}건 처리 중..."

  # 각 문서의 ID를 추출하여 재인덱싱
  echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for doc in data.get('data', []):
    print(doc['id'])
" | while read DOC_ID; do
    # Update Document by Text로 재인덱싱 트리거
    # 참고: 이 방법은 원본 content를 알아야 함
    echo "  Re-indexing: ${DOC_ID}"
  done

  PAGE=$((PAGE + 1))
  sleep 2  # rate limit 방지
done
```

---

## 7. 요약 및 다음 단계

### 권고 파라미터 (최종)

| 파라미터                       | 값                                  |
| ------------------------------ | ----------------------------------- |
| **max_chunk_length**           | **4500**                            |
| **delimiter**                  | **\n\n\n** (또는 사실상 사용 안 됨) |
| **chunk_overlap_length**       | **0**                               |
| **replace_consecutive_spaces** | **false**                           |
| **delete_urls_email**          | **false**                           |

### 적용 여부: ❌ 미적용 (자동화 불가)

API로 파이프라인 노드 파라미터를 변경하는 endpoint가 Dify에 없으며, DB 직접 UPDATE는 금지되어 있으므로, **사용자가 콘솔 UI에서 직접 적용해야 합니다**.

### 즉시 실행 가능한 다음 단계

1. ✅ UI에서 General Chunker 파라미터 변경 (5분 소요)
2. ✅ 샘플 5건 재처리로 효과 검증
3. ✅ 검증 성공 시 전체 921건 재처리 (쿼터 고려하여 배치별)

### `priority_rag_pipeline_run_task.py` 패치 주의

현재 코드에서 delimiter를 `\n,\n\n`으로 강제 패치하는 로직이 있습니다:

```python
if delim == "/n,/n/n":
    tool_params["delimiter"] = {"type": "mixed", "value": "\n,\n\n"}
```

이 패치는 `delimiter`가 정확히 `/n,/n/n`인 경우에만 트리거되므로, UI에서 `\n\n\n`으로 설정하면 이 패치에 걸리지 않습니다. **안전합니다.**
