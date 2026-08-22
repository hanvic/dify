# WEB_CITATION_REPORT.md

## 작업 요약

희망브리지 RAG 챗봇 citation 미리보기 기능을 web 프론트에 반영하고, 메타데이터를 모든 적재 문서에 자동 적용하는 작업 완료.

## (A) 프론트 코드 반영

### 시도한 경로와 결과

| # | 경로 | 결과 |
|---|------|------|
| 1 | `docker compose build web` | docker-compose.yaml에 build context 추가 후 시도. corepack이 `unofficial-builds.nodejs.org`에서 arm64 musl node 바이너리를 못 가져와서 실패 (네트워크 타임아웃). |
| 2 | 호스트 `pnpm build` + standalone 마운트 | macOS에서 빌드하면 Linux 바이너리 호환 불가. 스킵. |
| 3 | **호스트 `pnpm dev` + nginx upstream 전환** | ✅ 성공. |

### 최종 반영 방식

**호스트 Next.js dev 서버 (Turbopack) + nginx upstream 전환**

1. `dify-web-1` 컨테이너 중지
2. `docker/nginx/conf.d/default.conf`에서 `web:3000` → `host.docker.internal:3000`
3. 호스트에서 `pnpm dev` (포트 3000)
4. nginx reload

### 되돌리는 명령

```bash
# 1. 호스트 dev 서버 중지
pkill -f "next dev"

# 2. nginx 설정 원복
cd /Users/hanchangpyo/Documents/proj/dify/docker/nginx/conf.d
cp default.conf.original default.conf

# 3. web 컨테이너 재시작
cd /Users/hanchangpyo/Documents/proj/dify/docker
docker compose start web

# 4. nginx reload
docker compose exec nginx nginx -s reload

# 5. .env.local 원복 (옵션)
# cd /Users/hanchangpyo/Documents/proj/dify/web
# 원래 내용으로 복원
```

### pnpm check 결과

```
$ pnpm check
$ vp fmt --check && vp lint --quiet && pnpm lint:eslint
Checking formatting...
All matched files use the correct format.
Finished in 2629ms on 8576 files using 8 threads.
Found 2400 warnings and 0 errors.
Finished in 32.9s on 6936 files with 399 rules using 8 threads.
$ eslint --concurrency=auto
(no errors)
```

### 테스트 결과

```
$ npx vitest run app/components/base/chat/chat/citation/__tests__/popup.spec.tsx
✓ popup.spec.tsx (7 tests) 258ms
Test Files  1 passed (1)
Tests       7 passed (7)
```

### 검증 증거

```
# Web / route (via nginx) → 307 (redirect to signin = 정상)
curl -s -o /dev/null -w "%{http_code}" http://localhost/
→ 307

# /signin 페이지 → 200 + 전체 HTML
curl -sL http://localhost/signin -o /dev/null -w "%{http_code}"
→ 200

# 문서 다운로드 URL API (as_attachment=false) → 200 + URL
curl -s "http://localhost/v1/datasets/.../documents/.../download?as_attachment=false"
→ {"url":"http://localhost/files/.../file-preview?..."}

# 이미지 다운로드 → 200 + 181,393 bytes (JPEG magic: FF D8 FF E0)
curl -s -o /dev/null -w "%{http_code} %{size_download}" "$URL"
→ 200 181393

# 소스 파일 핵심 키워드 존재 확인
popup.tsx: IMAGE_EXTENSIONS(2), local_file(2), previewUrl(5), loadingPreview(1), useDocumentDownload(2)
i18n/en-US: "chat.citation.loadingPreview": "Loading preview…"
i18n/ko-KR: "chat.citation.loadingPreview": "미리보기 불러오는 중…"
```

### 사용자 수동 확인 절차

1. 브라우저에서 `http://localhost/signin` 접속, 로그인
2. Agent 앱으로 이동: `http://localhost/agent/knnsy0VMJudIZGdu`
3. 챗봇에 질문 입력 (예: "1994년 갑근세 관련 서류 보여줘")
4. 답변 하단의 citation 태그 (파일명 표시) 클릭
5. **기대 화면**: 팝업에 문서 이미지 미리보기 (JPEG 썸네일)가 표시됨
6. 이미지 클릭 시 새 탭에서 원본 이미지가 열림

---

## (B) 메타데이터 일괄 적용

### 스크립트

`scripts/apply_metadata.py` — 재현 가능한 Python 스크립트

```bash
cd docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts
export $(grep -v '^#' .env | xargs)
python3 apply_metadata.py [--dry-run] [--batch-size 50] [--force]
```

### 실행 결과

```
Total documents: 423
Successfully parsed: 423 (0 failures)
COMPLETE: updated=15, skipped=408, failed=0
```

- 408개: 이미 메타데이터 있음 (이전 배치/수동 작업으로 적용됨)
- 15개: 신규 문서에 메타데이터 적용 완료
- 배치 프로세스(PID 39766)로 새로 추가되는 문서는 이 스크립트를 재실행하면 자동 적용됨

### 메타데이터 필드

| 필드명 | ID | 타입 |
|--------|-----|------|
| 서류철명 | 960c277c-... | string |
| 문서명 | 202fefb2-... | string |
| 생산연도 | 73516d1f-... | string |
| 작성자 | 82e365be-... | string |
| 페이지번호 | 0435a2f1-... | number |

---

## 미완사항

1. **Docker 이미지 빌드**: arm64 macOS에서 Docker 빌드가 corepack/node musl 바이너리 다운로드 실패로 불가. 정식 배포 시에는 CI/CD 파이프라인(linux/amd64)에서 빌드하거나, 호스트에서 `pnpm build` 후 multi-stage copy 방식 사용 필요.
2. **프로덕션 빌드**: 현재 dev 모드(Turbopack)로 구동 중. 프로덕션 성능이 필요하면 `pnpm build && pnpm start`로 전환 (동일 nginx 설정으로 동작).
3. **Content-Type**: 파일 다운로드 API가 `application/octet-stream`을 반환. 브라우저에서 `<img>` 태그는 content-type 무관하게 렌더링하므로 기능적 문제 없음. 백엔드에서 mime-type 설정 개선 가능.
4. **배치 완료 후 메타데이터 재적용**: 배치(PID 39766)가 완료된 후 `python3 apply_metadata.py`를 한 번 더 실행하면 신규 문서에도 메타데이터 적용됨.

---

## 상태

- 배치 프로세스: PID 39766, 정상 실행 중
- 디스크 여유: 32GB (안전)
- Web 서비스: Next.js dev (PID 73741), 포트 3000
- Nginx: `host.docker.internal:3000` upstream으로 전환됨
