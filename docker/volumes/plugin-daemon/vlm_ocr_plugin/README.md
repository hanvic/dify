# VLM OCR Plugin

A Dify Tool Plugin that sends an image to a local Ollama vision model and returns extracted Korean document text as markdown.

이 플러그인은 이미지를 로컬 Ollama 비전 언어 모델(VLM)에 전송하고, 한국어 문서 텍스트를 마크다운 형식으로 추출하여 반환하는 Dify 도구 플러그인입니다. 스캔 문서, 영수증, 품의서, 공문 등에서 한글과 영문이 혼재된 텍스트, 표, 도장, 서명 영역을 구조화된 마크다운으로 변환하는 것을 목표로 합니다.

## File Structure

```
vlm_ocr_plugin/
├── README.md                 # This file
├── DECISIONS.md              # Architecture and design decisions
├── PIPELINE_SETUP_GUIDE.md   # Headless dataset pipeline setup guide
├── PROMPT_DESIGN.md          # OCR prompt design rationale
├── manifest.yaml             # Plugin manifest
├── main.py                   # Plugin entry point
├── requirements.txt          # Python dependencies
├── provider/
│   ├── vlm_ocr.yaml          # Provider declaration and credentials
│   └── vlm_ocr.py            # Provider implementation
├── tools/
│   ├── __init__.py
│   ├── prompts.py            # OCR prompt builders
│   ├── vlm_ocr.yaml          # Tool declaration
│   └── vlm_ocr.py            # Tool implementation
├── _assets/
│   └── icon.svg              # Plugin icon
├── vlm_ocr_plugin.difypkg   # Pre-built package (optional)
└── scripts/
    ├── .env.example          # Example environment variables
    ├── install_plugin.sh     # Headless plugin install script
    └── run_pipeline.sh       # Headless dataset pipeline script
```

## Prerequisites

1. Install and run [Ollama](https://ollama.com) on a host reachable from the Dify plugin daemon.
   - Default URL: `http://host.docker.internal:11434`
   - If Dify runs in Docker, use `host.docker.internal` to reach the host Ollama server.
2. Pull a vision-capable model. The plugin defaults to `qwen3.5:cloud`, which is a local/custom model tag. Replace it with a tag that actually exists in your Ollama environment, for example:

   ```bash
   # Public vision models (recommended starting points)
   ollama pull qwen2.5vl:7b
   ollama pull qwen2.5:14b

   # Or use your own custom Modelfile tag
   ollama pull qwen3.5:cloud
   ```

3. For the headless install script, ensure `docker`, `curl`, and `jq` are installed on the host.
4. For the headless pipeline script, ensure `curl` and `jq` are installed and a dataset API key is available.

## Ollama Server Setup

When Ollama serves embedding or vision models for long-running pipelines, set a long enough `keep_alive` value so models stay loaded between requests. Otherwise each pipeline step may reload the model from disk.

```bash
# On the host running Ollama
export OLLAMA_KEEP_ALIVE=30m
ollama serve
```

Or set it permanently in the Ollama service environment:

```bash
# macOS: add via launchctl (adjust path as needed)
launchctl setenv OLLAMA_KEEP_ALIVE 30m

# Linux systemd: create or edit /etc/systemd/system/ollama.service.d/keep_alive.conf
# [Service]
# Environment="OLLAMA_KEEP_ALIVE=30m"
# Then run: systemctl daemon-reload && systemctl restart ollama
```

This keeps models such as `qwen3-embedding:8b` resident in GPU/VRAM for 30 minutes after the last request, reducing pipeline latency and avoiding reload spikes during batch indexing.

## Installation

### Option 1: Manual installation through the Dify UI

1. Build the package:

   ```bash
   cd docker/volumes/plugin-daemon/vlm_ocr_plugin
   zip -r vlm_ocr_plugin.difypkg . \
     -x "*.pyc" "__pycache__/*" ".*" "*.difypkg" \
     -x "scripts/*" "DECISIONS.md" "PROMPT_DESIGN.md" "PIPELINE_SETUP_GUIDE.md" "README.md"
   ```

   > **Note:** Dify plugin CLI는 `.difyignore`를 자동으로 처리하지 않을 수 있으므로, 배포 전 `unzip -l`로 패키지 내용을 확인하세요. README.md를 패키지에 포함하려면 `-x "README.md"` 줄을 제거하세요. Dify 마켓플레이스 배포 시 README.md는 패키지에 포함하지 않아도 됩니다.

2. In Dify Console, go to **Plugins** → **Install from local package** and select `vlm_ocr_plugin.difypkg`.
3. Open the **VLM OCR** provider settings and enter your Ollama base URL and model tag.

### Option 2: Headless install via helper script

1. Copy the example environment file and fill in any local overrides:

   ```bash
   cd docker/volumes/plugin-daemon/vlm_ocr_plugin/scripts
   cp .env.example .env
   # Edit .env if your Dify deployment uses non-default credentials or ports.
   ```

2. Run the install script from the host:

   ```bash
   ./install_plugin.sh
   ```

   The script will:
   - Read the default tenant ID from the Dify Postgres database.
   - Copy the plugin source into the running `plugin_daemon` container.
   - Run `/app/commandline plugin package` inside the container.
   - Upload the resulting `.difypkg` to the plugin daemon management API with `verify_signature=false`.
   - Start the installation task and poll it until the plugin is installed.

## 서명 검증 비활성화 (로컬 개발용)

Headless 설치 스크립트는 업로드 단계에서 `verify_signature=false`를 전송합니다. 로컬 개발 환경에서만 사용하고, 운영 환경에서는 플러그인 서명을 검증하세요.

플러그인 데몬이 서명 검증을 강제하지 않도록 하려면 `docker/.env`에 다음 값을 추가할 수 있습니다:

```bash
FORCE_VERIFYING_SIGNATURE=false
```

> **보안 경고**: 이 설정은 신뢰할 수 없는 플러그인 설치 시 위험할 수 있습니다. 로컬 개발 또는 신뢰하는 플러그인 테스트 외에는 사용하지 마세요.

## plugin daemon 5002 포트 매핑

`install_plugin.sh`는 `http://host.docker.internal:5002`로 플러그인 데몬 관리 API를 호출합니다. 기본 `docker-compose.yaml`은 플러그인 데몬의 `5003` 포트만 노출하므로, 호스트에서 스크립트를 실행하려면 다음 두 가지 방법 중 하나를 선택하세요.

1. **권장: 플러그인 데몬 컨테이너 내부에서 실행**

   ```bash
   docker compose exec plugin_daemon bash
   # 컨테이너 내부에서 /vlm_ocr_plugin/scripts/install_plugin.sh 실행
   ```

2. **호스트에서 실행하려면 포트 매핑 추가**

   `docker/docker-compose.override.yaml`을 만들어 `5002:5002`를 추가합니다:

   ```yaml
   services:
     plugin_daemon:
       ports:
         - "5002:5002"
   ```

   그 후 Dify 스택을 재시작하고 `./install_plugin.sh`를 실행하세요.

## Pipeline Setup Summary

The plugin can be used in two ways:

1. **Workflow / Chatflow tool node**: Add an **Image Upload** or **File Upload** node, connect the file variable to the **VLM OCR** tool node, and capture the `result` output for downstream nodes.
2. **Headless knowledge pipeline**: Use `scripts/run_pipeline.sh` to upload an image to a dataset pipeline and trigger indexing without using the UI.

To run the pipeline headlessly:

1. Create a knowledge base in Dify and enable its service API.
2. Generate a dataset API key.
3. Create or open a knowledge pipeline and note the dataset ID and the datasource node ID you want to start from (`START_NODE_ID`).
4. Configure `scripts/.env` with `DATASET_API_KEY`, `DATASET_ID`, and `START_NODE_ID`.
5. Run the script with an image file:

   ```bash
   ./run_pipeline.sh /path/to/document.jpg
   ```

   The script uploads the image, calls `/v1/datasets/{dataset_id}/pipeline/run`, and polls the document list until indexing completes or fails.

For detailed pipeline configuration steps, see [PIPELINE_SETUP_GUIDE.md](./PIPELINE_SETUP_GUIDE.md).

## Usage in a Workflow

1. Add an **Image Upload** or **File Upload** node to obtain an image file variable.
2. Add the **VLM OCR** tool node.
3. Connect the image file variable to the `image_file` parameter.
4. Optionally fill `prompt` with extra OCR instructions.
5. Optionally override `model` or `ollama_base_url` per tool call.
6. The tool outputs `result` containing the extracted markdown text.

## Parameters

- `image_file` (`file`, required) — the image file to extract text from
- `prompt` (`string`, optional) — extra instructions appended to the OCR prompt
- `model` (`string`, optional) — Ollama model tag, overrides provider setting
- `ollama_base_url` (`string`, optional) — Ollama API URL, overrides provider setting
- `download_mode` (`select`, default `auto`) — how the plugin obtains image bytes:
  - `auto`: try the image URL first; if the URL is blocked or unreachable, fall back to the file blob provided by Dify
  - `blob`: always use the file blob from Dify (avoids SSRF/network issues with private/internal file URLs)
  - `url`: always download from the image URL

## Provider Credentials

- `ollama_base_url` — Ollama API base URL (default `http://host.docker.internal:11434`)
- `ollama_model` — model tag (default `qwen3.5:cloud`; replace with a local Ollama tag such as `qwen2.5vl:7b` or `qwen2.5:14b`)
- `think` — `auto` / `true` / `false`; sends `think: true` to Ollama when enabled (`auto` enables it for `qwen3.5` tags)

## Notes

- The image is resized so that its longer side is at most 4096 pixels before being base64-encoded for Ollama.
- The image is sent as raw base64 (no `data:image/...;base64,` prefix).
- For prompt design details and customization examples, see [PROMPT_DESIGN.md](./PROMPT_DESIGN.md).

## Troubleshooting

### Ollama connection errors

If the tool reports `Ollama 서버에 연결할 수 없습니다`:

1. Verify Ollama is running:

   ```bash
   curl http://localhost:11434/api/tags
   ```

2. From inside the plugin daemon container, confirm the host is reachable:

   ```bash
   docker compose exec plugin_daemon curl http://host.docker.internal:11434/api/tags
   ```

3. If `host.docker.internal` does not resolve, update the provider `ollama_base_url` to the host IP address that the container can reach.

### Model not found

If you see `요청한 모델을 Ollama에서 찾을 수 없습니다`:

```bash
# Use a public vision model, or replace with your own local tag
ollama pull qwen2.5vl:7b
```

### Plugin installation fails

1. Check the plugin daemon logs:

   ```bash
   docker compose logs -f plugin_daemon
   ```

2. Verify the tenant ID resolved correctly:

   ```bash
   docker compose exec db_postgres psql -U postgres -d dify -c "SELECT id, name, created_at FROM tenants;"
   ```

3. Confirm `PLUGIN_DAEMON_KEY` in `scripts/.env` matches the `SERVER_KEY` used by the plugin daemon container.

### Pipeline run or indexing errors

1. Check the API response printed by `run_pipeline.sh`.
2. Review the API server logs:

   ```bash
   docker compose logs -f api
   ```

3. Verify the dataset API key and dataset ID are correct and that the service API is enabled for the knowledge base.
4. Confirm `START_NODE_ID` matches a datasource node in the target pipeline.

### Korean text quality issues

- Ensure the model supports Korean vision tasks.
- Add per-document instructions through the `prompt` parameter.
- Review the prompt structure and examples in [PROMPT_DESIGN.md](./PROMPT_DESIGN.md).
