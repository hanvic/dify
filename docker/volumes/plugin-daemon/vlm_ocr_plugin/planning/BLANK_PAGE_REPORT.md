# 번 페이지 감지 구현 보고서

## 개요
PDF 배치 OCR 처리 중 빈 페이지(blank page)를 감지하여 VLM 호출을 절약하고,
연속 실패 가드의 오작동을 방지하는 기능 구현.

## 임계치 교정 (2026-08-04)

### 데이터
- 원본: 희망브리지 문서전자화(2025.04) 27,754페이지
- 라벨 소스: index.db3의 block_text 길이 (CP949)
  - 빈 페이지 (text < 10자): 221건 (0.8%)
  - 내용 페이지 (text >= 50자): 26,951건

### 샘플링
- **빈 페이지**: 200건 렌더링 + 지표 계산
- **내용 페이지**: 300건 무작위 샘플링 + 지표 계산
- 렌더 조건: 200DPI, max 2048px, JPEG quality 70

### 측정 결과

| 그룹 | stddev | edge_mean | content_ratio |
|------|--------|-----------|---------------|
| **빈 페이지** (n=200) | min=2.98, med=13.79, max=84.21 | min=0.73, med=5.31, max=15.27 | min=0.0002, med=0.0106, max=0.9695 |
| **내용 페이지** (n=300) | min=14.47, med=37.42, max=79.26 | min=2.09, med=12.48, max=42.98 | min=0.0048, med=0.0560, max=0.9628 |

### 결정된 임계치

**사전 감지 (Pre-detection):**
```
blank = (stddev < 12.0) AND (edge_mean < 5.0)
```

| 지표 | 임계값 | 근거 |
|------|--------|------|
| stddev | < 12.0 | 내용 페이지 최솟값 14.47의 83% |
| edge_mean | < 5.0 | 내용 페이지 최솟값 2.09의 239% (보수적) |

### 성능

| 지표 | 값 |
|------|-----|
| **오판정률 (FP)** | **0% (0/300 내용 페이지)** |
| 검출률 (TP) | 31.5% (63/200 빈 페이지) |
| 절약 VLM 호출 | ~31.5% × 221건 ≈ 70건 |

오판정(내용 있는 페이지를 버리는 것)이 0%이므로 안전합니다.
나머지 68.5%의 빈 페이지는 VLM을 호출한 뒤 사후 감지로 잡힙니다.

## 사전·사후 감지 동작

### 1. 사전 감지 (Pre-detection)
- 위치: 배치 러너 `process_single_page`, 플러그인 `_process_pdf`
- 시점: 페이지 이미지 렌더 직후, VLM 호출 전
- 동작: PIL로 grayscale 변환 → stddev + FIND_EDGES → 임계치 비교
- VLM 호출: **하지 않음** (쿼터 절약)

### 2. 사후 감지 (Post-detection)
- 위치: `_call_ollama()` 내 품질 가드
- 시점: VLM 응답 수신 후, 본문이 10자 미만일 때
- 동작: 응답 텍스트에 "빈 페이지", "blank page" 등 키워드 매칭
  - 키워드 있음 → `BlankPageError` (번 페이지, 재시도 불필요)
  - 키워드 없음 → `OcrContentQualityError` (진짜 장애, 재시도 대상)

## 분류 결과 3가지 처리

| 결과 | 예외 타입 | 연속실패 산입 | 상태파일 | 동작 |
|------|-----------|:----------:|---------|------|
| **성공** | 없음 | 리셋 | `processed_pages.jsonl` (doc_id 포함) | 문서 생성 |
| **번 페이지** | `BlankPageError` | ❌ 아님 | `processed_pages.jsonl` (status=blank) + `blank_pages.log` | 스킵 (기본) 또는 에러 문서 |
| **장애** | `RateLimit/Server/Quality` | ✅ 증가 | `failed_*.log` | 재시도 후 중단 가능 |

## 연속실패 카운트 분리

핵심 변경: `BatchStats.add_blank()` 메서드는 `consecutive_failures`를 증가시키지 않음.
- 연속 6개 빈 페이지가 있어도 배치가 중단되지 않음
- 진짜 장애(429, 서버 오류, 네트워크)만 카운트 증가
- 기존 MAX_CONSECUTIVE_FAILURES=5 가드는 그대로 유지

### 검증
```
[success] → consecutive=0
[blank]   → consecutive=0 (변하지 않음)
[blank]   → consecutive=0
[failure] → consecutive=1
[blank]   → consecutive=1 (변하지 않음!)
[failure] → consecutive=2
[success] → consecutive=0
```

## 기록 파일

### `batch_state/processed_pages.jsonl`
빈 페이지도 여기에 기록(재시도 방지):
```json
{"key":"doc:p5","doc_name":"doc_p5.jpeg","doc_id":"","ts":"...","status":"blank","blank_method":"pre_image","blank_reason":"stddev=3.50<12 AND edge_mean=1.20<5.0"}
```

### `batch_state/blank_pages.log`
사람이 검토할 수 있는 별도 로그:
```
doc:p5|pre_image|method=pre_image|stddev=3.50|edge=1.20|content_ratio=0.0010|reason=...|2026-08-04T01:00:00Z
```

## 재처리 스위치

```bash
python3 run_batch_parallel.py --reprocess-blanks
```
- `blank_pages.log`를 읽어 해당 키들을 `processed_pages.jsonl`에서 제거
- 이후 일반 배치 재실행으로 해당 페이지만 재처리

## 설정

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `BLANK_PAGE_ACTION` | `skip` | `skip`=문서 미생성, `error`=에러 문서 생성 |
| `BLANK_STDDEV_THRESHOLD` | `12.0` | stddev 임계값 |
| `BLANK_EDGE_THRESHOLD` | `5.0` | edge_mean 임계값 |

## PDF 경로 동작 (UI 업로드)

- 빈 페이지 감지 시: 건너뛰고 다음 페이지 계속
- 결과 마크다운 끝에: `> ℹ️ 빈 페이지 건너뜀: p.2, p.5 (2장)`
- **모든 페이지가 빈 PDF**: `BlankPageError` 예외 발생 → 문서 실패 상태

## 테스트 결과

```
89 passed in 5.09s
```

주요 테스트 케이스:
- ✅ 순백색 이미지 = 번 판정
- ✅ 텍스트 있는 이미지 = 번 아님
- ✅ 번 페이지가 연속실패 카운트 미증가
- ✅ 상태파일 기록 포맷
- ✅ PDF 중 일부 번 페이지 스킵
- ✅ 전체 번 PDF 예외
- ✅ 429 에러는 여전히 장애 분류

## 되돌리는 방법

1. `manifest.yaml`의 `version`을 `0.1.3`으로 복원
2. `tools/blank_detector.py` 삭제
3. `tools/vlm_ocr.py`에서 blank_detector import 제거, 기존 OcrContentQualityError 로직 복원
4. `scripts/run_batch_parallel.py`에서 blank 관련 코드 제거
5. `bash scripts/install_plugin.sh`로 재설치

Git에서: `git diff HEAD~1` → 변경된 파일 확인 후 `git checkout HEAD~1 -- <files>`

## 플러그인 버전
0.1.3 → **0.1.4**
