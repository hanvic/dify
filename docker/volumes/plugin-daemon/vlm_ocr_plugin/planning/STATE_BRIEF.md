# STATE BRIEF — 희망브리지 문서전자화 RAG 챗봇 (2026-07-31 검증됨)

이 문서는 메인 에이전트가 **직접 명령을 실행해 확인한 사실**만 담습니다. 추측은 `(추정)`으로 표시.
모든 서브에이전트는 작업 전 이 문서를 먼저 읽고, 필요하면 재검증 후 갱신하세요.

---

## 0. 최종 목표 (사용자 진술)

Ollama VLM으로 **기업 문서 스캔본을 디지털화** → Dify 지식베이스에 임베딩 → **챗봇 대화 시 참조 문서를
미리보기(citation preview) 할 수 있는 UI 제공**.

접근 방식 결정 이력:
- 초기: Dify RAG 파이프라인을 UI로 조립(PDF → PDF-to-Image 플러그인 → Vision → 임베딩).
  - 실패 원인: 원본 PDF가 **100~300MB**(수백 페이지)라서 타임아웃, 설정 난이도, 표/필기체 추출 품질 저하.
- 현재: **자체 제작 Tool 플러그인**(VLM OCR)으로 `파일 → VLM OCR → chunk → embedding` 파이프라인 구성.
- 다음 단계: 이미지 뿐 아니라 **원본 PDF를 그대로 처리**하는 경로 확보 + 전체 배치 임베딩.

---

## 1. 원본 데이터 인벤토리 (검증)

### 1.1 `~/Downloads/희망브리지/`
```
총 809 파일 / 20 GB
├── 희망브리지 문서전자화(2025.04)/       # 21개 서류철 디렉터리
│   └── <서류철>/  *.pdf (377개) + *.txt (377개, PDF와 1:1 사이드카)
├── 희망브리지 문서전자화(2025.04) index.db3   # 88 MB SQLite — 스캐너 산출 인덱스
├── 희망브리지 문서전자화(2025.04) 리스트.xlsx  # 26 KB 목록
├── images/준공식 행사 conv_png/            # PNG 32장 (테스트용)
└── vision_test/                            # 모델 비교 리포트 md 18개
```

### 1.2 PDF 규모 (검증)
- PDF **377개**, 총 **20 GB**
- 총 페이지 **27,754** (index.db3 `page_info` 기준 정확값)
- 파일당 페이지: 최대 361p(`1990 일반문서_1990.pdf`), 25~360p 분포
- 파일 크기 상위: 297MB(`원천징수_1969~1971.pdf`), 231MB, 224MB, 215MB …
- 크기/페이지 비율 ≈ **1.1 page/MB** (≈ 0.75MB/page, 스캔 이미지 PDF)
- 서류철 카테고리 분포: 회계장부1 107, 회계장부2 106, 일반문서 64, 회계장부 47,
  원천세 23, 갑근세 기타서류(적립금) 16, 복지자금 14
- 문서 연도 범위: **1965 ~ 2005**

### 1.3 사이드카 `.txt` (377개) — 스캐너 OCR 원시 덤프
형식이 사람이 읽을 수 없는 블록 덤프:
```
2
page : 0, block : 0
1
page : 0, block : 1
```
→ 텍스트 본문 품질 낮음. **직접 임베딩 소스로 부적합**.

### 1.4 `index.db3` — 고가치 메타데이터 + 블록 OCR (검증)
- 인코딩: **CP949(EUC-KR)**. `sqlite3` CLI로 읽으면 깨짐 → Python `text_factory=lambda b: b.decode('cp949','replace')` 필요.
- 테이블:
  - `headers(id, value)` → 컬럼 라벨:
    `header_3=서류철명, header_4=서류철 생산연도, header_5=문서명, header_6=문서 생산연도,`
    `header_7=작성자, header_8~10=비고1~3, header_11=file_path`
  - `index_info` — **377행 = PDF 1:1 메타데이터**
    예: `갑근세 기타서류(적립금) | 1994~1999 | 1994 원천징수 영수철 | 1994 | 전국재해대책협의회 | …`
  - `page_info(file_name, page_no, block_no, block_text)` — **694,511 블록 / 27,754 페이지**
    - 블록 텍스트 평균 **25.6자**, 최대 1,770자 → 바운딩박스 단위 파편
    - 실제 내용은 유의미함:
      `"징수 1.사업자등록번호 1 0 5 8 2 0 0 4 2 7 2.법인명 (상호) (사)재해구호협회 3.대표자(성명) 최학래"`
    - `file_name`은 `희망브리지 문서전자화(2025.04)\<서류철>\<파일>.pdf` 형태(백슬래시)
- 활용 가치: **문서 메타데이터(서류철명/문서명/연도/작성자)**를 Dify 문서 메타데이터로 주입 →
  메타데이터 필터 검색 + citation 표시 개선. 페이지 인벤토리·진행률 산출의 기준값.

### 1.5 `~/Downloads/docu_conv_jpeg/` — 선행 추출 이미지
- JPEG **36장** (`준공식 행사_1.jpeg` ~ `_35.jpeg` + `준공식 행사_1 중간.jpeg`), 총 19 MB
- 상태 파일: `processed_hashes.jsonl` (완료 해시 기록), `failed_*.log` 3개(모두 0바이트=실패 없음)
- **이 폴더는 단일 문서('준공식 행사') 1건의 페이지 이미지**임. 377개 PDF 전체와 무관한 파일럿 샘플.

---

## 2. 실행 환경 (검증)

### 2.1 Docker (모두 Up)
```
dify-api-1            langgenius/dify-api:1.16.0-rc1        healthy
dify-worker-1         langgenius/dify-api:1.16.0-rc1
dify-worker_beat-1 / dify-api_websocket-1
dify-web-1            langgenius/dify-web:1.16.0-rc1
dify-plugin_daemon-1  langgenius/dify-plugin-daemon:0.6.3-local
dify-db_postgres-1    postgres:15-alpine   healthy
dify-weaviate-1       semitechnologies/weaviate:1.27.0
dify-redis-1 / nginx / ssrf_proxy / sandbox / local_sandbox / agent_backend
```
- api·worker는 3일 전 재시작(=코드 마운트 여부 확인 필요), web은 10일 전 기동.

### 2.2 디스크 — ⚠️ 최대 제약
```
/System/Volumes/Data  460Gi total / 421Gi used / 16Gi avail (97%)
```
**남은 공간 16 GB.** 27,754 페이지를 300dpi JPEG로 일괄 변환하면 (추정) 20~60 GB 필요 → **불가능**.
반드시 스트리밍(페이지 단위 변환 → OCR → 즉시 삭제) 설계여야 함.

### 2.3 Ollama (검증) — ⚠️ 로컬 비전 모델 없음
```
qwen3.5:cloud, qwen3.5:397b-cloud, kimi-k2.7-code:cloud, deepseek-v4-pro:cloud,
glm-5.2:cloud, gemma4:31b-cloud, deepseek-v3.2:cloud, deepseek-v3.1:671b-cloud   ← 전부 :cloud
qwen3-embedding:8b (4.7 GB)  ← 유일한 로컬 모델(임베딩용)
ollama ps: 실행 중 모델 없음
```
→ **VLM 추론은 전부 Ollama Cloud 경유**. 로컬 GPU 제약은 없으나 네트워크 지연·쿼터·레이트리밋에 종속.
→ 임베딩은 로컬 `qwen3-embedding:8b`로 처리 가능(추정: 현재 데이터셋이 이걸 사용).

### 2.4 컨테이너 코드 반영 방식 — ⚠️ 소스 마운트 없음 (검증)
```
dify-api-1  image=langgenius/dify-api:1.16.0-rc1 (prebuilt)
            mounts: docker/volumes/app/storage -> /app/api/storage   ← 소스 코드 마운트 없음
dify-web-1  image=langgenius/dify-web:1.16.0-rc1,  mounts: (없음)
dify-plugin_daemon-1  mounts: docker/volumes/plugin_daemon -> /app/storage
docker-compose.override.yaml = plugin_daemon 5002 포트 노출만
```
→ **`api/`, `web/` 로컬 수정은 현재 서비스에 전혀 반영되지 않은 상태.** 반영 경로 선택지:
  - (A) `docker compose build api web` 후 재기동 — 정석이나 **디스크 16GB 여유에서 web 빌드 위험**
  - (B) api는 수정 `.py`만 bind mount로 덮어쓰기(경량, 즉시). web은 Next.js 빌드 필요해 mount 불가
  - (C) web은 로컬 `pnpm dev` 구동(node_modules 설치 필요, 추정 1~2GB)
→ 계획서에서 디스크 예산과 함께 명시적으로 선택할 것.

### 2.5 PDF 처리 도구 — 현재 전무 (검증)
```
pdftoppm/pdfinfo/mutool/qpdf/gs/magick/convert : 전부 MISSING
python3: fitz(PyMuPDF)/pypdf/pdf2image/PIL     : 전부 MISSING
brew: /opt/homebrew/bin/brew (사용 가능)
```
→ PDF→이미지 경로를 쓰려면 도구 설치가 선행 작업. 설치 위치 선택지: 호스트(brew poppler / pip PyMuPDF)
   또는 플러그인 컨테이너 내부(requirements.txt에 PyMuPDF 추가). **플러그인이 PDF를 직접 받는 설계라면
   플러그인 쪽에 PyMuPDF를 넣는 것이 파이프라인 단순화에 유리** (판단은 계획 단계에서).

### 2.6 데이터셋 현황 (검증)
```
id=20087ab8-8e76-4f75-bfc8-88a24f4fd73c  name="Untitled 2"
indexing_technique=high_quality  runtime_mode=rag_pipeline  chunk_structure=text_model
documents=33  segments=152
```
- 빈 데이터셋 2개(030e3900, a6c005e3)는 이전 세션에서 삭제 완료.
- 에이전트 앱 "희망사다리"(`/agent/knnsy0VMJudIZGdu`)의 `retriever_resource.enabled=true` (DB 직접 UPDATE, 사용자 승인)

### 2.7 파이프라인 그래프 (검증)
데이터셋 `20087ab8…`의 workflow 노드:
```
1784815360101  datasource        "File"
1784815369680  tool              "VLM OCR"
1784825060267  tool              "General Chunker"
knowledgeBase  knowledge-index   "기술 자료"
```
→ `File → VLM OCR → General Chunker → knowledge-index`. **PDF-to-Image 노드는 없음**(이미지 전용 경로).

---

## 3. 코드 자산 (검증)

### 3.1 VLM OCR 플러그인 소스 — **정본 위치**
`/Users/hanchangpyo/Documents/proj/dify/docker/volumes/plugin-daemon/vlm_ocr_plugin/` (git untracked)
```
manifest.yaml           version: 0.1.1  (meta.version 0.0.1, python 3.12, memory 256MB)
provider/vlm_ocr.{py,yaml}
tools/vlm_ocr.py        765 lines
tools/vlm_ocr.yaml      파라미터 정의
tools/prompts.py        187 lines
tests/test_vlm_ocr.py, tests/test_run_batch.bats
scripts/run_batch.sh    9.4 KB  ← 배치 러너
scripts/run_pipeline.sh 5.2 KB  ← 단건 러너
scripts/compress_image.py 3.4 KB
scripts/.env            (실제 키 설정됨 — 값 노출 금지)
vlm_ocr_plugin.difypkg  패키징 결과물
README.md / DECISIONS.md / PIPELINE_SETUP_GUIDE.md / PROMPT_DESIGN.md
batch_run.log, install.log, scripts/batch_run_20260730_011448.log (46 KB)
```
⚠️ 혼동 주의: `docker/vlm_ocr_plugin/`(하이픈 없는 경로)에는 `DECISIONS.md`,`PIPELINE_SETUP_GUIDE.md`
**문서 사본만** 존재. 코드는 위 `volumes/plugin-daemon/vlm_ocr_plugin/`이 정본.

### 3.2 도구 파라미터 (tools/vlm_ocr.yaml, 검증)
| name | type | required | default | 비고 |
|---|---|---|---|---|
| `image_file` | file | ✅ | — | **이미지 단일 파일만 입력** |
| `prompt` | string | ❌ | — | 추가 지침 |
| `model` | string | ❌ | — | provider 기본값 오버라이드 |
| `ollama_base_url` | string | ❌ | — | `http://host.docker.internal:11434` 권장 |
| `download_mode` | select(auto/blob/url) | ✅ | auto | 파이프라인에선 **blob 고정** 권장 |
| `include_summary` | boolean | ❌ | false | `[부가 정보 요약]` 섹션 추가(표 셀 값 자연어화, 도장/서명/레이아웃 보강) |
| `enable_thinking` | boolean | ❌ | false | 추론 켜고 thinking은 폐기, 최종답만 반환 |

output_schema: `{result: string}`

### 3.3 배치 러너 `scripts/run_batch.sh` (검증)
- 대상: `IMAGE_DIR`의 `*.{jpg,jpeg,png}` (기본 `~/Downloads/docu_conv_jpeg`)
- 흐름: sha256 중복 스킵 → `compress_image.py`(long side 4096, 10MB 제한) →
  `POST /v1/datasets/pipeline/file-upload` → `POST /v1/datasets/{id}/pipeline/run`
  (`datasource_type=local_file`, `datasource_info_list=[{reference: file_id, name}]`) →
  `GET /v1/datasets/{id}/documents?keyword=<name>` 폴링
- 특성: **완전 순차 처리**(병렬 없음), `MAX_RETRIES=3`, `POLL_TIMEOUT=4200s`,
  실패 시 문서 삭제 후 재시도, 성공 시 `processed_hashes.jsonl` 기록
- ⚠️ PDF 미지원. 27,754 페이지에는 처리량이 근본적으로 부족(§5 참조).

### 3.4 Dify 코어 수정 (git 미커밋, 브랜치 `fix/rag-pipeline-empty-dataset-chunk-structure`)
커밋됨(2개, push 대기):
```
4df88f0944 fix(api): handle null chunk_structure defensively in index processor and pipeline generator
4dd0d547f2 fix(api): set chunk_structure default when creating empty RAG pipeline dataset
```
미커밋 수정(citation 미리보기 기능):
```
api/services/dataset_service.py                          LOCAL_FILE 허용 + reference 키 인식,
                                                         get_document_download_url(as_attachment=)
api/controllers/console/datasets/datasets_document.py    ?as_attachment=false 지원
api/tasks/rag_pipeline/priority_rag_pipeline_run_task.py (내용 확인 필요)
web/app/components/base/chat/chat/citation/popup.tsx     local_file 다운로드 + 이미지 썸네일
web/service/datasets.ts, web/service/knowledge/use-document.ts, web/models/datasets.ts
web/i18n/*/common.json  (23 locale)  chat.citation.loadingPreview 키
```
검증 상태: 백엔드 syntax OK. 프론트는 로컬에 node_modules/pnpm 없어 lint/tsc **미실행**.

---

## 4. 미완료 작업 (이전 세션 인계)

1. `api`·`web` 컨테이너 재빌드/재기동 → citation 수정 반영
2. 실동작 확인: `/agent/knnsy0VMJudIZGdu`에서 jpeg 질의 → citation 패널 썸네일+다운로드
3. 프론트 테스트 `web/app/components/base/chat/chat/citation/__tests__/popup.spec.tsx` 추가
4. (선택) 업스트림 PR: citation local_file 지원
5. `chunk_structure` 폴백 2커밋 push/PR (수동 대기)

---

## 5. 규모 산술 — 계획 시 반드시 반영

| 항목 | 값 |
|---|---|
| 총 페이지 | **27,754** |
| 현재 처리 완료 | 33 문서(=33 페이지 이미지) / 152 세그먼트 → **0.12%** |
| 관측 처리 속도 | 문서당 ~40초 (include_summary + enable_thinking, 순차) |
| 순차 전체 소요 | 27,754 × 40s ≈ **308 시간 ≈ 12.8일** |
| 디스크 여유 | **16 GB** (전체 페이지 JPEG 일괄 변환 불가) |
| VLM 위치 | Ollama **Cloud** (레이트리밋/쿼터 미확인 — 반드시 실측) |

→ 계획서는 최소한 다음을 다뤄야 함: 병렬화 한계 실측, 우선순위 기반 단계적 적재(예: 서류철/연도별),
스트리밍 PDF→페이지 변환+즉시 삭제, 중단·재개(idempotent), 실패 격리, 진행률 관측.

---

## 6. 참조

- Dify 공식 문서: https://docs.dify.ai/en/home (지식 파이프라인 / Service API / 플러그인 개발)
- 프로젝트 규약: `AGENTS.md`, `api/AGENTS.md`, `web/AGENTS.md`
- 백엔드 CLI는 `uv run --project api <cmd>`
- 자율 결정 기록은 `DECISIONS.md`에 계속 추가
