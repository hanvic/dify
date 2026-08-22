# VLM OCR 파이프라인 설정 가이드

이 가이드는 JPEG/PNG 이미지를 입력받아 VLM OCR 플러그인으로 텍스트를 추출한 뒤, 추출된 마크다운 텍스트를 Dify 지식 베이스에 인덱싱하는 headless Dify 지식 파이프라인을 설정하는 방법을 설명합니다.

## 스크립트가 수행하는 작업

`scripts/run_pipeline.sh`는 Dify 서비스 API에 대해 다음 세 단계를 수행합니다:

1. **파일 업로드** — `POST /v1/datasets/pipeline/file-upload`
2. **파이프라인 실행** — `POST /v1/datasets/{dataset_id}/pipeline/run`
3. **인덱싱 상태 폴링** — `GET /v1/datasets/{dataset_id}/documents?keyword=<filename>`

## 사전 준비 사항

- VLM OCR 플러그인이 설치된 실행 중인 Dify 환경
- Dify에서 생성된 지식 베이스
- 최소 하나의 데이터소스(datasource) 노드를 포함하는 게시되거나 초안 상태의 지식 파이프라인
- 데이터셋 서비스 API 키
- 스크립트를 실행하는 호스트에 `curl`과 `jq` 설치

## 1단계: 지식 베이스 생성

1. Dify 콘솔을 엽니다.
2. **Knowledge** → **Create Knowledge**로 이동합니다.
3. 파이프라인을 편집할 수 있는 **Custom** 또는 원하는 템플릿을 선택합니다.
4. 임베딩 모델을 선택하고 **Create**를 클릭합니다.

URL(`datasets/<dataset_id>`)에서 데이터셋 ID를 확인하거나, 지식 베이스 설정에서 나중에 확인할 수 있습니다.

## 2단계: 지식 파이프라인 생성

1. 지식 베이스를 열고 **Pipeline** 탭으로 이동합니다.
2. **Upload File**에서 읽어 들이는 데이터소스 노드를 추가합니다.
3. 데이터소스 노드를 문서 추출기(document extractor)에 연결하거나, 인덱싱 노드에 직접 연결합니다.
4. 추출 단계에서 VLM OCR을 사용하려면, 파이프라인이 업로드된 이미지를 처리하고 청킹(chunking) 전에 마크다운 텍스트로 변환하는 위치에 **VLM OCR** 도구를 추가합니다.
   - **VLM OCR 도구 설정**:
     - `image_file`: 데이터소스 노드의 파일 출력을 연결
     - `ollama_base_url`: 로컬 Ollama 서버 주소를 `http://host.docker.internal:11434`로 설정
     - `download_mode`: **`blob`으로 명시 설정**합니다. Dify가 생성하는 내부 파일 URL은 네트워크/SSRF 제한으로 차단될 수 있으므로, Dify가 전달하는 파일 blob을 직접 사용합니다.
     - `think`: **`false`로 명시 설정**합니다. OCR 출력에 추론 과정이 필요하지 않으며, 일부 모델은 `think=true` 응답을 반환할 수 있습니다.
5. 파이프라인을 저장하고 선택적으로 게시(publish)합니다.

시작하려는 데이터소스 노드의 **node ID**를 기록해 두세요. 스크립트는 이 값을 `START_NODE_ID`로 전달합니다.

## 3단계: 서비스 API 활성화 및 API 키 생성

1. 지식 베이스 설정을 엽니다.
2. **API** 탭으로 이동합니다.
3. 서비스 API가 활성화되지 않았다면 활성화합니다.
4. 새 **Dataset API Key**를 생성하거나 기존 키를 복사합니다.

## 4단계: 환경 설정

1. 예제 파일을 복사합니다:

   ```bash
   cd docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts
   cp .env.example .env
   ```

2. `.env`를 편집하여 최소한 다음 변수를 채웁니다:

   ```bash
   DATASET_API_KEY=your_dataset_api_key_here
   DATASET_ID=your_dataset_id_here
   START_NODE_ID=your_datasource_node_id_here
   ```

3. 선택적 재정의 값:

   - `DIFY_API_BASE` — Dify 서비스 API의 기본 URL(기본값 `http://localhost/v1`)
   - `IS_PUBLISHED` — `true`면 게시된 파이프라인 실행, `false`면 초안 실행(기본값 `true`)
   - `RESPONSE_MODE` — `blocking` 또는 `streaming`(기본값 `blocking`)
   - `POLL_INTERVAL` 및 `POLL_TIMEOUT` — 인덱싱 상태 폴링 제어(기본값 각각 `5`, `300`)

## 5단계: 파이프라인 실행

JPEG 또는 PNG 파일 경로를 인자로 전달합니다:

```bash
./run_pipeline.sh /path/to/document.png
```

스크립트는 업로드된 파일 ID, 파이프라인 실행 응답을 출력한 뒤, 인덱싱이 완료될 때까지 문서 목록을 폴링합니다.

## 예상 출력

```text
[run_pipeline] Uploading document.png to http://localhost/v1/datasets/pipeline/file-upload
[run_pipeline] Uploaded file ID: <file_id>
[run_pipeline] Running pipeline for dataset <dataset_id>
[run_pipeline] Pipeline run response:
{
  ...
}
[run_pipeline] Polling document indexing status (timeout 300s)
[run_pipeline] Document <doc_id> status: parsing (display: ...)
[run_pipeline] Document <doc_id> status: indexing (display: ...)
[run_pipeline] Document <doc_id> status: completed (display: available)
[run_pipeline] Indexing completed.
```

## 문제 해결

### `401 Unauthorized`

- `DATASET_API_KEY`가 올바른지, 해당 지식 베이스에서 서비스 API가 활성화되어 있는지 확인하세요.

### `404 Not Found`

- `DATASET_ID`가 실제로 존재하는 지식 베이스와 일치하는지 확인하세요.
- `DIFY_API_BASE`가 올바른 Dify API 호스트를 가리키고 `/v1`을 포함하는지 확인하세요.

### `Pipeline run failed` 또는 출력 누락

- `START_NODE_ID`가 대상 파이프라인의 데이터소스 노드인지 확인하세요.
- 파이프라인이 저장(초안)되었거나 게시되었는지(`IS_PUBLISHED=true`) 확인하세요.
- API 서버 로그를 확인하세요: `docker compose logs -f api`.

### 인덱싱 시간 초과

- `.env`의 `POLL_TIMEOUT`을 늘리세요.
- 문서 목록을 수동으로 확인합니다:

  ```bash
  curl -H "Authorization: Bearer ${DATASET_API_KEY}" \
    "${DIFY_API_BASE}/datasets/${DATASET_ID}/documents?limit=10"
  ```

- API 서버 및 worker 로그에서 오류를 찾아보세요:

  ```bash
  docker compose logs -f api
  docker compose logs -f worker
  ```

## 고급: `.env` 없이 실행

모든 변수는 직접 보낼 수 있습니다:

```bash
export DATASET_API_KEY="..."
export DATASET_ID="..."
export START_NODE_ID="..."
./run_pipeline.sh /path/to/document.jpg
```
