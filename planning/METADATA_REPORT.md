# METADATA_REPORT.md

## 희망브리지 문서 메타데이터 자동 주입 완료 보고서

**작성일**: 2026-07-31  
**데이터셋**: `20087ab8-8e76-4f75-bfc8-88a24f4fd73c`

---

## 1. 스크립트 사용법

### 기본 실행 (미연결 문서만 처리)

```bash
python3 scripts/inject_metadata.py
```

### 전수 재주입 (강제)

```bash
python3 scripts/inject_metadata.py --force
```

### 미리보기 (실제 주입 없이 확인)

```bash
python3 scripts/inject_metadata.py --dry-run
```

### 현재 상태 확인만

```bash
python3 scripts/inject_metadata.py --verify-only
```

### 환경변수 (.env)

`scripts/.env`에서 설정 로드:

- `DIFY_BASE_URL` - Dify API 주소
- `DIFY_DATASET_ID` - 데이터셋 ID
- `DIFY_API_TOKEN` - API 토큰
- `INDEX_DB_PATH` - index.db3 경로

---

## 2. 매칭 로직 설명

### 문서명 → index.db3 매핑 과정

1. **index.db3 로드**: CP949로 인코딩된 SQLite DB를 `text_factory = lambda b: b.decode('cp949','replace')`로 디코딩
2. **키 생성**: `file_path` 필드에서 폴더명과 PDF 파일명을 추출하여 `{folder}_{pdf_stem}` 형태의 base_key 생성
   - 예: `희망브리지...\회계장부_2002~2004 기타장부\금전출납부 1 복지자금.pdf`  
     → base*key = `회계장부\_2002~2004 기타장부*금전출납부 1 복지자금`
3. **문서명 파싱**: Dify 문서명에서 `_p{N}.jpeg` 접미사를 분리하여 base_key와 페이지 번호 추출
   - 예: `회계장부_2002~2004 기타장부_금전출납부 1 복지자금_p0.jpeg`  
     → base*key = `회계장부\_2002~2004 기타장부*금전출납부 1 복지자금`, page = 0
4. **NFC 정규화**: macOS APFS의 NFD 파일명을 NFC로 변환하여 비교
5. **폴백**: index.db3에 없는 문서(예: `준공식 행사_*`)는 base_key에서 서류철명을 추출하여 부분 메타데이터 생성

### 폴백 로직 (index.db3에 없는 문서)

- 연도 범위 패턴(`{서류철}_{연도}`) 감지 시: 서류철명을 추출하고 나머지를 문서명으로 설정
- 연도 없는 경우: 첫 번째 `_` 기준으로 서류철명/문서명 분리
- 판단 근거: 준공식 행사 등의 문서는 index.db3에 미등재된 추가 자료이므로, 완전 스킵보다는 서류철명이라도 넣어 검색 가능하게 함

---

## 3. 전·후 바인딩 수

| 지표                         | Before | After                  |
| ---------------------------- | ------ | ---------------------- |
| dataset_metadata_bindings 행 | 440    | 1,560                  |
| 문서 총수                    | 276    | 304 (배치 진행 중)     |
| 메타데이터 연결된 문서       | 72     | 296                    |
| 미연결 문서                  | 204    | 8 (인덱싱 대기 중)     |
| 매핑 성공                    | -      | 221건 (폴백 33건 포함) |
| 매핑 실패                    | -      | 0건                    |

---

## 4. 테스트 결과

```
$ python3 -m pytest tests/test_inject_metadata.py -v

tests/test_inject_metadata.py::TestParseDocName::test_standard_pattern PASSED
tests/test_inject_metadata.py::TestParseDocName::test_page_number_multidigit PASSED
tests/test_inject_metadata.py::TestParseDocName::test_no_page_number PASSED
tests/test_inject_metadata.py::TestParseDocName::test_jpg_extension PASSED
tests/test_inject_metadata.py::TestParseDocName::test_nfc_normalization PASSED
tests/test_inject_metadata.py::TestParseDocName::test_parentheses_in_name PASSED
tests/test_inject_metadata.py::TestFallbackMetadata::test_with_year_range PASSED
tests/test_inject_metadata.py::TestFallbackMetadata::test_with_single_year PASSED
tests/test_inject_metadata.py::TestFallbackMetadata::test_no_year_fallback PASSED
tests/test_inject_metadata.py::TestLoadIndexDb::test_load_and_decode PASSED
tests/test_inject_metadata.py::TestLoadIndexDb::test_key_matches_doc_name_parse PASSED
tests/test_inject_metadata.py::TestIdempotency::test_skip_existing_metadata PASSED
tests/test_inject_metadata.py::TestIdempotency::test_force_reinjection PASSED

============================== 13 passed in 0.04s ==============================
```

---

## 5. 메타데이터 필터 검색 증거

### 테스트 A: 서류철명 필터

```
POST /v1/datasets/{id}/retrieve
{
  "query": "원천세",
  "retrieval_model": {
    "search_method": "semantic_search",
    "top_k": 5,
    "metadata_filtering_conditions": {
      "logical_operator": "and",
      "conditions": [{"name": "서류철명", "comparison_operator": "contains", "value": "갑근세"}]
    }
  }
}

결과: 5건, 전부 서류철명="갑근세 기타서류(적립금)" 문서만 반환됨
```

### 테스트 B: 서류철 대비

```
필터: 서류철명 contains "회계장부", query="금전출납" → 5건 (회계장부 문서만)
필터: 서류철명 contains "일반문서", query="금전출납" → 0건 (일반문서에는 금전출납 없음)
```

**결론**: 메타데이터 필터가 정상 동작하여 특정 서류철/연도만 검색 가능

---

## 6. 주기 실행 기동·중지 방법

### 기동

```bash
cd /path/to/dify
nohup bash scripts/inject_metadata_loop.sh > scripts/inject_metadata_loop.log 2>&1 &
```

- 10분(600초) 간격으로 `inject_metadata.py` 자동 실행
- PID: `scripts/inject_metadata_loop.pid`에 기록
- 로그: `scripts/inject_metadata_loop.log`

### 중지 (2가지 방법)

```bash
# 방법 1: kill
kill $(cat scripts/inject_metadata_loop.pid)

# 방법 2: graceful stop (10초 이내 종료)
touch scripts/STOP_METADATA_LOOP
```

### 현재 상태

- PID 68005로 실행 중
- 첫 루프에서 신규 3건 자동 주입 확인 완료

---

## 7. 미매칭 건수와 사유

| 구분           | 건수 | 사유                                                                             |
| -------------- | ---- | -------------------------------------------------------------------------------- |
| 직접 매칭 성공 | 188  | index.db3 base_key와 정확히 일치                                                 |
| 폴백 매칭      | 33   | index.db3에 미등재 (준공식 행사 등), 문서명에서 서류철명 추출하여 부분 메타 생성 |
| 매칭 실패      | 0    | -                                                                                |

### 폴백 대상 문서 (33건)

- `준공식 행사_*` 계열 문서들: index.db3에 해당 서류철이 등재되어 있지 않으나, 문서명 패턴에서 "준공식 행사"를 서류철명으로 추출하여 메타데이터 주입
- 판단: 완전 스킵보다 부분 메타(서류철명만이라도)를 넣는 것이 검색에 유리

---

## 산출물 목록

| 파일                              | 설명                                      |
| --------------------------------- | ----------------------------------------- |
| `scripts/inject_metadata.py`      | 메인 주입 스크립트 (idempotent, API 기반) |
| `scripts/inject_metadata_loop.sh` | 10분 주기 자동 실행 래퍼                  |
| `scripts/.env`                    | 환경변수 (API 토큰 등)                    |
| `scripts/inject_metadata.log`     | 실행 로그                                 |
| `tests/test_inject_metadata.py`   | pytest 단위 테스트 (13개)                 |
| `planning/METADATA_REPORT.md`     | 이 보고서                                 |
