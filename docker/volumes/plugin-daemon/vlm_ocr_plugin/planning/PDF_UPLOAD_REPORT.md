# PDF Upload Report

Dify UI를 통한 PDF 직접 업로드 지원 구현 보고서.

## 사용자 절차

1. Dify 콘솔 → Knowledge → 대상 지식베이스 열기
2. "Add File" 클릭 → PDF 파일을 드래그하거나 선택
3. 파이프라인이 자동으로 실행됨: File(datasource) → VLM OCR → General Chunker → knowledge-index
4. PDF의 각 페이지가 OCR되어 하나의 문서로 적재됨
5. 세그먼트에 `## 📄 p.N` 구분자가 포함되어 citation에서 페이지 번호 확인 가능

## 설계 결정과 근거

| 결정 | 근거 |
|------|------|
| 순차 OCR (병렬 아님) | Ollama Cloud 무료 티어 쿼터 보호. 120p/3600s 내 처리 가능 |
| max_pages=120 기본값 | 3600초 / 30초(worst case per page) = 120 |
| 메모리 512MB | PyMuPDF(~50MB) + 렌더(~30MB) + Pillow(~50MB) + 기존 + 여유 |
| UPLOAD_FILE_SIZE_LIMIT=50MB | nginx 100M 상한 대비 보수적. 일상 PDF 커버 |
| 페이지 구분자 `## 📄 p.N` | Chunker가 `##`을 자연 분할점으로 인식. Citation 추적 가능 |
| JPEG 2048px/quality 70 | 기존 pdf_to_pages.py와 동일 기준. 평균 168KB, 최대 250KB |
| PdfPageLimitExceededError | 초과 시 조용히 자르지 않고 명시적 예외 |
| Fail-fast 유지 | 429/5xx/빈 결과 → 예외 전파. 오염된 텍스트 임베딩 방지 |

## 사이즈·페이지 한도와 초과 시 동작

| 한도 | 값 | 초과 시 동작 |
|------|---|------------|
| 파일 업로드 크기 | 50MB | HTTP 413 (nginx 100M이 최종 상한) |
| PDF 최대 페이지 | 120 (기본, 파라미터로 변경 가능) | PdfPageLimitExceededError → 문서 error 상태 |
| VLM 타임아웃 | 3600초 | OllamaServerError → 문서 error 상태 |
| 페이지 이미지 크기 | ≤250KB (adaptive quality) | 자동 품질 하향 조정 |

## 테스트 결과

```
tests/test_pdf_support.py - 13 tests passed (0 failed)
tests/ 전체 - 59 tests passed (0 failed)
```

테스트 커버리지:
- ✅ PDF 감지 (MIME/extension)
- ✅ 페이지 순서 보장 (p.1 → p.2 → p.3)
- ✅ 페이지 수 한도 초과 시 예외
- ✅ 압축 범위 (250KB 이하)
- ✅ 기존 이미지 경로 회귀
- ✅ Fail-fast: 429/5xx → 예외 전파
- ✅ 페이지 구분자 형식

## 실동작 검증 증거

### 업로드 + 파이프라인 실행

```
# 1. PDF 업로드 (API: /v1/datasets/pipeline/file-upload)
→ file_id: 750196fd-8bcd-4314-9cd5-2af8ab0bda23
→ mime_type: application/pdf, size: 12790 bytes

# 2. 파이프라인 실행 (API: /v1/datasets/{id}/pipeline/run)
→ document_id: 46f1e7ea-7fd8-42dd-b92c-b70b7b31071b
→ indexing_status: completed (80초 후)

# 3. 세그먼트 확인 (DB 직접 쿼리)
→ 총 20개 세그먼트
→ 페이지 구분자 확인: "## 📄 p.1", "## 📄 p.2", "## 📄 p.3"
```

### 세그먼트 본문 샘플

```
Position 1: "## 📄 p.1"
Position 2: "# Page 1 - PDF Upload Test"
Position 3: "This is page 1 of a test document for VLM OCR."
Position 6: "| Cell R1C1 | Cell R1C2 | Cell R1C3 | ..."  (표 OCR)
Position 8: "## 📄 p.2"
Position 15: "## 📄 p.3"
```

### 플러그인 버전 확인

```
plugin_id=vlm_ocr/vlm_ocr version=0.1.3 memory=536870912
```

## 되돌리는 방법

1. **업로드 제한 원복**: `docker/.env`에서 `UPLOAD_FILE_SIZE_LIMIT=50` 행 삭제 후 `docker compose up -d --no-deps api worker`
2. **플러그인 롤백**: v0.1.2 패키지로 `install_plugin.sh` 재실행 (manifest.yaml version: 0.1.2, requirements.txt에서 PyMuPDF 제거, tools/vlm_ocr.py에서 PDF 관련 코드 제거)
3. **메모리 원복**: manifest.yaml의 `resource.memory`를 `268435456`으로 변경

## 남은 제약

1. **120페이지 한도**: 그 이상은 배치 스크립트(`scripts/run_batch_parallel.py`) 사용 필요
2. **순차 처리 속도**: 10~30초/페이지 → 120페이지 시 최대 60분
3. **50MB 파일 크기 제한**: 대용량 PDF(>50MB)는 UI 업로드 불가
4. **Ollama Cloud 쿼터**: 무료 티어 한도 초과 시 429 에러 (fail-fast로 문서 error 상태)
5. **PyMuPDF 플러그인 컨테이너 설치**: requirements.txt에 포함되어 packager가 자동 설치하지만, 초기 설치 시간 증가
