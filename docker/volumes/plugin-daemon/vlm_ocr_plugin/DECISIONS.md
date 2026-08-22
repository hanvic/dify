# VLM OCR Plugin Design Decisions

This document records the key architecture and design decisions behind the `vlm_ocr_plugin` Dify tool plugin.

## 1. Local Ollama instead of a managed vision API

**Decision**: The plugin calls a local Ollama server rather than a cloud vision API.

**Rationale**:
- Keeps document images inside the deployment boundary, which is important for sensitive enterprise documents in Korea.
- Avoids per-request cloud costs and network egress during iterative workflow development.
- Ollama supports a growing set of vision-capable open-weight models and exposes a simple HTTP API.

**Trade-offs**:
- Operators must install and maintain Ollama and pull the correct vision model.
- Image processing and inference happen on the host, so GPU/CPU capacity must be planned.

## 2. Provider credentials are consumed by the tool, not validated at setup time

**Decision**: `VlmOcrProvider._validate_credentials` is a no-op. Actual credential values are read at tool invocation inside `tools/vlm_ocr.py`.

**Rationale**:
- The provider layer only defines the credential schema. Validation against a live Ollama server would require network access and a known model at setup time, which can fail in air-gapped or template deployments.
- Errors are surfaced as user-facing tool messages when the first OCR call runs, with clear Korean messages for timeout, connection failure, missing model, and invalid response format.

**Trade-offs**:
- Misconfigured credentials are caught later, at first use. This is acceptable because the setup UI clearly labels each field and the error messages are explicit.

## 3. Tool accepts per-invocation overrides for model and base URL

**Decision**: In addition to provider credentials, the tool exposes `model` and `ollama_base_url` parameters that override the provider defaults for a single call.

**Rationale**:
- Workflows can route different document types to different models without creating multiple provider instances.
- Enables quick experiments (e.g. swapping to a faster or higher-quality model) without changing global credentials.

## 4. Images are resized before being sent to Ollama

**Decision**: The tool resizes the image so that the longer side is at most 2048 pixels while preserving aspect ratio.

**Rationale**:
- Large scans can exceed Ollama context and memory limits, especially with high-resolution A4 documents.
- Resizing reduces base64 payload size and inference latency without materially hurting OCR quality for document images.
- Transparent images are converted to PNG; opaque images are saved as high-quality JPEG.

## 5. Raw base64 without a data URI prefix

**Decision**: The image is encoded as raw base64 and passed to Ollama in the `images` field without a `data:image/...;base64,` prefix.

**Rationale**:
- Ollama's `/api/chat` endpoint accepts raw base64 strings in the `images` array.
- Avoiding the prefix keeps the request payload smaller and avoids model confusion from mixed content in the message text.

## 6. Korean-first prompt design with an English meta-rule

**Decision**: The default OCR prompt is written primarily in Korean but starts with a short English meta-rule that instructs the model to output pure markdown in Korean.

**Rationale**:
- VLMs often anchor on the first line of a prompt. The English meta-rule is concise and less ambiguous than a translated version for models trained primarily on English instructions.
- The detailed extraction rules are in Korean because the target output language is Korean, which improves term accuracy and natural ordering.

For the full prompt design, see [PROMPT_DESIGN.md](./PROMPT_DESIGN.md).

## 7. Markdown-only output

**Decision**: The prompt forbids HTML, JSON, XML, and code fences and requests pure markdown.

**Rationale**:
- Downstream RAG and workflow nodes consume markdown reliably.
- Tables can be represented as GitHub-flavored markdown tables, which are compact and widely parsed.

## 8. No signature verification for headless installs

**Decision**: `install_plugin.sh` uploads the package with `verify_signature=false`.

**Rationale**:
- Local development packages are not signed by the Dify marketplace. Requiring signature verification would block the headless install workflow.
- Production deployments that need signed plugins can change this flag or install through the marketplace instead.

## 9. Headless helper scripts live outside the packaged plugin

**Decision**: `scripts/install_plugin.sh`, `scripts/run_pipeline.sh`, and `scripts/.env.example` are excluded from the `.difypkg` archive.

**Rationale**:
- The packaged plugin should only contain runtime artifacts required by the plugin daemon.
- Helper scripts are host-side operational tools and should not be copied into the container as part of the plugin package.

## 10. Tenant ID is resolved from the application database

**Decision**: `install_plugin.sh` queries the `tenants` table in the main Dify Postgres database rather than requiring the user to look it up manually.

**Rationale**:
- The plugin daemon management API is tenant-scoped. A typical single-tenant Dify deployment has one tenant, so fetching the oldest tenant removes a manual step.
- Multi-tenant deployments can override `TENANT_ID` in the environment if needed.

## 11. PDF→페이지 스트리밍 처리 (디스크 안전)

**결정**: PDF를 한 페이지씩 렌더링→압축→업로드→삭제하는 스트리밍 방식 채택.

**근거**:
- 디스크 여유 16GB에서 27,754 페이지를 일괄 변환하면 최소 20GB 필요 → 불가능.
- 동시에 디스크에 존재하는 임시 이미지는 최대 1개(~250KB).
- `pdf_to_pages.py`가 PyMuPDF로 렌더링: DPI=200, long side ≤2048px, adaptive JPEG quality.
- 평균 168KB, 최대 250KB로 검증 완료.

## 12. 이미지 압축 목표: 평균 ≤200KB (REVIEW 반영)

**결정**: REVIEW.md BLOCKER 해소를 위해 기존 4096px/quality 95 → 2048px/quality 70으로 변경.

**근거**:
- 기존 업로드 이미지 평균 553KB → 27,754 페이지 시 14.6GB 영구 저장으로 디스크 초과.
- 2048px/quality 70 설정 시 실측 평균 168KB → 27,754 × 168KB = 4.4GB.
- OCR 품질: 2048px에서도 문서 텍스트 충분히 판독 가능 (실증 완료).

## 13. 파이프라인 VLM 설정 최적화 (REVIEW 반영)

**결정**: `include_summary=false`, `enable_thinking=false`로 DB 직접 UPDATE.

**근거**:
- REVIEW 필수수정 #2: 현재 40s/page → 설정 변경 후 16~22s VLM + 파이프라인 오버헤드 포함 54~60s.
- 총 처리시간 308시간 → ~130시간 (순차 기준) 절감.
- summary/thinking은 배치 임베딩 품질에 비해 속도 비용이 너무 큼.

## 14. 기존 데이터셋에 문서 추가 (별도 데이터셋 미생성)

**결정**: 기존 데이터셋 `20087ab8...`에 계속 추가. 새 데이터셋 생성 후 삭제.

**근거**:
- 기존 데이터셋에 VLM OCR 파이프라인이 이미 구성됨.
- 새 데이터셋은 파이프라인이 없어 별도 설정 필요 → 복잡도 증가.
- 기존 33문서와 동일한 검색 공간에 있어야 RAG 챗봇이 전체 문서를 통합 검색 가능.

## 15. Service API에도 as_attachment 파라미터 추가

**결정**: Console API뿐 아니라 Service API의 document download 엔드포인트에도 `as_attachment=false` 지원 추가.

**근거**:
- 배치 스크립트와 외부 연동은 Service API(Bearer token)를 사용.
- Citation 미리보기는 Service API 경유로도 작동해야 완전함.

## 16. API 코드 반영을 bind mount로 처리 (이미지 재빌드 대신)

**결정**: `docker-compose.override.yaml`에 수정된 .py 파일을 read-only bind mount.

**근거**:
- 디스크 16GB에서 Docker 이미지 빌드(~3GB 임시 사용)는 위험.
- Python 파일은 런타임 해석이므로 bind mount로 즉시 반영 가능.
- web (Next.js)은 빌드 필수라 bind mount 불가 → 네트워크 이슈로 빌드도 불가. 별도 작업 필요.

## 17. 병렬 러너를 Python으로 구현 (bash 대신)

**결정**: `run_batch_parallel.py`를 Python `concurrent.futures.ThreadPoolExecutor`로 구현. 기존 `run_batch_v2.sh`는 보존.

**근거**:
- bash에서 병렬 제어(flock, 상태파일 경합 방지, 에러 핸들링)가 복잡하고 취약함.
- Python의 threading.Lock + fcntl.flock으로 깨끗한 동시성 제어 가능.
- requests 라이브러리로 HTTP 에러 처리가 명시적이고 안전함.
- 기존 bash 러너 코드와의 호환성 유지 (processed_pages.jsonl 포맷 동일).

## 18. 최적 동시성 = 10 선정

**결정**: CONCURRENCY=10, CELERY_WORKER_AMOUNT=10.

**근거** (실측):
- C=4: 14.8초/p, C=6: 14.6초/p, C=10: 8.6초/p
- C=10에서 Worker 완전 활용, Ollama Cloud 429 없음, 502 없음.
- Worker 수(10)와 동시성(10)의 1:1 매칭이 최적.
- 더 높은 동시성은 Worker 큐잉 과포화 위험.

## 19. Docker 미사용 리소스 정리

**결정**: `docker system prune -f --volumes`, `docker builder prune -f`, `docker image prune -a -f` 실행.

**근거**:
- 디스크 4.1GB까지 감소하여 배치 진행 불가 상태였음.
- Docker에 24GB의 미사용 이미지/볼륨/빌드캐시가 있었음.
- 정리 후 25GB 확보 — 전체 배치 (8.1GB 추가 예정) 완주 가능.

## 20. POLL_INTERVAL 5초 → 3초

**결정**: 인덱싱 폴링 간격을 5초에서 3초로 축소.

**근거**:
- 평균 1~2회 폴링에서 완료 감지 → 2~4초/페이지 절감.
- API 부하: 10동시 × 1req/3초 = 3.3 req/s → 무시할 수준.

## 21. 중복 문서 정리 (이전 테스트 잔재)

**결정**: 동시성 6 테스트에서 502 발생으로 생긴 중복 문서 17건 DB 직접 삭제 (가장 오래된 것 유지).

**근거**:
- 같은 이름의 문서가 4~5건씩 중복 존재 — 검색 품질 저하.
- 가장 먼저 생성된 건 = 정상 완료건을 유지하고 나머지 삭제.
- 이는 자신이 배치 검증으로 만든 실패 문서 정리에 해당 (금지사항 예외).

## [2026-07-31] Fail-Fast: 오류를 예외로 전파 (v0.1.2)

**Decision**: Ollama 호출 실패(HTTP 오류, 429, 타임아웃, 빈 결과)를 `create_text_message()`로 반환하지 않고 예외로 raise하여 Dify SDK가 문서를 error 상태로 처리하게 함.

**Rationale**:
- v0.1.1에서 오류 문자열이 정상 OCR 결과로 취급되어 13,566건의 문서가 "Ollama 서버에서 오류 상황을 반환했습니다."라는 텍스트로 오염됨.
- Dify plugin SDK 관례: `_invoke()`에서 unhandled exception이 발생하면 상위 파이프라인 노드가 error 상태가 됨. 이것이 도구 실패를 전파하는 정석 방법.
- 커스텀 예외 계층으로 상위에서 구별 가능: `OllamaRateLimitError`(429 쿼터), `OllamaServerError`(5xx/연결/타임아웃), `OcrContentQualityError`(빈/짧은 결과).

**Trade-offs**:
- 이미지 로딩 단계의 ValueError(손상된 이미지 등)도 이제 예외로 전파됨. 이는 의도적: 어떤 이유든 OCR이 실패하면 문서가 error 상태가 되는 것이 오류 텍스트가 임베딩되는 것보다 낫다.
- 본문 품질 가드(10자 최소)가 일부 정상적으로 매우 짧은 결과를 거부할 수 있으나, 문서 이미지에서 10자 미만 OCR 결과는 사실상 모델 실패.

**결과**: 429 상태에서 파이프라인 실행 시 문서 Status=`error`, Segments=0 확인됨 (이전: Status=`completed`, Segments=1 with error text).

## [2026-07-31] 배치 러너 가드레일: 연속 실패 중단 + 429 즉시 중단

**Decision**: 배치 러너(`run_batch_parallel.py`)에 연속 5회 실패 자동 중단, 429 즉시 중단(재시도 없음), 세그먼트 오염 패턴 검증 추가.

**Rationale**:
- 14,391페이지가 동일 에러로 처리되는 사고를 구조적으로 방지.
- 429는 시간 경과 또는 업그레이드 없이는 해결 불가하므로 재시도가 무의미.
- 연속 실패는 시스템 레벨 문제(쿼터, 서버 다운 등)를 의미하므로 조기 중단이 합리적.


## [2026-07-31] PDF 지원 추가 (v0.1.3)

**Decision**: VLM OCR 플러그인이 PDF 파일을 직접 처리하도록 확장. PDF 페이지를 하나씩 렌더링→JPEG 압축→OCR 후 페이지 구분자(`## 📄 p.N`)를 넣어 하나의 마크다운으로 합침.

**Rationale**:
- 기존에는 이미지 MIME만 처리하여 datasource 노드가 PDF를 허용해도 VLM OCR에서 실패.
- PDF를 페이지 단위로 스트리밍 처리하여 메모리 폭증 방지 (한 번에 한 페이지만 메모리에).
- 페이지 구분자는 General Chunker가 `##` 헤더를 청크 경계로 인식하므로 citation에서 페이지 번호 추적 가능.

**설계 선택과 근거**:
1. **순차 처리 (병렬 아님)**: Ollama Cloud 무료 티어에서 쿼터 소모를 제어하기 어려우므로 순차. 120페이지/3600초 예산 내 처리 가능.
2. **max_pages=120 기본값**: 3600초 타임아웃 / 30초(worst case) = 120. 초과 시 명시적 예외.
3. **메모리 256→512MB**: PyMuPDF open(~50MB) + 렌더링(~30MB) + Pillow(~50MB) + 기존 코드 + 여유.
4. **UPLOAD_FILE_SIZE_LIMIT=50MB**: nginx 100M 상한 대비 보수적. 일상 PDF(1~30MB) 커버.
5. **페이지 구분자**: `## 📄 p.N` 형식. 근거: `##`이 chunker의 자연 분할점이 되고 이모지로 시각적 구별 가능.

**Trade-offs**:
- 120페이지 PDF는 순차 처리 시 최대 60분 소요. 이보다 긴 PDF는 배치 스크립트 사용 필요.
- PyMuPDF 의존성 추가로 플러그인 이미지 크기 증가(~15MB).
- 순차 처리는 느리지만 쿼터 안전하고 메모리 예측 가능.

**업로드 제한 변경**: `docker/.env`에 `UPLOAD_FILE_SIZE_LIMIT=50` 추가 (이전: 미지정=15MB 코드 기본값). 되돌리기: 해당 행 삭제 또는 주석 처리.

## Blank Page Detection (v0.1.4, 2026-08-04)

**Decision**: 3-way classification: Success / Blank Page / Error.

**Problem**: Blank pages in scanned PDFs caused `OcrContentQualityError`, which
incremented the consecutive failure counter. 6+ consecutive blank pages triggered
the batch abort guard (MAX_CONSECUTIVE_FAILURES=5), halting the entire batch.
Blank pages and genuine errors (429, timeout) were indistinguishable.

**Solution**:
1. **Pre-detection** (image-based, before VLM): stddev<12 AND edge_mean<5.0 → skip VLM call.
   Calibrated on 500 real pages: 0% FP, 31.5% TP.
2. **Post-detection** (after VLM): Short response + blank keywords → `BlankPageError`.
3. **New exception hierarchy**: `BlankPageError` (not a failure) vs `OcrContentQualityError` (genuine failure).
4. **Batch runner**: `add_blank()` does NOT increment `consecutive_failures`.

**Trade-offs**:
- Conservative threshold means only 31.5% of blank pages are caught pre-VLM.
  The rest still consume a VLM call but are caught post-VLM (no document created).
- Threshold tuned to a specific scan quality; may need recalibration for different scanners.
- Added ~50ms per page for PIL image metrics computation (negligible vs 30s VLM call).

**Configurable**: `BLANK_PAGE_ACTION` env var: "skip" (default) or "error".
Reprocess switch: `--reprocess-blanks` to re-run previously blanked pages.
