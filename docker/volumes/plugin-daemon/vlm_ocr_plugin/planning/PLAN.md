# 실행 계획서 — 희망브리지 문서전자화 배치 임베딩 + Citation 미리보기

**버전**: 1.0 (리뷰 반영)  
**일시**: 2026-07-31  
**기반**: STATE_BRIEF.md + REVIEW.md 필수 수정 4가지 반영

---

## Phase 0: 환경 준비 (선행 조건)

### 0-1. 호스트 Python 도구 설치
- `pip3 install PyMuPDF Pillow` (PDF→이미지 변환 + 압축)
- 디스크 사용: ~50MB

### 0-2. 파이프라인 VLM 설정 변경 ★ REVIEW 필수수정 #2
- 파이프라인 워크플로우의 VLM OCR 노드 설정:
  - `include_summary`: true → **false**
  - `enable_thinking`: true → **false**
- 방법: DB UPDATE (workflows 테이블 graph JSON)
- 예상 효과: 처리 시간 40s → 16~22s/page

### 0-3. 처리 시간 전제조건 ★ REVIEW 필수수정 #3
- 현재 Ollama 구독: **Free** (사용자 확인 필요)
- Free 티어 시나리오: 순차 1건씩, ~22s/page × 27,754 = **170h (7.1일)**
- Pro 구독 시(동시3): ~57h (2.4일)
- **사용자 확인 필요**: Ollama 구독 티어

---

## Phase 1: 파일럿 (50페이지)

### 1-1. PDF→페이지 이미지 스트리밍 스크립트 작성
- `scripts/pdf_to_pages.py`: PyMuPDF로 PDF 1페이지씩 렌더링 → 압축 JPEG → 업로드 → 삭제
- 압축 목표: **long side 2048px, quality 70, 평균 ≤200KB** ★ REVIEW 필수수정 #1
- 동시에 디스크에 존재하는 임시 이미지: 최대 1개 (~300KB)

### 1-2. 배치 러너 v2 작성
- `scripts/run_batch_v2.sh`: PDF 목록 순회, pdf_to_pages.py 호출, 진행률 기록
- Idempotency: `processed_pages.jsonl` + API 문서 존재 확인 (이중 방어)
- 디스크 가드: 매 10페이지마다 `df` 체크, 5GB 미만이면 자동 중단
- 429 에러 감지 → 60초 대기 후 재시도, 5회 연속 429면 중단

### 1-3. 파일럿 실행
- 대상: 소규모 PDF 1~2건 (~50페이지)
- 검증 항목:
  - VLM 처리 속도 실측 (include_summary=false)
  - 429 발생 여부
  - 세그먼트 생성 확인
  - 압축 이미지 평균 크기 확인

---

## Phase 2: 중규모 배치 (일반문서 서류철, ~4,700페이지)

### 2-1. 배치 실행
- nohup 백그라운드 실행
- 로그: `scripts/batch_run_phase2.log`
- 중단/재개: processed_pages.jsonl 기반

### 2-2. 디스크 체크포인트 ★ REVIEW 필수수정 #4
- Phase 2 완료 후 디스크 실측
- 예산 초과 시 대안:
  - (A) 이미지 압축 강화 (1024px, quality 50)
  - (B) 블록OCR 텍스트 폴백 (VLM 불필요 페이지)
  - (C) 외부 스토리지 이전

---

## Phase 3: 전체 배치 (27,754페이지)

- Phase 2 디스크 검증 통과 후 진행
- 서류철 단위로 순차 처리
- 예상 영구 스토리지: 27,754 × 200KB = **5.3GB**

---

## 디스크 예산표 ★ REVIEW 필수수정 #1

| 항목 | 크기 | 누적 |
|------|------|------|
| 현재 사용 (upload_files 80MB) | 0.08GB | 0.08GB |
| Phase 1 (50p × 200KB) | 0.01GB | 0.09GB |
| Phase 2 (4,700p × 200KB) | 0.92GB | 1.01GB |
| Phase 3 (전체 27,754p × 200KB) | 5.3GB | 5.3GB |
| Weaviate 벡터 (27,754 docs) | 1.7GB | 7.0GB |
| PostgreSQL 메타데이터 | 0.5GB | 7.5GB |
| PyMuPDF+Pillow 설치 | 0.05GB | 7.55GB |
| **여유** | **8.45GB** | — |

**핵심**: 이미지 압축 목표 ≤200KB 준수 시 총 7.55GB 사용, 잔여 8.45GB > 5GB 안전선.

---

## Citation 미리보기

- 이미 코드 수정 완료 (uncommitted)
- api/web 컨테이너 재빌드하여 반영
- web 빌드 시 디스크 ~2GB 일시 사용 → 빌드 후 회수

---

## 사용자 확인 필요 항목
1. Ollama Cloud 구독 티어 (Free vs Pro) — 처리 시간에 직결
2. Phase 2 완료 후 전체 배치 진행 승인
