# Citation & Metadata Report

## 1. 메타데이터 주입 결과

### 생성된 필드

| 필드명     | 타입   | ID                                   |
| ---------- | ------ | ------------------------------------ |
| 서류철명   | string | 960c277c-67b0-49cf-9a48-844f40fb0b3f |
| 문서명     | string | 202fefb2-a0b7-4505-9086-86d62b7ea42f |
| 생산연도   | string | 73516d1f-80c6-40f2-b5b7-e7100d4e917a |
| 작성자     | string | 82e365be-a2a0-4a1c-a131-17881b4e9302 |
| 페이지번호 | number | 0435a2f1-ec34-4a69-8e1c-15360a6a8ed4 |

### 주입 결과

- 총 문서: 79건 (배치 진행 중이므로 계속 증가)
- 매핑 성공: 46건
- 매핑 실패: 33건 (대부분 `준공식 행사_N.jpeg` 등 테스트 문서 — index.db3에 없음)
- 오류: 0건

### 검증 예시

```
문서: 회계장부_2002~2004 기타장부_금전출납부 2000 복지자금_2000_p11.jpeg
  서류철명: 회계장부
  문서명: 금전출납부 2000 복지자금
  생산연도: 2000
  페이지번호: 11
```

### 스크립트

- `scripts/inject_metadata.py` — 독립 실행, idempotent, 재실행 안전
- NFC 유니코드 정규화로 macOS NFD 파일명 호환 처리
- `--dry-run`, `--verify-only` 옵션 지원

---

## 2. Web 반영 방법 및 되돌리기

### 방법: 호스트 dev 서버 + nginx 프록시 변경

1. `pnpm install --filter "dify-web..."` → 호스트에 node_modules 설치 (약 2.4GB)
2. `pnpm --filter dify-web dev` → 호스트 3000포트에 Next.js dev 서버 기동
3. `docker/nginx/conf.d/default.conf.dev` 생성 → web upstream을 `host.docker.internal:3000`으로 변경
4. `docker-compose.override.yaml`에 nginx 볼륨 마운트 추가
5. `docker compose up -d nginx` → nginx 재생성

### 되돌리기 방법

```bash
# 1. dev 서버 종료
kill $(pgrep -f "next dev")

# 2. override에서 nginx 볼륨 제거
# docker/docker-compose.override.yaml에서 nginx: volumes: 섹션 삭제

# 3. nginx 재시작 (원래 이미지 설정으로 복원)
cd docker && docker compose up -d nginx

# 4. (선택) node_modules 삭제
rm -rf web/node_modules node_modules
```

### 디스크 사용량

- 설치 전: 16.3GB free
- 설치 후: 13.8GB free (5GB 하한 준수)

---

## 3. Citation 동작 검증 증거

### API 레벨 검증 (사람 없이)

#### 문서 다운로드 URL 획득

```bash
curl -s "http://localhost/v1/datasets/20087ab8.../documents/47a3a2bc.../download?as_attachment=false" \
  -H "Authorization: Bearer dataset-ZGK1qqolrOWLGyZZc8XSoLWp"
```

**응답:**

```json
{
  "url": "http://192.168.200.107/files/d48a3eab-6cbc-47e0.../file-preview?timestamp=1785437519&nonce=...&sign=..."
}
```

#### 이미지 바이트 다운로드

```bash
curl -s -o /dev/null -w "HTTP_CODE:%{http_code} CONTENT_TYPE:%{content_type} SIZE:%{size_download}" "$URL"
```

**결과:**

```
HTTP_CODE:200 CONTENT_TYPE:application/octet-stream SIZE:240601
```

#### Web Dev 서버 접근

```
curl -s -o /dev/null -w "%{http_code}" http://localhost/signin → 200
```

### data_source_type 확인

모든 적재 문서의 `data_source_type`이 `local_file`로 설정되어 있음 → 프론트엔드 citation 팝업의 `isDownloadable` 조건 충족.

---

## 4. 프론트엔드 테스트 실행 결과

```
$ pnpm --filter dify-web test -- --run app/components/base/chat/chat/citation/__tests__/popup.spec.tsx

 ✓ app/components/base/chat/chat/citation/__tests__/popup.spec.tsx (7 tests) 257ms

 Test Files  1 passed (1)
      Tests  7 passed (7)
   Duration  1.02s
```

테스트 케이스:

1. ✅ 파일명과 트리거 버튼 렌더링
2. ✅ local_file 문서에 다운로드 버튼 표시
3. ✅ 팝오버 오픈 시 이미지 미리보기 fetch (asAttachment=false)
4. ✅ 미리보기 로드 중 상태 표시
5. ✅ URL 수신 후 이미지 미리보기 렌더링
6. ✅ 비-이미지 파일은 미리보기 fetch 안 함
7. ✅ notion 문서는 미리보기 fetch 안 함

---

## 5. 미완 사항

1. **메타데이터 팝업 노출**: citation 팝업에서 메타데이터(서류철/문서명/연도/페이지)를 표시하는 UI 변경은 **미구현**. 이유: Dify의 retriever_resource(citation) 응답에 `doc_metadata` 필드가 포함되지 않는 구조이며, 이를 추가하려면 api의 retriever 응답 스키마를 변경해야 하고 다른 에이전트의 작업 범위와 충돌할 위험이 있음. 향후 api에서 citation 응답에 메타데이터를 포함하도록 확장한 뒤 프론트 팝업에 표시할 수 있음.

2. **배치 증분 메타데이터**: 배치 진행 중 신규 문서가 추가되면 `scripts/inject_metadata.py`를 다시 실행해야 함. Celery 후처리 hook으로 자동화 가능하나 현재는 수동.

3. **Web 운영 모드 복원**: 현재 dev 서버 모드로 동작 중. 프로덕션 배포 시 `docker compose build web` 또는 빌드 결과물 마운트로 전환 필요.

4. **Content-Type**: 이미지 다운로드 응답이 `application/octet-stream`으로 옴 (api의 FileService 구현). 브라우저에서 `<img src=...>`로 렌더링할 때는 문제 없음(바이너리가 JPEG이므로).
