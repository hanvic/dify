"""Lightweight unit tests for the VLM OCR tool.

These tests use mocks and in-memory images so they do not require a running
Dify stack or Ollama server.
"""

from __future__ import annotations

import os
import sys
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

# Make the plugin package importable from the tests directory.
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

from tools.vlm_ocr import VlmOcrTool, OllamaRateLimitError, OllamaServerError, OcrContentQualityError  # noqa: E402


@pytest.fixture
def tool() -> VlmOcrTool:
    """Return a VlmOcrTool instance with mocked runtime/session for unit testing."""
    mock_runtime = MagicMock()
    mock_runtime.credentials = {}
    mock_session = MagicMock()
    mock_session.session_id = "test-session"
    instance = VlmOcrTool.__new__(VlmOcrTool)
    instance.runtime = mock_runtime
    instance.session = mock_session
    return instance


def _make_image_bytes(width: int, height: int, mode: str = "RGB") -> bytes:
    """Create an in-memory JPEG image of the requested size."""
    output = BytesIO()
    Image.new(mode, (width, height), color=(255, 255, 255)).save(
        output, format="JPEG", quality=85
    )
    return output.getvalue()


# ============================================================
# Fail-fast: 429 쿼터 초과 → 예외 (에러 문자열 리턴 아님)
# ============================================================


class Test429RateLimit:
    """Ollama 429 응답 시 OllamaRateLimitError 예외가 발생해야 한다."""

    def test_429_raises_rate_limit_error(self, tool: VlmOcrTool) -> None:
        """429 응답 시 OllamaRateLimitError가 raise되고, 'quota' 또는 '쿼터' 식별자가 포함된다."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = '{"error":"you have reached your session usage limit"}'

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(OllamaRateLimitError) as exc_info:
                tool._call_ollama(
                    base_url="http://host.docker.internal:11434",
                    model="qwen3.5:cloud",
                    system_prompt="system",
                    user_prompt="user",
                    raw_base64="dGVzdA==",
                    think=False,
                )
            # 예외 문구에 쿼터 식별자가 있어야 상위에서 구별 가능
            assert "quota" in str(exc_info.value).lower() or "쿼터" in str(exc_info.value) or "rate_limit" in str(exc_info.value).lower()

    def test_429_is_not_caught_by_invoke(self, tool: VlmOcrTool) -> None:
        """_invoke에서 429 예외가 catch되지 않고 상위로 전파되어야 한다."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.text = '{"error":"session usage limit"}'

        with patch("requests.post", return_value=mock_response):
            with patch.object(tool, "_resolve_image_bytes", return_value=(b"fake", "image/jpeg")):
                with patch.object(tool, "_prepare_image_bytes", return_value=(b"fake", False, 100, 100)):
                    # _invoke는 generator — 소비 시 예외가 상위로 전파되어야 한다
                    with pytest.raises(OllamaRateLimitError):
                        list(tool._invoke({
                            "image_file": _make_mock_file(),
                            "prompt": None,
                            "download_mode": "auto",
                            "include_summary": False,
                            "enable_thinking": False,
                        }))


# ============================================================
# Fail-fast: 500 서버 오류 → 예외 (문자열 리턴 아님)
# ============================================================


class Test500ServerError:
    """Ollama 500 응답 시 OllamaServerError 예외가 발생해야 한다."""

    def test_500_raises_server_error(self, tool: VlmOcrTool) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(
            response=mock_response
        )

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(OllamaServerError):
                tool._call_ollama(
                    base_url="http://host.docker.internal:11434",
                    model="qwen3.5:cloud",
                    system_prompt="system",
                    user_prompt="user",
                    raw_base64="dGVzdA==",
                    think=False,
                )

    def test_500_does_not_return_text(self, tool: VlmOcrTool) -> None:
        """_invoke에서 500 에러가 catch되지 않고 상위로 전파되어야 한다."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(
            response=mock_response
        )

        with patch("requests.post", return_value=mock_response):
            with patch.object(tool, "_resolve_image_bytes", return_value=(b"fake", "image/jpeg")):
                with patch.object(tool, "_prepare_image_bytes", return_value=(b"fake", False, 100, 100)):
                    with pytest.raises(OllamaServerError):
                        list(tool._invoke({
                            "image_file": _make_mock_file(),
                            "prompt": None,
                            "download_mode": "auto",
                            "include_summary": False,
                            "enable_thinking": False,
                        }))


# ============================================================
# Fail-fast: 빈 본문 → 예외
# ============================================================


class TestEmptyContent:
    """모델이 빈 본문을 반환하면 예외가 발생해야 한다.

    After blank_detector integration:
    - Empty/whitespace → BlankPageError (blank page, not a failure)
    - Short content without blank keywords → OcrContentQualityError (genuine failure)
    """

    def test_empty_content_raises_blank_page_error(self, tool: VlmOcrTool) -> None:
        """빈 응답은 이제 BlankPageError (번 페이지 분류)를 올린다."""
        from tools.blank_detector import BlankPageError
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": ""}
        }

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(BlankPageError):
                tool._call_ollama(
                    base_url="http://host.docker.internal:11434",
                    model="qwen3.5:cloud",
                    system_prompt="system",
                    user_prompt="user",
                    raw_base64="dGVzdA==",
                    think=False,
                )

    def test_whitespace_only_raises_blank_page_error(self, tool: VlmOcrTool) -> None:
        """공백만 있는 응답도 BlankPageError를 올린다."""
        from tools.blank_detector import BlankPageError
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": "   \n\t  "}
        }

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(BlankPageError):
                tool._call_ollama(
                    base_url="http://host.docker.internal:11434",
                    model="qwen3.5:cloud",
                    system_prompt="system",
                    user_prompt="user",
                    raw_base64="dGVzdA==",
                    think=False,
                )

    def test_too_short_content_raises_quality_error(self, tool: VlmOcrTool) -> None:
        """10자 미만 결과는 유효한 OCR 결과가 아닌 것으로 간주한다."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": "abc"}
        }

        with patch("requests.post", return_value=mock_response):
            with pytest.raises(OcrContentQualityError):
                tool._call_ollama(
                    base_url="http://host.docker.internal:11434",
                    model="qwen3.5:cloud",
                    system_prompt="system",
                    user_prompt="user",
                    raw_base64="dGVzdA==",
                    think=False,
                )


# ============================================================
# 회귀 테스트: 정상 상황에서 기존 동작 유지
# ============================================================


class TestNormalOperation:
    """정상적인 OCR 응답에서는 기존대로 텍스트를 반환한다."""

    def test_normal_response_returns_text(self, tool: VlmOcrTool) -> None:
        expected_text = "# OCR 결과\n\n추출된 한국어 텍스트입니다. 이것은 정상적인 문서 본문입니다."
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": expected_text}
        }

        with patch("requests.post", return_value=mock_response) as mock_post:
            result = tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="qwen3.5:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )

        assert result == expected_text
        mock_post.assert_called_once()

    def test_thinking_tags_stripped(self, tool: VlmOcrTool) -> None:
        content_with_think = "<think>reasoning here</think>실제 OCR 결과 텍스트입니다. 충분히 긴 본문."
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": content_with_think}
        }

        with patch("requests.post", return_value=mock_response):
            result = tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="qwen3.5:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )

        assert "<think>" not in result
        assert "실제 OCR 결과 텍스트입니다" in result

    def test_minimum_valid_length(self, tool: VlmOcrTool) -> None:
        """정확히 최소 길이(10자)인 결과는 통과해야 한다."""
        valid_text = "1234567890"  # 정확히 10자
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": valid_text}
        }

        with patch("requests.post", return_value=mock_response):
            result = tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="qwen3.5:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )

        assert result == valid_text


# ============================================================
# 기존 테스트 (회귀 방지)
# ============================================================


def test_prepare_image_bytes_does_not_resize_small_image(tool: VlmOcrTool) -> None:
    image_bytes = _make_image_bytes(800, 600)
    prepared, resized, width, height = tool._prepare_image_bytes(image_bytes)

    assert resized is False
    assert width == 800
    assert height == 600
    assert len(prepared) > 0


def test_prepare_image_bytes_resizes_large_image(tool: VlmOcrTool) -> None:
    max_side = VlmOcrTool._MAX_IMAGE_SIDE
    image_bytes = _make_image_bytes(max_side * 2, max_side)
    prepared, resized, width, height = tool._prepare_image_bytes(image_bytes)

    assert resized is True
    assert width <= max_side
    assert height <= max_side
    assert max(width, height) == max_side


def test_prepare_image_bytes_resizes_rgba_image_to_png(tool: VlmOcrTool) -> None:
    output = BytesIO()
    Image.new("RGBA", (VlmOcrTool._MAX_IMAGE_SIDE * 2, 500), (255, 0, 0, 128)).save(
        output, format="PNG"
    )
    prepared, resized, width, height = tool._prepare_image_bytes(output.getvalue())

    assert resized is True
    assert width <= VlmOcrTool._MAX_IMAGE_SIDE
    assert height <= VlmOcrTool._MAX_IMAGE_SIDE


def test_call_ollama_success(tool: VlmOcrTool) -> None:
    expected_text = "# OCR 결과\n\n추출된 한국어 텍스트입니다. 이것은 충분히 긴 정상 결과입니다."
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "message": {"role": "assistant", "content": expected_text}
    }

    with patch("requests.post", return_value=mock_response) as mock_post:
        result = tool._call_ollama(
            base_url="http://host.docker.internal:11434",
            model="kimi-k2.7-code:cloud",
            system_prompt="system",
            user_prompt="user",
            raw_base64="dGVzdA==",
            think=False,
        )

    assert result == expected_text
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["model"] == "kimi-k2.7-code:cloud"
    assert kwargs["json"]["think"] is False
    assert kwargs["json"]["stream"] is False
    assert kwargs["timeout"] == VlmOcrTool._OLLAMA_TIMEOUT


def test_call_ollama_timeout_override(tool: VlmOcrTool, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"message": {"content": "이것은 충분히 긴 정상 결과입니다 - timeout override test"}}

    monkeypatch.setenv("VLM_OCR_TIMEOUT", "900")
    with patch("requests.post", return_value=mock_response) as mock_post:
        tool._call_ollama(
            base_url="http://host.docker.internal:11434",
            model="kimi-k2.7-code:cloud",
            system_prompt="system",
            user_prompt="user",
            raw_base64="dGVzdA==",
            think=False,
        )

    _, kwargs = mock_post.call_args
    assert kwargs["timeout"] == (10, 900)


def test_call_ollama_model_not_found(tool: VlmOcrTool) -> None:
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "model not found"

    with patch("requests.post", return_value=mock_response):
        with pytest.raises(OllamaServerError, match="모델"):
            tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="missing-model",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )


def test_call_ollama_connection_error(tool: VlmOcrTool) -> None:
    import requests

    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
        with pytest.raises(OllamaServerError, match="연결"):
            tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="kimi-k2.7-code:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )


def test_prepare_image_bytes_logs_traceback_on_unexpected_error(
    tool: VlmOcrTool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When VLM_OCR_LOG=1, an unexpected image preparation error prints a traceback."""
    monkeypatch.setenv("VLM_OCR_LOG", "1")

    def _always_fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(Image, "open", _always_fail)

    with pytest.raises(ValueError, match="OCR용으로 준비"):
        tool._prepare_image_bytes(b"not an image")

    captured = capsys.readouterr()
    assert "RuntimeError: boom" in captured.err or "Traceback" in captured.err


def test_resolve_think_flag(tool: VlmOcrTool) -> None:
    assert tool._resolve_think_flag("qwen3.5:cloud", "auto") is True
    assert tool._resolve_think_flag("kimi-k2.7-code:cloud", "auto") is False
    assert tool._resolve_think_flag("any-model", "true") is True
    assert tool._resolve_think_flag("any-model", "false") is False


def test_get_ollama_timeout_defaults(tool: VlmOcrTool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLM_OCR_TIMEOUT", raising=False)
    assert tool._get_ollama_timeout() == (10, 3600)


def test_get_ollama_timeout_rejects_invalid_env(
    tool: VlmOcrTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VLM_OCR_TIMEOUT", "not-a-number")
    assert tool._get_ollama_timeout() == (10, 3600)


# ============================================================
# 에러 메시지가 결과 본문에 절대 포함 안 됨을 검증
# ============================================================

class TestErrorNeverInOutput:
    """알려진 에러 패턴이 정상 결과로 돌아오지 않는지 확인."""

    ERROR_PATTERNS = [
        "Ollama 서버에서 오류 상황을 반환했습니다.",
        "Ollama 서버에서 오류 응답을 반환했습니다.",
        "OCR 처리 중 예기치 않은 오류가 발생했습니다.",
    ]

    def test_error_patterns_not_in_normal_output(self, tool: VlmOcrTool) -> None:
        """정상 OCR 결과에 에러 패턴이 없어야 한다 (당연하지만 회귀 방지)."""
        normal_text = "정상적인 문서 본문입니다. 이것은 OCR로 추출된 내용입니다."
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": normal_text}
        }

        with patch("requests.post", return_value=mock_response):
            result = tool._call_ollama(
                base_url="http://host.docker.internal:11434",
                model="qwen3.5:cloud",
                system_prompt="system",
                user_prompt="user",
                raw_base64="dGVzdA==",
                think=False,
            )

        for pattern in self.ERROR_PATTERNS:
            assert pattern not in result


# ============================================================
# Helper
# ============================================================

def _make_mock_file():
    """Create a minimal mock File object for _invoke tests."""
    from unittest.mock import MagicMock
    from dify_plugin.file.file import File
    mock_file = MagicMock(spec=File)
    mock_file.filename = "test.jpg"
    mock_file.extension = "jpg"
    mock_file.mime_type = "image/jpeg"
    mock_file.url = "http://localhost/test.jpg"
    mock_file.blob = _make_image_bytes(100, 100)
    return mock_file
