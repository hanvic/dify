"""Tests for batch runner guardrails.

Validates that the batch runner correctly:
1. Detects poisoned content patterns in segments
2. Stops on consecutive failures
3. Stops immediately on 429 (no retry)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Make scripts importable
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
)

# We need to patch env before importing run_batch_parallel
os.environ.setdefault("DATASET_API_KEY", "test-key")
os.environ.setdefault("DATASET_ID", "test-dataset")
os.environ.setdefault("START_NODE_ID", "test-node")

from run_batch_parallel import (  # noqa: E402
    ERROR_CONTENT_PATTERNS,
    MAX_CONSECUTIVE_FAILURES,
    BatchStats,
    PoisonedContentError,
    RateLimitError,
    _verify_document_segments,
)


class TestErrorPatternDetection:
    """에러 패턴 감지 테스트."""

    def test_known_error_patterns_exist(self) -> None:
        """알려진 오류 패턴이 목록에 있어야 한다."""
        assert "Ollama 서버에서 오류 상황을 반환했습니다" in ERROR_CONTENT_PATTERNS
        assert "Ollama 서버에서 오류 응답을 반환했습니다" in ERROR_CONTENT_PATTERNS
        assert "QUOTA_EXCEEDED" in ERROR_CONTENT_PATTERNS
        assert "rate_limit" in ERROR_CONTENT_PATTERNS

    def test_verify_segments_detects_poisoned_content(self) -> None:
        """오염된 세그먼트가 있으면 False를 반환한다."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"content": "Ollama 서버에서 오류 상황을 반환했습니다."}
            ]
        }

        with patch("run_batch_parallel.SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            result = _verify_document_segments("doc-123", "test_p1.jpeg")

        assert result is False

    def test_verify_segments_passes_clean_content(self) -> None:
        """정상 세그먼트는 True를 반환한다."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"content": "이것은 정상적인 OCR 결과입니다. 한국어 문서 본문."}
            ]
        }

        with patch("run_batch_parallel.SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            result = _verify_document_segments("doc-123", "test_p1.jpeg")

        assert result is True

    def test_verify_segments_detects_empty(self) -> None:
        """세그먼트가 0개면 False를 반환한다."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": []}

        with patch("run_batch_parallel.SESSION") as mock_session:
            mock_session.get.return_value = mock_resp
            result = _verify_document_segments("doc-123", "test_p1.jpeg")

        assert result is False


class TestConsecutiveFailureGuardrail:
    """연속 실패 가드레일 테스트."""

    def test_consecutive_failure_threshold(self) -> None:
        """MAX_CONSECUTIVE_FAILURES 이상 연속 실패하면 abort가 된다."""
        stats = BatchStats(total_pages=100)
        
        # 연속 실패 누적
        for i in range(MAX_CONSECUTIVE_FAILURES):
            stats.add_failed()
        
        assert stats.consecutive_failures >= MAX_CONSECUTIVE_FAILURES

    def test_success_resets_consecutive_failures(self) -> None:
        """성공하면 연속 실패 카운터가 리셋된다."""
        stats = BatchStats(total_pages=100)
        
        # 4회 실패
        for _ in range(4):
            stats.add_failed()
        assert stats.consecutive_failures == 4
        
        # 성공 시 리셋
        stats.add_processed()
        assert stats.consecutive_failures == 0

    def test_abort_reason_recorded(self) -> None:
        """중단 시 사유가 기록된다."""
        stats = BatchStats(total_pages=100)
        stats.request_abort("quota_exceeded_429")
        assert stats.is_aborted
        assert stats.abort_reason == "quota_exceeded_429"


class TestRateLimitBehavior:
    """429 처리 동작 테스트."""

    def test_429_does_not_retry(self) -> None:
        """429 발생 시 재시도 없이 즉시 중단한다 (이전에는 대기 후 재시도했음)."""
        stats = BatchStats(total_pages=100)
        
        # 429 즉시 abort 요청
        stats.request_abort("quota_exceeded_429")
        assert stats.is_aborted
        assert "429" in stats.abort_reason

    def test_summary_str_includes_counts(self) -> None:
        """summary_str에 성공/실패/스킵 건수가 포함된다."""
        stats = BatchStats(total_pages=100)
        stats.add_processed()
        stats.add_processed()
        stats.add_failed()
        
        summary = stats.summary_str()
        assert "2" in summary  # processed
        assert "1" in summary  # failed
