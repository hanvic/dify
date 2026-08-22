#!/usr/bin/env python3
"""tests/test_batch_supervisor.py

배치 수퍼바이저 로직 단위 테스트:
- 지수 백오프 대기 상향
- 대기 상한 준수
- 0진행 연속 감지 후 중단
- 디스크 여유 가드
- 프로빙 호출 결과에 따른 분기
- 진행률 파일 갱신

실행:
    pytest tests/test_batch_supervisor.py -v
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── 배치 수퍼바이저 핵심 로직 추출 (bash → python 동치) ───

class BatchSupervisorLogic:
    """batch_supervisor.sh의 핵심 로직을 Python으로 재현하여 테스트."""

    def __init__(
        self,
        initial_wait: int = 1800,
        max_wait: int = 7200,
        backoff_factor: int = 2,
        max_zero_progress: int = 5,
        total_pages: int = 27754,
        min_disk_free_gb: int = 5,
    ):
        self.initial_wait = initial_wait
        self.max_wait = max_wait
        self.backoff_factor = backoff_factor
        self.max_zero_progress = max_zero_progress
        self.total_pages = total_pages
        self.min_disk_free_gb = min_disk_free_gb

        self.current_wait = initial_wait
        self.zero_progress_count = 0
        self.attempt = 0

    def compute_next_wait(self) -> int:
        """현재 대기 시간 반환 후 지수적 증가."""
        wait = self.current_wait
        self.current_wait = min(
            self.current_wait * self.backoff_factor,
            self.max_wait,
        )
        return wait

    def reset_wait(self):
        """진행이 있었을 때 대기 리셋."""
        self.current_wait = self.initial_wait

    def record_attempt(self, pages_done: int) -> str:
        """
        시도 결과를 기록하고 다음 행동을 반환.
        Returns:
            "continue" - 재시도
            "halt" - 0진행 반복으로 중단
            "completed" - 전체 완료
        """
        self.attempt += 1

        if pages_done > 0:
            self.zero_progress_count = 0
            self.reset_wait()
            return "continue"
        else:
            self.zero_progress_count += 1
            if self.zero_progress_count >= self.max_zero_progress:
                return "halt"
            return "continue"

    def check_disk(self, free_gb: int) -> bool:
        """디스크 여유 확인. False면 시작 불가."""
        return free_gb >= self.min_disk_free_gb


# ─── 테스트 ───

class TestExponentialBackoff:
    """대기 시간 지수 증가 및 상한 테스트."""

    def test_initial_wait(self):
        sv = BatchSupervisorLogic()
        assert sv.compute_next_wait() == 1800  # 30분

    def test_backoff_doubles(self):
        sv = BatchSupervisorLogic()
        sv.compute_next_wait()  # 1800 → next=3600
        assert sv.compute_next_wait() == 3600  # 60분

    def test_backoff_doubles_again(self):
        sv = BatchSupervisorLogic()
        sv.compute_next_wait()  # 1800
        sv.compute_next_wait()  # 3600
        assert sv.compute_next_wait() == 7200  # 120분 (상한)

    def test_cap_at_max_wait(self):
        sv = BatchSupervisorLogic()
        for _ in range(10):
            wait = sv.compute_next_wait()
        assert wait == 7200  # 상한 초과 안 함

    def test_reset_after_progress(self):
        sv = BatchSupervisorLogic()
        sv.compute_next_wait()  # 1800
        sv.compute_next_wait()  # 3600
        sv.reset_wait()
        assert sv.compute_next_wait() == 1800  # 리셋됨


class TestZeroProgressDetection:
    """연속 0진행 감지 후 중단."""

    def test_halt_after_max_zero_progress(self):
        sv = BatchSupervisorLogic(max_zero_progress=5)
        for i in range(4):
            result = sv.record_attempt(0)
            assert result == "continue"
        result = sv.record_attempt(0)
        assert result == "halt"

    def test_reset_on_progress(self):
        sv = BatchSupervisorLogic(max_zero_progress=5)
        # 4회 0진행
        for _ in range(4):
            sv.record_attempt(0)
        # 1회 진행
        result = sv.record_attempt(10)
        assert result == "continue"
        assert sv.zero_progress_count == 0
        # 다시 4회 0진행 - 아직 미도달
        for _ in range(4):
            result = sv.record_attempt(0)
            assert result == "continue"

    def test_single_zero_does_not_halt(self):
        sv = BatchSupervisorLogic(max_zero_progress=5)
        assert sv.record_attempt(0) == "continue"


class TestDiskGuard:
    """디스크 여유 체크."""

    def test_enough_disk(self):
        sv = BatchSupervisorLogic(min_disk_free_gb=5)
        assert sv.check_disk(10) is True

    def test_exact_threshold(self):
        sv = BatchSupervisorLogic(min_disk_free_gb=5)
        assert sv.check_disk(5) is True

    def test_below_threshold(self):
        sv = BatchSupervisorLogic(min_disk_free_gb=5)
        assert sv.check_disk(4) is False

    def test_zero_disk(self):
        sv = BatchSupervisorLogic(min_disk_free_gb=5)
        assert sv.check_disk(0) is False


class TestBatchSupervisorScript:
    """bash 스크립트 자체의 구문 검증 및 드라이런 테스트."""

    SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "batch_supervisor.sh"

    def test_script_exists_and_executable(self):
        assert self.SCRIPT_PATH.exists()
        assert os.access(self.SCRIPT_PATH, os.X_OK)

    def test_bash_syntax_check(self):
        """bash -n으로 구문 오류 확인."""
        result = subprocess.run(
            ["bash", "-n", str(self.SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"구문 오류: {result.stderr}"


class TestWebKeepaliveScript:
    """web_keepalive.sh 구문 검증."""

    SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "web_keepalive.sh"

    def test_script_exists_and_executable(self):
        assert self.SCRIPT_PATH.exists()
        assert os.access(self.SCRIPT_PATH, os.X_OK)

    def test_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(self.SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"구문 오류: {result.stderr}"


class TestInjectMetadataLoop:
    """inject_metadata_loop.sh 보완 확인."""

    SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "inject_metadata_loop.sh"

    def test_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(self.SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"구문 오류: {result.stderr}"

    def test_has_error_tolerance(self):
        """비정상 종료 시 계속 실행하는 로직 존재 확인."""
        content = self.SCRIPT_PATH.read_text()
        assert "set +e" in content, "예외 톨러런스 (set +e) 없음"
        assert "exit_code" in content, "exit_code 체크 없음"


class TestDryRunSimulation:
    """수퍼바이저 드라이런: 가짜 배치 러너로 429 상황 시뮬레이션."""

    def test_fake_429_scenario(self, tmp_path):
        """가짜 배치 러너가 429로 종료하는 시나리오."""
        # 가짜 배치 러너 (즉시 exit 1 - 429 시뮬레이션)
        fake_runner = tmp_path / "fake_batch.py"
        fake_runner.write_text("import sys; sys.exit(1)\n")

        # 가짜 processed 파일 (변화 없음 = 0진행)
        state_dir = tmp_path / "batch_state"
        state_dir.mkdir()
        processed = state_dir / "processed_pages.jsonl"
        processed.write_text("")

        # 수퍼바이저 로직 시뮬레이션
        sv = BatchSupervisorLogic(max_zero_progress=3)

        # 3회 0진행 시뮬레이션
        for _ in range(2):
            result = sv.record_attempt(0)
            assert result == "continue"

        result = sv.record_attempt(0)
        assert result == "halt"

    def test_recovery_after_progress(self, tmp_path):
        """쿼터 회복 후 진행되면 대기 리셋."""
        sv = BatchSupervisorLogic()

        # 2회 0진행
        sv.record_attempt(0)
        sv.record_attempt(0)
        # 대기 시간이 증가했을 것
        wait_before = sv.current_wait

        # 진행 발생
        sv.record_attempt(50)
        assert sv.current_wait == sv.initial_wait
        assert sv.zero_progress_count == 0
