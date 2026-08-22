#!/usr/bin/env python3
"""PDF 업로드 + 파이프라인 실행 검증 스크립트.

run_pipeline.sh의 JPEG/PNG 전용 로직을 PDF까지 확장.
- /v1/datasets/pipeline/file-upload 로 PDF 업로드
- /v1/datasets/{id}/pipeline/run 으로 워크플로우 트리거
- /v1/datasets/{id}/documents 로 색인 상태 폴링
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_pipeline_pdf.py <pdf-path>", file=sys.stderr)
        return 1
    pdf = Path(sys.argv[1]).expanduser().resolve()
    if not pdf.is_file():
        print(f"file not found: {pdf}", file=sys.stderr)
        return 1
    if pdf.suffix.lower() != ".pdf":
        print(f"only .pdf supported, got: {pdf.suffix}", file=sys.stderr)
        return 1

    env = load_env(SCRIPT_DIR / ".env")
    base = env.get("DIFY_API_BASE", "http://localhost/v1")
    api_key = env.get("DATASET_API_KEY", "")
    dataset_id = env.get("DATASET_ID", "")
    start_node = env.get("START_NODE_ID", "")
    if not (api_key and dataset_id and start_node):
        print("DATASET_API_KEY / DATASET_ID / START_NODE_ID required", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bearer {api_key}"}
    file_name = pdf.name
    size_mb = pdf.stat().st_size / 1024 / 1024
    print(f"[1/4] uploading {file_name} ({size_mb:.2f} MB)")

    t0 = time.time()
    with pdf.open("rb") as fh:
        up = requests.post(
            f"{base}/datasets/pipeline/file-upload",
            headers=headers,
            files={"file": (file_name, fh, "application/pdf")},
            timeout=300,
        )
    up_elapsed = time.time() - t0
    print(f"      upload: HTTP {up.status_code} in {up_elapsed:.1f}s")
    if up.status_code != 200 and up.status_code != 201:
        print(up.text[:500], file=sys.stderr)
        return 1
    file_id = (up.json().get("id") or up.json().get("data", {}).get("id") or "").strip()
    if not file_id:
        print(f"no file id in response: {up.text[:200]}", file=sys.stderr)
        return 1
    print(f"      file_id={file_id}")

    print("[2/4] running pipeline (blocking, max 60min)")
    payload = {
        "inputs": {},
        "datasource_type": "local_file",
        "datasource_info_list": [{"reference": file_id, "name": file_name}],
        "start_node_id": start_node,
        "is_published": True,
        "response_mode": "blocking",
    }
    t0 = time.time()
    run = requests.post(
        f"{base}/datasets/{dataset_id}/pipeline/run",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=3600,
    )
    run_elapsed = time.time() - t0
    print(f"      pipeline run: HTTP {run.status_code} in {run_elapsed:.1f}s")
    try:
        body = run.json()
        code = body.get("code")
        if code not in (None, 0, "0"):
            print(f"      code={code} message={body.get('message')}", file=sys.stderr)
            print(json.dumps(body, ensure_ascii=False)[:500], file=sys.stderr)
            return 1
        data = body.get("data") or {}
        workflow_run_id = data.get("workflow_run_id") or data.get("id")
        print(f"      workflow_run_id={workflow_run_id}")
    except Exception as e:
        print(f"      parse failed: {e}; raw={run.text[:300]}", file=sys.stderr)
        return 1

    print("[3/4] polling document indexing status (up to 10min)")
    deadline = time.time() + 600
    doc_id = None
    while time.time() < deadline:
        lst = requests.get(
            f"{base}/datasets/{dataset_id}/documents",
            headers=headers,
            params={"keyword": file_name, "limit": 5},
            timeout=60,
        )
        if lst.status_code == 200:
            for d in lst.json().get("data") or []:
                if d.get("name") == file_name:
                    doc_id = d["id"]
                    status = d.get("indexing_status")
                    disp = d.get("display_status")
                    err = d.get("error")
                    print(f"      doc={doc_id} status={status} display={disp} err={err}")
                    if status == "completed":
                        print("[4/4] OK")
                        return 0
                    if status in ("error", "failed"):
                        print(f"      FAILED: {err}", file=sys.stderr)
                        return 1
                    break
        time.sleep(5)
    print("      timed out", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
