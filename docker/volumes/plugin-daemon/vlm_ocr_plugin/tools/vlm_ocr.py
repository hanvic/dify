from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
import re
import socket
import sys
import traceback
from collections.abc import Generator
from io import BytesIO
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from PIL import Image, UnidentifiedImageError
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File

from tools.prompts import build_ocr_prompt, build_resize_note
from tools.blank_detector import detect_blank_pre, detect_blank_post, BlankPageError, BlankDetectionResult

Image.MAX_IMAGE_PIXELS = 100_000_000

# PDF support constants
_PDF_MAX_LONG_SIDE: int = 2048
_PDF_JPEG_QUALITY: int = 70
_PDF_MIN_QUALITY: int = 40
_PDF_TARGET_SIZE_BYTES: int = 250 * 1024  # 250KB hard cap per page image
_PDF_DPI: int = 200
_PDF_DEFAULT_MAX_PAGES: int = 120  # 3600s timeout / 30s per page = 120


class PdfPageLimitExceededError(ValueError):
    """Raised when a PDF exceeds the max_pages limit.

    Inherits ValueError so the plugin SDK treats it as a user-facing error,
    but uses a specific class for programmatic identification.
    """


class NetworkImageError(ValueError):
    """Raised for network/transport failures when downloading an image URL."""


class OllamaRateLimitError(Exception):
    """Raised when Ollama returns HTTP 429 (quota/rate limit exceeded).

    This exception is intentionally NOT a ValueError so it propagates through
    the plugin SDK as an unhandled error, causing the document to enter error
    state rather than producing a poisoned text segment.
    """


class OllamaServerError(Exception):
    """Raised for non-recoverable Ollama server errors (5xx, connection, timeout, 404).

    Like OllamaRateLimitError, this propagates unhandled so the pipeline node
    fails rather than storing error text as OCR content.
    """


class OcrContentQualityError(Exception):
    """Raised when Ollama returns a 200 but the content is empty or too short.

    Minimum content length: 10 characters (post-strip).
    Rationale: A valid OCR result from a document image should contain at least
    a few words. Shorter outputs indicate model failure, hallucination, or
    blank-page detection gone wrong. Failing explicitly is better than embedding
    garbage into the knowledge base.

    NOTE: After blank_detector integration, this is raised ONLY for genuine
    model failures (not blank pages). Blank pages raise BlankPageError instead.
    """

    # Minimum character count for a valid OCR result (after stripping whitespace
    # and thinking tags). 10 chars ≈ 3-5 Korean syllables or 2-3 English words.
    MIN_CONTENT_LENGTH = 10


class VlmOcrTool(Tool):
    """Resolve image bytes and call a local Ollama VLM for OCR."""

    _MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024
    _CHUNK_SIZE = 64 * 1024
    _REQUEST_TIMEOUT = (10, 60)
    _OLLAMA_TIMEOUT = (10, 3600)
    _MAX_IMAGE_SIDE = 4096

    _SENSITIVE_QUERY_KEYS = {
        "sign",
        "token",
        "nonce",
        "api_key",
        "apikey",
        "api-key",
        "key",
        "secret",
        "secret_key",
        "private_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "password",
    }

    # Hosts that are allowed to serve images even if they resolve to private/internal
    # addresses. Dify's own file-preview URLs are served from these origins.
    # `api` is the Dify backend's in-compose hostname (INTERNAL_FILES_URL).
    _ALLOWED_HOSTS: frozenset[str] = frozenset(
        {
            "host.docker.internal",
            "localhost",
            "127.0.0.1",
            "192.168.200.107",
            "api",
        }
    )

    _FORMAT_TO_MIME_TYPE = {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "BMP": "image/bmp",
        "WEBP": "image/webp",
        "TIFF": "image/tiff",
        "SVG": "image/svg+xml",
    }

    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        image_file: Any = tool_parameters.get("image_file")
        prompt: Any = tool_parameters.get("prompt")
        download_mode: Any = tool_parameters.get("download_mode")
        include_summary = bool(tool_parameters.get("include_summary"))
        enable_thinking = bool(tool_parameters.get("enable_thinking"))

        if os.environ.get("VLM_OCR_DEBUG") == "1":
            typed_image_file = image_file if isinstance(image_file, File) else None
            typed_prompt = prompt if isinstance(prompt, str) else None
            text = self._serialize_debug_payload(typed_image_file, typed_prompt)
            self._debug_log("VLM_OCR_DEBUG payload:\n%s", text)
            yield self.create_text_message(text)
            return

        # The plugin SDK's `_convert_parameters` already deserializes a dict
        # carrying `dify_model_identity == "__dify__file__"` into a `File`.
        # If we get here without a File, the backend sent an unexpected shape;
        # fall back to `_coerce_to_file` for non-standard inputs (or fail).
        if not isinstance(image_file, File):
            image_file = self._coerce_to_file(image_file, tool_parameters)
        if not isinstance(image_file, File):
            self._debug_log(
                "Invalid image_file: type=%s repr=%.400s",
                type(tool_parameters.get("image_file")).__name__,
                repr(tool_parameters.get("image_file")),
            )
            yield self.create_text_message(
                "Failed to resolve image: no image file provided or invalid file object."
            )
            return

        # --- PDF path: detect and route to multi-page PDF processing ---
        if self._is_pdf_file(image_file):
            self._debug_log("PDF file detected: %s", image_file.filename)
            max_pages = int(tool_parameters.get("max_pages") or _PDF_DEFAULT_MAX_PAGES)
            ocr_text = self._process_pdf(image_file, tool_parameters={
                "max_pages": max_pages,
                "include_summary": include_summary,
                "enable_thinking": enable_thinking,
                "prompt": prompt,
                "model": tool_parameters.get("model"),
                "ollama_base_url": tool_parameters.get("ollama_base_url"),
            })
            yield self.create_text_message(ocr_text)
            yield self.create_variable_message("result", ocr_text)
            return

        extra_instructions = prompt if isinstance(prompt, str) else None
        mode = str(download_mode).lower() if isinstance(download_mode, str) else "auto"

        # FAIL-FAST: All exceptions from Ollama calls (rate limit, server error,
        # content quality) propagate UNHANDLED to the plugin SDK. This causes the
        # pipeline node to enter error state rather than storing error messages
        # as document content. This is the fix for the 13,566-document pollution
        # incident where error strings were embedded as OCR results.
        image_bytes, mime_type = self._resolve_image_bytes(image_file, mode)
        prepared_bytes, resized, width, height = self._prepare_image_bytes(
            image_bytes
        )
        raw_base64 = base64.b64encode(prepared_bytes).decode("ascii")

        image_metadata = {
            "filename": image_file.filename,
            "extension": image_file.extension,
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }
        system_prompt, user_prompt = build_ocr_prompt(
            image_metadata=image_metadata,
            extra_instructions=extra_instructions,
            include_system=True,
            include_summary=include_summary,
        )
        if resized:
            user_prompt += "\n\n" + build_resize_note(width, height)

        model, base_url, think = self._resolve_ollama_config(
            tool_parameters, enable_thinking=enable_thinking
        )
        ocr_text = self._call_ollama(
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_base64=raw_base64,
            think=think,
        )
        yield self.create_text_message(ocr_text)
        yield self.create_variable_message("result", ocr_text)

    @staticmethod
    def _is_pdf_file(image_file: File) -> bool:
        """Check if the given file is a PDF based on MIME type or extension."""
        mime = getattr(image_file, "mime_type", None)
        if mime and "pdf" in str(mime).lower():
            return True
        ext = getattr(image_file, "extension", None)
        if ext and str(ext).lower().strip(".") == "pdf":
            return True
        filename = getattr(image_file, "filename", None)
        if filename and str(filename).lower().endswith(".pdf"):
            return True
        return False

    def _process_pdf(
        self,
        pdf_file: File,
        tool_parameters: dict[str, Any],
    ) -> str:
        """Process a multi-page PDF by rendering each page and OCR-ing it.

        Renders pages one at a time (streaming) to avoid loading the entire PDF
        into memory as images. Each page is rendered at 200 DPI, resized to
        max 2048px long side, compressed to JPEG ≤250KB, then sent to Ollama.

        Returns a single markdown string with page separators for citation.

        Raises:
            PdfPageLimitExceededError: If page count exceeds max_pages.
            OllamaRateLimitError: If Ollama returns 429 during processing.
            OllamaServerError: If Ollama returns a server error.
        """
        import fitz  # PyMuPDF - imported here to avoid loading for image-only calls

        max_pages = int(tool_parameters.get("max_pages", _PDF_DEFAULT_MAX_PAGES))
        include_summary = bool(tool_parameters.get("include_summary", False))
        enable_thinking = bool(tool_parameters.get("enable_thinking", False))
        extra_instructions = tool_parameters.get("prompt")
        if not isinstance(extra_instructions, str):
            extra_instructions = None

        # Get PDF bytes
        pdf_bytes = self._load_blob(pdf_file)
        if pdf_bytes is None:
            url = getattr(pdf_file, "url", None)
            if isinstance(url, str) and url:
                pdf_bytes, _ = self._download_url(url)
            else:
                raise ValueError("PDF 파일 데이터를 가져올 수 없습니다.")

        # Open PDF and check page count
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = doc.page_count

        if page_count > max_pages:
            doc.close()
            raise PdfPageLimitExceededError(
                f"PDF 페이지 수({page_count})가 최대 한도({max_pages}페이지)를 초과합니다. "
                f"UI 업로드는 {max_pages}페이지 이하의 PDF만 처리할 수 있습니다. "
                f"더 긴 PDF는 배치 스크립트를 사용하세요."
            )

        # Resolve Ollama config once for all pages
        model, base_url, think = self._resolve_ollama_config(
            tool_parameters, enable_thinking=enable_thinking
        )

        # Process pages one at a time (streaming to save memory)
        page_results: list[str] = []
        skipped_blank_pages: list[int] = []

        for page_idx in range(page_count):
            self._debug_log("Processing PDF page %d/%d", page_idx + 1, page_count)

            # Render page to pixmap
            page = doc[page_idx]
            mat = fitz.Matrix(_PDF_DPI / 72, _PDF_DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Convert to PIL Image
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            # Release pixmap memory immediately
            del pix

            # Resize if longer side exceeds limit
            width, height = img.size
            long_side = max(width, height)
            if long_side > _PDF_MAX_LONG_SIDE:
                ratio = _PDF_MAX_LONG_SIDE / long_side
                new_size = (int(width * ratio), int(height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                width, height = img.size

            # --- 번 페이지 사전 감지 ---
            pre_result = detect_blank_pre(img)
            if pre_result.is_blank:
                self._debug_log(
                    "PDF page %d/%d: blank detected (pre), skipping. %s",
                    page_idx + 1, page_count, pre_result.reason,
                )
                skipped_blank_pages.append(page_idx + 1)
                del img
                continue

            # Compress to JPEG with adaptive quality
            jpeg_bytes = self._compress_page_image(img)
            del img  # Free PIL image memory

            # Encode to base64
            raw_base64 = base64.b64encode(jpeg_bytes).decode("ascii")
            del jpeg_bytes  # Free compressed bytes

            # Build prompt for this page
            image_metadata = {
                "filename": f"{pdf_file.filename}_p{page_idx + 1}",
                "extension": "jpg",
                "mime_type": "image/jpeg",
                "width": width,
                "height": height,
            }
            system_prompt, user_prompt = build_ocr_prompt(
                image_metadata=image_metadata,
                extra_instructions=extra_instructions,
                include_system=True,
                include_summary=include_summary,
            )

            # Call Ollama (fail-fast for server/rate errors, skip for blank)
            try:
                ocr_text = self._call_ollama(
                    base_url=base_url,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    raw_base64=raw_base64,
                    think=think,
                )
            except BlankPageError as e:
                # Post-detection caught blank page → skip and continue
                self._debug_log(
                    "PDF page %d/%d: blank detected (post-VLM), skipping. %s",
                    page_idx + 1, page_count, e.detection_result.reason,
                )
                skipped_blank_pages.append(page_idx + 1)
                continue

            # Add page separator and content
            page_header = f"## 📄 p.{page_idx + 1}\n\n"
            page_results.append(page_header + ocr_text)

        doc.close()

        # --- 모든 페이지가 빈 PDF인 경우 예외 발생 ---
        if not page_results and skipped_blank_pages:
            raise BlankPageError(
                f"PDF의 모든 페이지({page_count}장)가 빈 페이지입니다. "
                f"빈 페이지 목록: {skipped_blank_pages}",
                BlankDetectionResult(is_blank=True, method="pre_image",
                                     reason=f"all_{page_count}_pages_blank"),
            )

        # Add skipped page notice if any
        if skipped_blank_pages:
            skip_notice = (
                f"\n\n---\n\n> ℹ️ **빈 페이지 건너뜀**: "
                f"p.{', p.'.join(str(p) for p in skipped_blank_pages)} "
                f"({len(skipped_blank_pages)}장)"
            )
            return "\n\n---\n\n".join(page_results) + skip_notice

        # Join all pages with separator
        return "\n\n---\n\n".join(page_results)

    @staticmethod
    def _compress_page_image(img: Image.Image) -> bytes:
        """Compress a PIL Image to JPEG with adaptive quality to meet size target.

        Target: ≤250KB per page image (matching pdf_to_pages.py specs).
        """
        current_quality = _PDF_JPEG_QUALITY
        while current_quality >= _PDF_MIN_QUALITY:
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=current_quality, optimize=True)
            size = buffer.tell()
            if size <= _PDF_TARGET_SIZE_BYTES:
                return buffer.getvalue()
            current_quality -= 10

        # Even at min quality, return the result
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=_PDF_MIN_QUALITY, optimize=True)
        return buffer.getvalue()

    @staticmethod
    def _coerce_to_file(obj: Any, tool_parameters: dict[str, Any]) -> File | None:
        """Wrap a datasource-bound file-like value into a dify_plugin.File.

        The plugin framework's `_convert_parameters` already deserializes a dict
        carrying `dify_model_identity == '__dify__file__'` into a proper `File`.
        This helper only runs for non-File inputs (plain dicts without the
        identity marker, or SQLAlchemy-like objects) and builds a `File` from
        their fields. `File` is a Pydantic model whose required fields are
        `url: str` and `type: FileType`; `blob` is a read-only property that
        fetches from `url` on demand, so it must NOT be passed to the
        constructor. Returning `None` triggers the upstream "Failed to resolve
        image" path so the user sees an explicit error instead of a silent
        crash.
        """
        if obj is None or isinstance(obj, File):
            return obj if isinstance(obj, File) else None
        from dify_plugin.file.entities import FileType
        from dify_plugin.file.file import File as _File

        def _build(
            url: str | None,
            mime_type: str | None,
            filename: str | None,
            extension: str | None,
            size: int | None = None,
        ) -> File | None:
            if not isinstance(url, str) or not url:
                return None
            return _File(
                url=url,
                type=FileType.IMAGE,
                mime_type=mime_type,
                filename=filename,
                extension=extension,
                size=size,
            )

        # 1) dict-like — prefer `url`/`related_url`; `blob` is not a File field
        if isinstance(obj, dict):
            url = obj.get("url") or obj.get("related_url")
            return _build(
                url=url if isinstance(url, str) else None,
                mime_type=obj.get("mime_type") or obj.get("mimeType"),
                filename=obj.get("filename") or obj.get("name"),
                extension=obj.get("extension")
                or (obj.get("name", "").rsplit(".", 1)[-1] if obj.get("name") else None),
                size=obj.get("size"),
            )

        # 2) SQLAlchemy-like object — read `related_url`/`url`; skip blob (plugin
        #    daemon has no `extensions.ext_storage` and `blob` isn't a File arg)
        for attr in ("related_url", "url", "key", "id", "name"):
            if hasattr(obj, attr):
                url = getattr(obj, "related_url", None) or getattr(obj, "url", None)
                if not isinstance(url, str) or not url:
                    continue
                return _build(
                    url=url,
                    mime_type=getattr(obj, "mime_type", None),
                    filename=getattr(obj, "name", None),
                    extension=(getattr(obj, "name", "") or "").rsplit(".", 1)[-1] or None,
                    size=getattr(obj, "size", None),
                )
        return None

    def _resolve_ollama_config(
        self,
        tool_parameters: dict[str, Any],
        *,
        enable_thinking: bool = False,
    ) -> tuple[str, str, bool]:
        credentials = getattr(self.runtime, "credentials", {}) or {}

        model = tool_parameters.get("model") or credentials.get(
            "ollama_model", "qwen3.5:cloud"
        )
        base_url = tool_parameters.get("ollama_base_url") or credentials.get(
            "ollama_base_url", "http://host.docker.internal:11434"
        )
        think_setting = credentials.get("think", "auto")

        # An explicit per-node toggle wins over the credential-level setting.
        # When enabled, the VLM reasons over the image before emitting the OCR
        # text (+ summary); only the final answer is returned (see
        # ``_strip_thinking``), so reasoning never reaches embedding.
        if enable_thinking:
            think = True
        else:
            think = self._resolve_think_flag(model, think_setting)
        return str(model), str(base_url), think

    def _resolve_think_flag(self, model: str, think_setting: str) -> bool:
        think_setting = str(think_setting).lower()
        if think_setting == "true":
            return True
        if think_setting == "false":
            return False
        return "qwen3.5" in str(model).lower()

    @classmethod
    def _get_ollama_timeout(cls) -> tuple[int, int]:
        """Return the Ollama request timeout.

        The read timeout can be overridden at runtime via the ``VLM_OCR_TIMEOUT``
        environment variable (seconds). The connect timeout is kept at the
        default value.
        """
        timeout_env = os.environ.get("VLM_OCR_TIMEOUT")
        if timeout_env:
            try:
                seconds = int(timeout_env)
                if seconds > 0:
                    return (cls._OLLAMA_TIMEOUT[0], seconds)
            except ValueError:
                pass
        return cls._OLLAMA_TIMEOUT

    def _call_ollama(
        self,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        raw_base64: str,
        think: bool,
    ) -> str:
        chat_url = base_url.rstrip("/") + "/api/chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [raw_base64],
                },
            ],
            "stream": False,
        }
        payload["think"] = think

        self._debug_log(
            "Calling Ollama %s with model=%s, think=%s, images=%d bytes base64",
            chat_url,
            model,
            think,
            len(raw_base64),
        )

        try:
            response = requests.post(
                chat_url,
                json=payload,
                timeout=self._get_ollama_timeout(),
            )
        except requests.exceptions.Timeout as e:
            self._debug_log("Ollama request timed out: %s", e)
            raise OllamaServerError("Ollama 서버 응답 시간이 초과되었습니다.") from e
        except requests.exceptions.ConnectionError as e:
            self._debug_log("Ollama connection error: %s", e)
            raise OllamaServerError("Ollama 서버에 연결할 수 없습니다.") from e
        except requests.exceptions.RequestException as e:
            self._debug_log("Ollama request failed: %s", e)
            raise OllamaServerError("Ollama 요청에 실패했습니다.") from e

        # --- 429 Rate Limit: 쿼터 초과 전용 예외 ---
        if response.status_code == 429:
            self._debug_log("Ollama returned 429 (rate limit): %s", response.text)
            raise OllamaRateLimitError(
                f"[QUOTA_EXCEEDED] Ollama API 쿼터 초과 (HTTP 429). "
                f"세션 사용량 한도에 도달했습니다. 업그레이드하거나 대기 후 재시도하십시오. "
                f"rate_limit | quota | 429"
            )

        if response.status_code == 404:
            self._debug_log("Ollama returned 404: %s", response.text)
            raise OllamaServerError("요청한 모델을 Ollama에서 찾을 수 없습니다.")

        try:
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            self._debug_log("Ollama HTTP error: %s", e)
            raise OllamaServerError(
                f"Ollama 서버에서 오류 응답을 반환했습니다 (HTTP {response.status_code})."
            ) from e
        except json.JSONDecodeError as e:
            self._debug_log("Ollama returned invalid JSON: %s", e)
            raise OllamaServerError("Ollama 응답 형식이 올바르지 않습니다.") from e

        try:
            content = str(data["message"]["content"])
        except (KeyError, TypeError) as e:
            self._debug_log("Ollama response missing expected content field: %s", e)
            raise OllamaServerError("Ollama 응답에서 OCR 결과를 추출할 수 없습니다.") from e

        content = self._strip_thinking(content)

        # --- 본문 품질 가드 ---
        # 정상 상태코드(200)이어도 결과가 빈/공백/지나치게 짧으면 예외로 올린다.
        # 이는 오류 메시지가 임베딩되는 것을 방지하기 위한 최후의 방어선이다.
        if not content or len(content.strip()) < OcrContentQualityError.MIN_CONTENT_LENGTH:
            # --- 번 페이지 사후 감지 ---
            # VLM이 "빈 페이지"라고 응답한 경우 → BlankPageError (재시도 불필요)
            # VLM이 이상하게 짧은 응답을 한 경우 → OcrContentQualityError (재시도 대상)
            post_result = detect_blank_post(content)
            if post_result.is_blank:
                raise BlankPageError(
                    f"VLM 사후 감지: 빈 페이지로 판정 ({post_result.reason})",
                    post_result,
                )
            raise OcrContentQualityError(
                f"OCR 결과가 비어있거나 너무 짧습니다 "
                f"(길이: {len(content.strip()) if content else 0}자, "
                f"최소: {OcrContentQualityError.MIN_CONTENT_LENGTH}자). "
                f"모델이 유효한 텍스트를 추출하지 못했습니다."
            )

        return content

    # Matches Qwen3-style inline reasoning blocks ``<think>...</think>`` that
    # some Ollama model/version combos emit inside ``message.content`` instead
    # of the separate ``message.thinking`` field. Stripped so only the final
    # answer reaches downstream chunking and embedding.
    _THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

    @staticmethod
    def _strip_thinking(content: str) -> str:
        """Remove reasoning traces inlined into the content field.

        Ollama >=0.9 returns reasoning in a separate ``message.thinking`` field,
        which we already ignore by reading only ``message.content``. A few
        models still inline reasoning as ``<think>...</think>`` blocks (or a
        dangling ``</think>`` with no opening tag) inside ``content``; strip
        those so thinking text never reaches downstream chunking/embedding.
        """
        if not content:
            return content
        content = VlmOcrTool._THINK_TAG_RE.sub("", content)
        idx = content.find("</think>")
        if idx != -1:
            content = content[idx + len("</think>") :]
        return content.strip()

    def _prepare_image_bytes(
        self, image_bytes: bytes
    ) -> tuple[bytes, bool, int, int]:
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                img.load()
                original_width, original_height = img.size

                if (
                    original_width <= self._MAX_IMAGE_SIDE
                    and original_height <= self._MAX_IMAGE_SIDE
                ):
                    return image_bytes, False, original_width, original_height

                ratio = min(
                    self._MAX_IMAGE_SIDE / original_width,
                    self._MAX_IMAGE_SIDE / original_height,
                )
                target_width = int(original_width * ratio)
                target_height = int(original_height * ratio)

                resized = img.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
                output = BytesIO()
                if resized.mode in ("RGBA", "P"):
                    resized.save(output, format="PNG", optimize=True)
                else:
                    resized.save(output, format="JPEG", quality=95)
                return output.getvalue(), True, target_width, target_height
        except UnidentifiedImageError as e:
            self._debug_log("PIL could not identify image: %s", e)
            raise ValueError("이미지 파일을 인식할 수 없거나 손상되었습니다.")
        except Exception as e:
            self._debug_log("Image preparation failed: %s", e)
            self._log_traceback()
            raise ValueError("이미지를 OCR용으로 준비하는 중 실패했습니다.")

    def _resolve_image_bytes(
        self, image_file: File, mode: str = "auto"
    ) -> tuple[bytes, str | None]:
        url = getattr(image_file, "url", None)

        # data: URI is always handled directly
        if isinstance(url, str) and url.startswith("data:"):
            image_bytes, mime_type = self._parse_data_uri(url)
            img_format = self._validate_image_bytes(image_bytes)
            if not mime_type:
                mime_type = self._format_to_mime_type(img_format)
            return image_bytes, mime_type

        # mode=blob: prefer the file blob passed by Dify; never call the network.
        if mode == "blob":
            image_bytes = self._load_blob(image_file)
            if image_bytes is None:
                raise ValueError(
                    "이미지 blob을 사용할 수 없습니다. Dify에서 파일 객체가 제대로 전달되었는지 확인하세요."
                )
            mime_type = self._resolve_mime_type(image_file, None)
            img_format = self._validate_image_bytes(image_bytes)
            if not mime_type:
                mime_type = self._format_to_mime_type(img_format)
            return image_bytes, mime_type

        # mode=url: always download from URL (useful for public URLs)
        if mode == "url":
            if not isinstance(url, str) or not url:
                raise ValueError("download_mode=url일 때는 이미지 URL이 필요합니다.")
            image_bytes, mime_type = self._download_url(url)
            mime_type = self._resolve_mime_type(image_file, mime_type)
            img_format = self._validate_image_bytes(image_bytes)
            if not mime_type:
                mime_type = self._format_to_mime_type(img_format)
            return image_bytes, mime_type

        # mode=auto (default): try URL first, fall back to blob if blocked or missing
        if isinstance(url, str) and url:
            try:
                image_bytes, mime_type = self._download_url(url)
            except NetworkImageError:
                self._debug_log(
                    "URL download blocked or failed for %s, falling back to file blob",
                    self._mask_debug_url(url),
                )
                image_bytes = self._load_blob(image_file)
                if image_bytes is not None:
                    mime_type = self._resolve_mime_type(image_file, None)
                else:
                    raise
            else:
                mime_type = self._resolve_mime_type(image_file, mime_type)

            img_format = self._validate_image_bytes(image_bytes)
            if not mime_type:
                mime_type = self._format_to_mime_type(img_format)
            return image_bytes, mime_type

        image_bytes = self._load_blob(image_file)
        if image_bytes is None:
            raise ValueError("이미지 URL이나 저장된 이미지 데이터를 찾을 수 없습니다.")

        mime_type = self._resolve_mime_type(image_file, None)
        img_format = self._validate_image_bytes(image_bytes)
        if not mime_type:
            mime_type = self._format_to_mime_type(img_format)
        return image_bytes, mime_type

    def _parse_data_uri(self, url: str) -> tuple[bytes, str]:
        match = re.match(r"data:([^;]*)(?:;[^;]+)*;base64,(.+)", url, re.IGNORECASE)
        if not match:
            self._debug_log("Unsupported data URI format")
            raise ValueError("이미지 데이터 URI 형식이 지원되지 않습니다.")

        mime_type = match.group(1).strip().lower()
        encoded = match.group(2)

        try:
            decoded = base64.b64decode(encoded)
        except Exception as e:
            self._debug_log("Failed to decode base64 data URI: %s", e)
            raise ValueError("이미지 데이터 URI 형식이 올바르지 않습니다.")

        if len(decoded) > self._MAX_DOWNLOAD_SIZE:
            self._debug_log("Data URI exceeded max size limit")
            raise ValueError("이미지 데이터 URI가 허용 한도(20MB)를 초과했습니다.")

        return decoded, mime_type

    def _download_url(self, url: str) -> tuple[bytes, str | None]:
        allowed_ips = self._validate_url_security(url)

        try:
            with requests.get(
                url,
                timeout=self._REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=False,
            ) as response:
                try:
                    connected_ip = response.raw._connection.sock.getpeername()[0]
                except AttributeError:
                    self._debug_log("Unable to determine connected peer address")
                    raise NetworkImageError(
                        "이미지 다운로드 연결 정보를 확인할 수 없습니다."
                    )

                if connected_ip not in allowed_ips:
                    self._debug_log(
                        "Connected IP %s not in allowed IPs %s",
                        connected_ip,
                        allowed_ips,
                    )
                    raise NetworkImageError(
                        "이미지 다운로드 중 연결된 IP가 검증된 주소와 일치하지 않습니다."
                    )

                if response.is_redirect:
                    self._debug_log("Blocked redirect response: %s", response.status_code)
                    raise NetworkImageError("이미지 다운로드 중 리디렉션이 차단되었습니다.")

                response.raise_for_status()
                content_type = response.headers.get("Content-Type")
                mime_type = self._normalize_content_type(content_type)

                content = bytearray()
                for chunk in response.iter_content(chunk_size=self._CHUNK_SIZE):
                    if chunk:
                        content.extend(chunk)
                        if len(content) > self._MAX_DOWNLOAD_SIZE:
                            self._debug_log("Download exceeded max size limit")
                            raise ValueError(
                                "이미지 크기가 허용 한도(20MB)를 초과했습니다."
                            )
        except requests.exceptions.Timeout as e:
            self._debug_log("Timeout downloading image: %s", e)
            raise NetworkImageError("이미지 다운로드 시간이 초과되었습니다.")
        except requests.exceptions.ConnectionError as e:
            self._debug_log("Connection error downloading image: %s", e)
            raise NetworkImageError("이미지 URL에 연결할 수 없습니다.")
        except requests.exceptions.RequestException as e:
            self._debug_log("Request error downloading image: %s", e)
            raise NetworkImageError("이미지 다운로드에 실패했습니다.")

        return bytes(content), mime_type

    def _validate_url_security(self, url: str) -> list[str]:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            self._debug_log("Rejected URL with non-HTTP scheme: %s", scheme)
            raise ValueError("허용되지 않은 URL scheme입니다.")

        hostname = parsed.hostname
        if not hostname:
            self._debug_log("Rejected URL with empty hostname")
            raise ValueError("URL에 유효한 호스트 이름이 없습니다.")

        # Hosts that are explicitly allowed (e.g. Dify's own file server)
        if hostname.lower() in self._ALLOWED_HOSTS:
            try:
                infos = socket.getaddrinfo(hostname, None)
            except socket.gaierror:
                self._debug_log(
                    "Could not resolve allowed hostname: %s", hostname
                )
                raise NetworkImageError("이미지 URL의 주소를 확인할 수 없습니다.")
            return [sockaddr[0] for _, _, _, _, sockaddr in infos]

        try:
            addr = ipaddress.ip_address(hostname)
        except ValueError:
            addr = None

        if addr is not None:
            if self._is_blocked_address(addr):
                self._debug_log("Rejected URL with blocked IP literal: %s", hostname)
                raise ValueError("내부 네트워크 주소로의 요청은 허용되지 않습니다.")
            return [str(addr)]

        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            self._debug_log("Could not resolve hostname for SSRF check: %s", hostname)
            raise NetworkImageError("이미지 URL의 주소를 확인할 수 없습니다.")

        allowed_ips: list[str] = []
        for info in infos:
            _, _, _, _, sockaddr = info
            ip_str = sockaddr[0]
            try:
                resolved_addr = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if self._is_blocked_address(resolved_addr):
                self._debug_log(
                    "Rejected URL resolving to blocked address: %s -> %s",
                    hostname,
                    ip_str,
                )
                raise ValueError("내부 네트워크 주소로의 요청은 허용되지 않습니다.")
            allowed_ips.append(ip_str)

        return allowed_ips

    def _is_blocked_address(
        self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> bool:
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )

    def _load_blob(self, image_file: File) -> bytes | None:
        try:
            return image_file.blob
        except Exception as e:
            self._debug_log("Failed to retrieve image blob: %s", e)
            return None

    def _resolve_mime_type(
        self, image_file: File, http_mime_type: str | None
    ) -> str | None:
        if http_mime_type:
            return http_mime_type

        if image_file.mime_type:
            return image_file.mime_type

        extension = getattr(image_file, "extension", None)
        if extension:
            ext = extension if extension.startswith(".") else f".{extension}"
            guessed, _ = mimetypes.guess_type(f"file{ext}")
            return guessed

        return None

    def _normalize_content_type(self, header_value: str | None) -> str | None:
        if not header_value:
            return None
        return header_value.split(";")[0].strip().lower() or None

    def _validate_image_bytes(self, image_bytes: bytes) -> str | None:
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                img.load()
                if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                    self._debug_log(
                        "Image resolution exceeds limit: %sx%s",
                        img.width,
                        img.height,
                    )
                    raise ValueError("이미지 해상도가 허용 한도를 초과했습니다.")
                return img.format
        except UnidentifiedImageError as e:
            self._debug_log("PIL could not identify image: %s", e)
            raise ValueError("이미지 파일을 인식할 수 없거나 손상되었습니다.")
        except Exception as e:
            self._debug_log("PIL validation failed: %s", e)
            self._log_traceback()
            raise ValueError("이미지 유효성 검사에 실패했습니다.")

    def _format_to_mime_type(self, image_format: str | None) -> str | None:
        if not image_format:
            return None
        return self._FORMAT_TO_MIME_TYPE.get(image_format.upper())

    def _serialize_debug_payload(
        self, image_file: File | None, prompt: str | None
    ) -> str:
        if isinstance(image_file, File):
            payload = {
                "image_file": {
                    "dify_model_identity": getattr(
                        image_file, "dify_model_identity", None
                    ),
                    "url": self._mask_debug_url(getattr(image_file, "url", None)),
                    "mime_type": image_file.mime_type,
                    "filename": image_file.filename,
                    "extension": image_file.extension,
                    "size": image_file.size,
                    "type": image_file.type.value if image_file.type else None,
                },
                "prompt": prompt,
            }
        else:
            payload = {
                "image_file": image_file,
                "prompt": prompt,
            }

        return json.dumps(payload, default=str, ensure_ascii=False, indent=2)

    def _mask_debug_url(self, url: Any) -> Any:
        if not isinstance(url, str):
            return url

        if url.startswith("data:"):
            match = re.match(r"data:([^;]*)(?:;[^;]+)*;base64,(.+)", url, re.IGNORECASE)
            if match:
                mime_type = match.group(1).strip().lower()
                encoded = match.group(2)
                return f"data:{mime_type};base64,<{len(encoded)} bytes>"
            return "data:<unsupported data URI>"

        parsed = urlparse(url)
        if parsed.scheme.lower() in {"http", "https"}:
            netloc = parsed.netloc
            if "@" in netloc:
                userinfo, _, hostinfo = netloc.rpartition("@")
                netloc = f"***@{hostinfo}"

            if parsed.query:
                masked_params = [
                    (key, "***" if key.lower() in self._SENSITIVE_QUERY_KEYS else value)
                    for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                ]
                masked_query = urlencode(masked_params)
            else:
                masked_query = ""

            return urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    masked_query,
                    parsed.fragment,
                )
            )

        return url

    def _log_traceback(self) -> None:
        """Print the active exception traceback to stderr when debug logging is on."""
        if os.environ.get("VLM_OCR_DEBUG") == "1" or os.environ.get("VLM_OCR_LOG") == "1":
            traceback.print_exc()

    def _debug_log(self, message: str, *args: object) -> None:
        if os.environ.get("VLM_OCR_DEBUG") == "1" or os.environ.get("VLM_OCR_LOG") == "1":
            try:
                sys.stderr.write("[vlm_ocr] " + (message % args) + "\n")
            except Exception:
                pass
