# 브릿지 지식 파이프라인 구성 가이드

`vlm_ocr` 플러그인 설치 후 Dify 웹 UI에서 `브릿지` 지식 파이프라인을 구성하는 방법입니다.

---

## 1. 파이프라인 편집 페이지 열기

브라우저에서 아래 주소를 엽니다.

```
http://localhost/datasets/994b543a-ea2d-4790-8fb7-1a84a841bdb0/pipeline
```

기존 데이터셋 형식이면 **지식 파이프라인으로 변환** 프롬프트가 나타납니다. 안내에 따라 변환합니다.

---

## 2. 노드 추가 및 설정

### 2.1 Data Source 노드

- **유형**: File
- **허용 확장자**: `jpeg`, `jpg`, `png`

### 2.2 VLM OCR Tool 노드

- **Tool**: `vlm_ocr` 선택
- **입력 매핑**: `image_file` → Data Source 노드의 `file` 출력
- **인증 정보**:
  - `ollama_base_url`: `http://host.docker.internal:11434`
  - `ollama_model`: `qwen3.5:cloud`
  - `think`: `auto`

### 2.3 General Chunker 노드

- **Input content**: `{{#vlm_ocr_node.result#}}`
  - `vlm_ocr_node` 부분은 실제 VLM OCR 노드 ID로 교체합니다.

### 2.4 Knowledge Index 노드

- **Embedding model**: `qwen3-embedding:8b`
- **Chunk input**: General Chunker 노드의 `result` 출력

---

## 3. 노드 연결

아래 순서대로 에지를 연결합니다.

```
Data Source → VLM OCR → General Chunker → Knowledge Index
```

---

## 4. 저장 및 게시

1. **Save draft** 버튼을 눌러 초안을 저장합니다.
2. **Publish** 버튼을 눌러 파이프라인을 게시합니다.

---

## 5. 문서 업로드 및 파이프라인 실행

1. 데이터셋 문서 페이지로 이동합니다.
2. `~/Downloads/희망브리지/images/준공식 행사 conv_jpeg/` 경로의 JPEG/PNG 파일을 업로드합니다.
3. 업로드 후 파이프라인이 자동으로 실행됩니다.

인덱싱이 완료될 때까지 기다립니다.

---

## 6. 챗봇에서 검색 테스트

1. 새 챗봇 앱을 만들거나 기존 앱을 엽니다.
2. Retrieval 노드를 추가하고 `브릿지` 지식을 선택합니다.
3. 한국어 질문으로 검색 결과를 테스트합니다.

---

## 부록: VLM OCR 노드 ID 확인

파이프라인 캔버스에서 VLM OCR 노드를 클릭하면 오른쪽 설정 패널 상단에 노드 ID가 표시됩니다. 해당 ID를 `{{#vlm_ocr_node.result#}}`의 `vlm_ocr_node` 자리에 넣습니다.

---

## 부록: 로그 확인

### 플러그인 데몬 로그

```bash
docker logs -f docker-plugin_daemon-1
```

### Ollama 서버 로그

```bash
tail -f /Users/hanchangpyo/.ollama/logs/server.log
```

---

## 부록: 플러그인 디버그 로그 활성화

`plugin_daemon` 컨테이너에 다음 환경 변수를 추가하고 재시작합니다.

```
VLM_OCR_LOG=1
```

```bash
docker compose restart plugin_daemon
```
