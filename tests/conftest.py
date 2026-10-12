"""Shared fixtures for the whole test suite."""

# CHerSun 2026

import pytest

from pyqenc.utils.process import lower_process_priority


@pytest.fixture(scope="session", autouse=True)
def _lowered_process_priority() -> None:
    """Run the suite (and every child process it spawns) at below-normal priority, same as a pipeline run."""
    lower_process_priority()
