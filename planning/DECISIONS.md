# DECISIONS.md — 메타데이터·Citation 미리보기 에이전트 자율 결정

## 2026-07-31

### D1: 메타데이터 주입을 Service API(dataset-\* 토큰)로 수행

- **근거**: Console API는 encrypted password 인증이 필요하여 자동화 불가. Service API `/v1/datasets/{id}/metadata`와 `/v1/datasets/{id}/documents/metadata` 엔드포인트가 동일 기능을 제공하며, 기존 dataset API 토큰으로 인증 가능.
- **대안**: DB 직접 INSERT — 스키마 결합도가 높고 서비스 무결성 위험.

### D2: NFC 유니코드 정규화로 macOS NFD 파일명 문제 해결

- **근거**: Dify에 저장된 문서명이 macOS APFS의 NFD(자모 분리) 형태로 저장됨. index.db3의 CP949→NFC 변환 결과와 매칭하려면 doc_name에 `unicodedata.normalize('NFC', ...)` 적용 필요.

### D3: Web 반영을 호스트 dev 서버 + nginx upstream 변경으로 수행

- **근거**: `docker compose build web`은 네트워크 타임아웃 이력이 있고, 빌드 시 ~3GB 추가 디스크 사용. 호스트 dev 서버 방식은 디스크 추가 사용 2.4GB로 한도(5GB↑) 내에서 가능하며, 되돌리기가 간단(override 한 줄 + nginx 재시작).

### D4: 메타데이터를 citation 팝업에 노출하지 않음

- **근거**: Dify의 retriever_resource(citation) API 응답 구조에 `doc_metadata` 필드가 없음. 이를 추가하려면 `api/services/completion_service.py` 또는 retriever 계층 수정이 필요하며, 이는 다른 에이전트의 api 수정 범위와 충돌 위험이 있어 현 단계에서 보류.

### D5: file_path에서 키 매핑 시 단일 백슬래시(chr 92) 분할 사용

- **근거**: SQLite에 CP949로 저장된 `header_11` 값은 Python에서 디코딩 후 단일 백슬래시 문자(chr 92)를 구분자로 사용. Python 리터럴 `"\\"` = 1개 백슬래시.
