"""Tests for the dependency-closure registry (2026-10-05 cli-intent-commands).

Covers `dependency_closure` (membership, deterministic order, cycle failure)
and `_build_registry`'s closure-derived construction plus the derived
video-need flag (Req 6, Req 8).
"""

# CHerSun 2026

from annotationlib import Format
from inspect import signature
from typing import ClassVar

import pytest

from pyqenc.api import extract_streams, process_audio
from pyqenc.app_config import load_app_config
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseOutcome,
    PhaseResult,
    Recovery,
    _build_registry,
    dependency_closure,
)
from pyqenc.phases.audio import AudioPhase
from pyqenc.phases.chunking import ChunkingPhase
from pyqenc.phases.encoding import EncodingPhase
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.merge import MergePhase
from pyqenc.phases.optimization import OptimizationPhase
from pyqenc.phases.probe import ProbePhase

_APP_CONFIG = load_app_config(default_only=True)


class _NoopPhase(Phase):
    """Minimal concrete Phase for declaration-only walks (never run)."""

    DEPENDS_ON: ClassVar[tuple[type[Phase], ...]] = ()

    def _recover(self) -> Recovery:  # pragma: no cover - never run
        raise NotImplementedError

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> PhaseResult:  # pragma: no cover
        raise NotImplementedError

    def _assemble_result(  # pragma: no cover - never run
        self,
        outcome: PhaseOutcome,
        artifacts: list[Artifact],
        message: str,
    ) -> PhaseResult:
        raise NotImplementedError


class TestDependencyClosure:
    """Pure declaration walks — no phases are constructed."""

    def test_audio_terminal_yields_audio_closure(self) -> None:
        assert dependency_closure((AudioPhase,)) == (
            JobPhase, ExtractionPhase, AudioPhase,
        )

    def test_extraction_terminal_yields_job_and_extraction(self) -> None:
        assert dependency_closure((ExtractionPhase,)) == (JobPhase, ExtractionPhase)

    def test_chunking_terminal_reaches_extraction_transitively(self) -> None:
        assert dependency_closure((ChunkingPhase,)) == (
            JobPhase, ExtractionPhase, ProbePhase, ChunkingPhase,
        )

    def test_merge_terminal_walks_the_video_chain(self) -> None:
        assert dependency_closure((MergePhase,)) == (
            JobPhase,
            ExtractionPhase,
            ProbePhase,
            ChunkingPhase,
            OptimizationPhase,
            EncodingPhase,
            MergePhase,
        )

    def test_multi_terminal_order_follows_terminal_sequence(self) -> None:
        # Audio as the first terminal lands its subtree (and itself) before
        # the merge walk starts — the audio-first scheduling of `auto`, now
        # carried by terminal order rather than a dependency declaration.
        assert dependency_closure((AudioPhase, MergePhase)) == (
            JobPhase,
            ExtractionPhase,
            AudioPhase,
            ProbePhase,
            ChunkingPhase,
            OptimizationPhase,
            EncodingPhase,
            MergePhase,
        )

    def test_cycle_fails_loudly(self) -> None:
        class _A(_NoopPhase):
            pass

        class _B(_NoopPhase):
            pass

        _A.DEPENDS_ON = (_B,)
        _B.DEPENDS_ON = (_A,)
        with pytest.raises(ValueError, match="DEPENDS_ON cycle"):
            dependency_closure((_A,))


class TestClosureDerivedRegistry:
    """`_build_registry` constructs exactly the closure; video-need derives."""

    def _registry(self, terminals, plan, tmp_path):
        return _build_registry(
            _APP_CONFIG,
            plan,
            tmp_path / "source.mkv",
            tmp_path,
            force=False,
            cleanup=CleanupLevel.NONE,
            no_metrics=True,
            collector=NoOpMetricsCollector(),
            terminals=terminals,
        )

    def test_audio_registry_contains_audio_closure_only(self, tmp_path) -> None:
        registry = self._registry((AudioPhase,), None, tmp_path)
        assert list(registry) == [JobPhase, ExtractionPhase, AudioPhase]
        # Derived video-need is False: the timestamps artifact and the video
        # row stay untouched in an audio-only run.
        assert registry[ExtractionPhase]._video_required is False

    def test_extraction_registry_needs_no_plan(self, tmp_path) -> None:
        registry = self._registry((ExtractionPhase,), None, tmp_path)
        assert list(registry) == [JobPhase, ExtractionPhase]
        assert registry[ExtractionPhase]._video_required is False

    def test_merge_registry_walks_video_chain_with_video_need(self, tmp_path) -> None:
        plan = _APP_CONFIG.resolve_encoding()
        registry = self._registry((MergePhase,), plan, tmp_path)
        assert list(registry) == [
            JobPhase,
            ExtractionPhase,
            ProbePhase,
            ChunkingPhase,
            OptimizationPhase,
            EncodingPhase,
            MergePhase,
        ]
        assert registry[ExtractionPhase]._video_required is True

    def test_probe_in_closure_without_plan_fails_loudly(self, tmp_path) -> None:
        with pytest.raises(AssertionError, match="video registry carries the plan"):
            self._registry((ChunkingPhase,), None, tmp_path)


class TestApiPlanFreedom:
    """The no-Probe closures' api entry points are plan-free by signature (Req 4)."""

    @pytest.mark.parametrize("func", [extract_streams, process_audio])
    def test_plan_free_signature(self, func) -> None:
        # STRING keeps annotations unevaluated (3.14 lazy annotations would
        # otherwise trip on api.py's TYPE_CHECKING-only names).
        sig = signature(func, annotation_format=Format.STRING)
        assert "plan" not in sig.parameters
