"""Unit tests for OptimizationPhase tolerance re-application from cached results.

Covers requirement 7.7:
- When all strategy results are cached and only tolerance changed, re-select
  without re-encoding.
- Correct strategy selection at various tolerance levels.
- Tolerance of 0% selects exactly one strategy (the best).
- Tolerance of 100% selects all passing strategies.
"""

import logging
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.state import (
    ArtifactState,
    OptimizationParams,
    ProbeState,
    StrategyTestResult,
)
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUALITY_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]

_APP_CONFIG = load_app_config(default_only=True)

# Resolve a few specific strategies for use in tests.
_ALL_STRATEGIES = _APP_CONFIG.encoding.resolved_strategies
_STRATEGY_MAP = {s.display_name(): s for s in _ALL_STRATEGIES}

# Pick 3 well-known strategies that exist in the default config.
_S1 = next(s for s in _ALL_STRATEGIES if s.preset == "slow" and s.profile == "h265-aq")
_S2 = next(s for s in _ALL_STRATEGIES if s.preset == "slow" and s.profile == "h265")
_S3 = next(s for s in _ALL_STRATEGIES if s.preset == "slow" and s.profile == "h265-anime")


def _make_job_phase(
    tmp_path: Path,
    strategies: list[Strategy],
    optimize: bool = True,
    tolerance: float = 5.0,
    force: bool = False,
    cleanup: CleanupLevel = CleanupLevel.NONE,
) -> tuple[JobPhase, Path]:
    """Create and run a JobPhase so that result is populated for downstream phases."""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * 1024)
    work_dir = tmp_path / "work"

    config = _APP_CONFIG.model_copy(deep=True)
    # Set quality targets as raw strings
    config.encoding.targets = ["vmaf-min:93.0"]
    # Set strategy pattern strings matching the requested strategies
    config.encoding.strategies = [f"{s.profile}+{s.preset}" for s in strategies]
    config.encoding.optimize = optimize
    config.encoding.optimize_tolerance = tolerance
    # Reset resolved caches so they get re-resolved from new strings
    config.encoding._resolved_targets   = None
    config.encoding._resolved_strategies = None
    config.encoding.resolve(config.codecs, config.profiles)

    job = JobPhase(
        config, {},
        source     = src,
        work_dir   = work_dir,
        force      = force,
        cleanup    = cleanup,
        no_metrics = True,
        collector  = MagicMock(),
    )
    job.run(dry_run=False)
    return job, work_dir


def _make_phase(
    tmp_path: Path,
    strategies: list[Strategy],
    optimize: bool = True,
    tolerance: float = 5.0,
    force: bool = False,
    cleanup: CleanupLevel = CleanupLevel.NONE,
) -> tuple[OptimizationPhase, Path]:
    """Create an OptimizationPhase with pre-run Job/Probe/Chunking deps wired in.

    OptimizationPhase depends on Job, Probe, and Chunking; the uniform run()
    skeleton resolves those dependencies (via the shared walk) BEFORE any
    cached-reuse/tolerance branch. So the registry must carry all three with a
    completed result — each is a real phase instance whose public ``result`` is
    pre-set to a COMPLETED typed result (the walk then treats them as already
    run without mocking phase internals).
    """
    from pyqenc.phases.chunking import ChunkingPhase as _CP
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.job import JobPhase as _JP
    from pyqenc.phases.probe import ProbePhase as _PP
    from pyqenc.phases.probe import ProbePhaseResult

    job, work_dir = _make_job_phase(
        tmp_path, strategies, optimize=optimize, tolerance=tolerance,
        force=force, cleanup=cleanup,
    )
    config = job._config  # already resolved AppConfig
    phases: PhaseRegistry = {_JP: job}

    probe = _PP(config, phases, collector=MagicMock(), crop_params=None)
    probe.result = ProbePhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "probe complete",
        stream    = None,
    )
    phases[_PP] = probe

    chunking = _CP(config, phases, collector=MagicMock())
    chunking.result = ChunkingPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "chunking complete",
        chunks    = [],
    )
    phases[_CP] = chunking

    phase = OptimizationPhase(config, phases=phases, collector=MagicMock())
    return phase, work_dir


def _persist_optimization(
    work_dir: Path,
    source: Path,
    strategy_results: list[StrategyTestResult],
    tolerance_pct: float,
    selected: list[str],
    *,
    test_chunks: list[str] | None = None,
) -> None:
    """Write optimization.yaml with given results.

    The persisted ``probe`` matches the coherent state the ``_make_phase``
    probe mock reports (``source=None`` → frame_count=0, empty crop), so the
    phase's probe-mismatch validation sees a current persisted state.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    OptimizationParams(
        probe            = ProbeState(frame_count=0, crop=CropParams()),
        test_chunks      = test_chunks if test_chunks is not None else ["chunk-001", "chunk-002"],
        strategy_results = strategy_results,
        tolerance_pct    = tolerance_pct,
        selected         = selected,
    ).save(work_dir / "optimization.yaml")


def _make_results(sizes: list[int]) -> list[StrategyTestResult]:
    """Create StrategyTestResult list for S1, S2, S3 with given sizes."""
    strategies = [_S1, _S2, _S3]
    return [
        StrategyTestResult(strategy=s.display_name(), total_size=sz)
        for s, sz in zip(strategies, sizes)
    ]


# ---------------------------------------------------------------------------
# _apply_tolerance static method
# ---------------------------------------------------------------------------

class TestApplyTolerance:
    """Tests for the tolerance selection logic in isolation."""

    def test_zero_tolerance_selects_best_only(self) -> None:
        results = _make_results([100, 110, 120])
        selected = OptimizationPhase._apply_tolerance(results, 0.0)
        assert len(selected) == 1
        assert selected[0] == _S1.display_name()

    def test_tolerance_includes_within_threshold(self) -> None:
        results = _make_results([100, 104, 120])
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1.display_name() in selected
        assert _S2.display_name() in selected
        assert _S3.display_name() not in selected

    def test_tolerance_100_selects_all(self) -> None:
        results = _make_results([100, 150, 200])
        selected = OptimizationPhase._apply_tolerance(results, 100.0)
        assert len(selected) == 3

    def test_empty_results_returns_empty(self) -> None:
        assert OptimizationPhase._apply_tolerance([], 5.0) == []

    def test_zero_size_results_excluded(self) -> None:
        """Strategies with total_size=0 (failed) are excluded."""
        results = [
            StrategyTestResult(strategy=_S1.display_name(), total_size=0),
            StrategyTestResult(strategy=_S2.display_name(), total_size=100),
        ]
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1.display_name() not in selected
        assert _S2.display_name() in selected

    def test_exact_threshold_boundary_included(self) -> None:
        """A strategy exactly at the threshold (100% * (1 + tol/100)) is included."""
        results = _make_results([100, 105, 200])
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1.display_name() in selected
        assert _S2.display_name() in selected


# ---------------------------------------------------------------------------
# Tolerance re-application from cached results (Req 7.7)
# ---------------------------------------------------------------------------

class TestToleranceReapplication:
    """Tests for re-selecting strategies from cached results when tolerance changes."""

    def test_reapplication_returns_completed_outcome(self, tmp_path: Path) -> None:
        """When all results cached and tolerance changed, the cheap re-select runs (COMPLETED)."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=10.0)
        source = phase._dep_result(JobPhase).source

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir  = work_dir,
            source    = source,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        result = phase.run(dry_run=False)
        # Tolerance re-application is cheap pending work (re-select + save),
        # so the phase COMPLETED it rather than short-circuiting to REUSED.
        assert result.outcome == PhaseOutcome.COMPLETED
        assert result.is_complete is True

    def test_reapplication_updates_selected_strategies(self, tmp_path: Path) -> None:
        """Re-application selects strategies based on new tolerance, not old."""
        strategies = [_S1, _S2, _S3]
        # New tolerance is 25% — should include S3 (20% above best)
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=25.0)
        source = phase._dep_result(JobPhase).source

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = source,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        result = phase.run(dry_run=False)
        assert _S1 in result.selected_strategies
        assert _S2 in result.selected_strategies
        assert _S3 in result.selected_strategies

    def test_reapplication_persists_new_tolerance(self, tmp_path: Path) -> None:
        """After re-application, optimization.yaml is updated with the new tolerance."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=10.0)
        source = phase._dep_result(JobPhase).source

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = source,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        phase.run(dry_run=False)

        persisted = OptimizationParams.load(work_dir / "optimization.yaml")
        assert persisted is not None
        assert persisted.tolerance_pct == 10.0

    def test_no_reapplication_when_tolerance_unchanged(self, tmp_path: Path) -> None:
        """When tolerance is unchanged and all results cached, outcome is REUSED (fast path)."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=5.0)
        source = phase._dep_result(JobPhase).source

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = source,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        result = phase.run(dry_run=False)
        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True
        assert _S1 in result.selected_strategies
        assert _S2 in result.selected_strategies

    def test_reapplication_zero_tolerance_selects_one(self, tmp_path: Path) -> None:
        """Changing tolerance to 0% selects exactly the best strategy."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=0.0)
        source = phase._dep_result(JobPhase).source

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = source,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        result = phase.run(dry_run=False)
        assert len(result.selected_strategies) == 1
        assert result.selected_strategies[0] == _S1

    def test_partial_results_not_reapplied(self, tmp_path: Path) -> None:
        """Re-application only triggers when ALL strategies have cached results."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=10.0)
        source = phase._dep_result(JobPhase).source

        # Only 2 of 3 strategies have results
        partial_results = [
            StrategyTestResult(strategy=_S1.display_name(), total_size=100),
            StrategyTestResult(strategy=_S2.display_name(), total_size=104),
        ]
        _persist_optimization(
            work_dir         = work_dir,
            source           = source,
            strategy_results = partial_results,
            tolerance_pct    = 5.0,
            selected         = [_S1.display_name(), _S2.display_name()],
        )

        result = phase.run(dry_run=False)
        assert result.outcome != PhaseOutcome.REUSED


# ---------------------------------------------------------------------------
# All-strategies mode
# ---------------------------------------------------------------------------

class TestAllStrategiesMode:
    """Tests for all-strategies mode (optimize=False)."""

    def test_returns_all_configured_strategies(self, tmp_path: Path) -> None:
        strategies = [_S1, _S2, _S3]
        phase, _ = _make_phase(tmp_path, strategies, optimize=False)
        result = phase.run(dry_run=False)

        assert result.is_complete is True
        assert sorted(s.display_name() for s in result.selected_strategies) == sorted(s.display_name() for s in strategies)

    def test_no_winners_in_all_strategies_mode(self, tmp_path: Path) -> None:
        """All-strategies mode runs no test encodes — no winner rows, no
        aggregated records on the result (they stay in optimization.yaml)."""
        phase, _ = _make_phase(tmp_path, [_S1, _S2], optimize=False)
        result = phase.run(dry_run=False)

        assert result.winners == []

# ---------------------------------------------------------------------------
# The per-pair ledger (Req 8 — Correctness Property 5)
# ---------------------------------------------------------------------------

def _make_chunk(cid_start: float, cid_end: float, tmp_path: Path):
    """A real VideoStreamChunk over a minimal extended stream."""
    from fractions import Fraction

    from pyqenc.stream_model import (
        ExtendedVideoStream,
        File,
        VideoStream,
        VideoStreamChunk,
        VideoStreamInfo,
    )
    return VideoStreamChunk(
        stream=ExtendedVideoStream(
            stream=VideoStream(
                file=File(path=tmp_path / "source.mkv", file_size_bytes=64),
                info=VideoStreamInfo(
                    track_id=0, codec_name="hevc", fps=24.0,
                    fps_fraction=Fraction(24, 1), resolution="1920x1080",
                    duration_seconds=100.0,
                ),
            ),
            frame_count=2400,
            crop=CropParams(),
        ),
        start_timestamp=cid_start,
        end_timestamp=cid_end,
        frame_count=24,
    )


class TestPairLedger:
    def _phase_with_chunks(
        self,
        tmp_path: Path,
        n_strategies: int,
        chunks: list,
    ):
        """Wire a real chunk set into the phase's ChunkingPhase dependency.

        ``chunks`` are used verbatim as the test set (no random selection):
        the persisted-selection path in ``_resolve_test_chunks`` reads them
        back by id.
        """
        from pyqenc.phases.chunking import ChunkingPhaseResult
        phase, work_dir = _make_phase(tmp_path, [_S1, _S2, _S3][:n_strategies])
        chunking = next(
            ph for cls, ph in phase._phases.items() if cls.__name__ == "ChunkingPhase"
        )
        chunking.result = ChunkingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            message   = "chunking complete",
            chunks    = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks],
        )
        # Persist the full chunk set as the test selection so recovery is
        # deterministic (the fresh random pick stays covered by the e2e run).
        _persist_optimization(
            work_dir, tmp_path / "source.mkv", [],
            tolerance_pct=5.0, selected=[],
            test_chunks=[c.safe_name() for c in chunks],
        )
        return phase, work_dir, chunks

    def test_fresh_ledger_counts_attempts_not_strategies(self, tmp_path: Path, caplog) -> None:
        """Bug guarded (Req 8.1): the recovery line counting per-strategy
        records instead of winning attempts — 3 test chunks x 3 strategies
        is 9 attempts to produce, and the line must say so."""
        import logging as _logging

        chunks = [_make_chunk(float(i * 10), float((i + 1) * 10), tmp_path) for i in range(3)]
        phase, _, _ = self._phase_with_chunks(tmp_path, 3, chunks)
        with caplog.at_level(_logging.INFO):
            result = phase.run(dry_run=True)

        assert result.outcome == PhaseOutcome.PENDING
        assert any(
            "Recovery: 9 total, 9 wanted (0 complete, 0 partial, 9 absent) — nothing to reuse"
            in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records if "Recovery" in r.message]

    def test_ledger_size_is_chunks_times_strategies(self, tmp_path: Path) -> None:
        """Correctness Property 5: ledger size == |test chunks| x |strategies|;
        row states are per-pair and presence-based."""
        chunks = [_make_chunk(float(i * 10), float((i + 1) * 10), tmp_path) for i in range(2)]
        phase, _, chunks = self._phase_with_chunks(tmp_path, 3, chunks)
        recovery = phase._recover()
        assert len(recovery.artifacts) == 6
        assert all(
            a.state == ArtifactState.ABSENT for a in recovery.artifacts
        )
        assert {a.payload.strategy.display_name() for a in recovery.artifacts} == {
            _S1.display_name(), _S2.display_name(), _S3.display_name(),
        }
        assert {a.payload.chunk.safe_name() for a in recovery.artifacts} == {
            c.safe_name() for c in chunks
        }

    def test_complete_only_when_winner_on_disk(self, tmp_path: Path) -> None:
        """Bug guarded (Req 8.2): a row aggregated as cached while its winner
        is missing on disk — the attempt would silently re-run or, worse, be
        reported as produced."""
        chunks = [_make_chunk(0.0, 10.0, tmp_path)]
        phase, work_dir, chunks = self._phase_with_chunks(tmp_path, 1, chunks)
        recovery = phase._recover()
        assert all(a.state == ArtifactState.ABSENT for a in recovery.artifacts)
        assert recovery.pending is True

        # Fabricate the winner (file + result sidecar) for the single pair.
        from decimal import Decimal

        from pyqenc.constants import ENCODED_OUTPUT_DIR
        from pyqenc.phases.encoding import EncodingResultSidecar
        from pyqenc.stream_model import EncodedChunk as _EC
        from pyqenc.utils.yaml_utils import write_yaml_atomic

        chunk = chunks[0]
        strat = _S1
        strategy_dir = work_dir / ENCODED_OUTPUT_DIR / strat.safe_name()
        strategy_dir.mkdir(parents=True, exist_ok=True)
        winner = strategy_dir / _EC.format_file_name(
            chunk.safe_name(), "1920x1080", Decimal(20))
        winner.write_bytes(b"x" * 16)
        write_yaml_atomic(
            strategy_dir / f"{chunk.safe_name()}.1920x1080.yaml",
            EncodingResultSidecar(
                winning_attempt=winner.name, crf=Decimal(20),
                metrics={}, targets_met=True,
            ).model_dump(exclude_none=True),
        )

        phase2, _, _ = self._phase_with_chunks(tmp_path, 1, chunks)
        recovery2 = phase2._recover()
        assert [a.state for a in recovery2.artifacts] == [ArtifactState.COMPLETE]
        assert recovery2.pending is False
        assert recovery2.artifacts[0].payload.crf == Decimal(20)



# ---------------------------------------------------------------------------
# Fixed-mode entry: cleanup guard, winner wipe, banner (Req 5-7)
# ---------------------------------------------------------------------------

from decimal import Decimal

from pyqenc.constants import ENCODED_OUTPUT_DIR


def _make_fixed_phase(
    tmp_path: Path,
    *,
    strategy_names: list[str],
    cleanup: CleanupLevel = CleanupLevel.NONE,
    optimize: bool = True,
) -> tuple[OptimizationPhase, Path, AppConfig]:
    """An OptimizationPhase harness whose config pins the knob via -q semantics.

    The override is applied exactly as ``_build_config`` applies it: assigned
    on the config, then strategies re-resolved — ``fixed_quality`` derives
    True for every matched strategy.
    """
    from pyqenc.phases.chunking import ChunkingPhase as _CP
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.job import JobPhase as _JP
    from pyqenc.phases.probe import ProbePhase as _PP
    from pyqenc.phases.probe import ProbePhaseResult

    config = _APP_CONFIG.model_copy(deep=True)
    config.encoding.strategies = strategy_names
    config.encoding.quality_range_override = (Decimal("18"), Decimal("18"))
    config.encoding.optimize = optimize
    config.encoding.resolve(config.codecs, config.profiles)
    assert config.encoding.fixed_quality, "harness must derive a fixed run"

    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * 1024)
    work_dir = tmp_path / "work"

    job = JobPhase(
        config, {},
        source     = src,
        work_dir   = work_dir,
        force      = False,
        cleanup    = cleanup,
        no_metrics = True,
        collector  = MagicMock(),
    )
    job.run(dry_run=False)
    phases: PhaseRegistry = {_JP: job}

    probe = _PP(config, phases, collector=MagicMock(), crop_params=None)
    probe.result = ProbePhaseResult(outcome=PhaseOutcome.COMPLETED, message="stub", stream=None)
    phases[_PP] = probe

    chunking = _CP(config, phases, collector=MagicMock())
    chunking.result = ChunkingPhaseResult(outcome=PhaseOutcome.COMPLETED, message="stub", chunks=[])
    phases[_CP] = chunking

    phase = OptimizationPhase(config, phases=phases, collector=MagicMock())
    return phase, work_dir, config


def _seed_encoded_winner(work_dir: Path, strategy: Strategy) -> Path:
    """Create an ``encoded/<strategy>/`` winner (file + sidecar) to observe wipes."""
    strategy_dir = work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name()
    strategy_dir.mkdir(parents=True, exist_ok=True)
    winner = strategy_dir / "chunk-001.1920x1080.q18.mkv"
    winner.write_bytes(b"x" * 32)
    (strategy_dir / "chunk-001.1920x1080.yaml").write_text("crf: 18\n", encoding="utf-8")
    return strategy_dir


class TestCleanupGuard:
    """Fixed + cleanup >= INTERMEDIATE hard-stops before any wipe/encode work."""

    @pytest.mark.parametrize("level", [CleanupLevel.INTERMEDIATE, CleanupLevel.ALL])
    def test_guard_stops_fixed_run(self, tmp_path: Path, level: CleanupLevel) -> None:
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], cleanup=level,
        )
        seeded = _seed_encoded_winner(work_dir, phase._config.encoding.resolved_strategies[0])
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.FAILED
        assert "cleanup" in result.message and "re-derivation substrate" in result.message
        # The guard fires BEFORE the wipe — the winner layer stays intact.
        assert seeded.exists()

    def test_guard_passes_fixed_none(self, tmp_path: Path) -> None:
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], cleanup=CleanupLevel.NONE,
        )
        seeded = _seed_encoded_winner(work_dir, phase._config.encoding.resolved_strategies[0])
        phase.run(dry_run=False)
        # No guard failure — the wipe ran instead.
        assert not seeded.exists()

    def test_guard_never_applies_to_searched_runs(self, tmp_path: Path) -> None:
        phase, work_dir = _make_phase(
            tmp_path, [_S1, _S2], optimize=False, force=False,
            cleanup=CleanupLevel.ALL,
        )
        # Search mode with ALL cleanup: no fixed-quality guard may fire (the
        # phase proceeds to its normal outcome instead of a guard stop).
        seeded = _seed_encoded_winner(work_dir, _S1)
        result = phase.run(dry_run=False)
        assert "cleanup" not in result.message
        assert seeded.exists()


class TestFixedWinnerWipe:
    """The winner layer is wiped unconditionally on every fixed start (Req 6)."""

    def test_wipe_on_all_strategies_path(self, tmp_path: Path) -> None:
        # Single fixed strategy + optimize on → all-strategies skip path.
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=True,
        )
        seeded = _seed_encoded_winner(work_dir, phase._config.encoding.resolved_strategies[0])
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED  # all-strategies skip result
        assert not seeded.exists()

    def test_wipe_on_optimize_path(self, tmp_path: Path) -> None:
        # Two fixed strategies + optimize on → the test-encode path. The
        # stubbed chunking has no chunks, so recovery fails AFTER the entry
        # block already ran — the wipe and banner are what must have fired.
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow", "h264+slow"], optimize=True,
        )
        strategies = phase._config.encoding.resolved_strategies
        seeded = [_seed_encoded_winner(work_dir, s) for s in strategies]
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.FAILED
        assert "No chunks" in result.message
        assert not any(d.exists() for d in seeded)

    def test_wipe_never_on_searched_runs(self, tmp_path: Path) -> None:
        phase, work_dir = _make_phase(tmp_path, [_S1], optimize=False, force=False)
        seeded = _seed_encoded_winner(work_dir, _S1)
        phase.run(dry_run=False)
        assert seeded.exists()

    def test_wipe_skipped_on_dry_run(self, tmp_path: Path) -> None:
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=False,
        )
        seeded = _seed_encoded_winner(work_dir, phase._config.encoding.resolved_strategies[0])
        phase.run(dry_run=True)
        # A dry run changes nothing — the wipe waits for the executing run.
        assert seeded.exists()


class TestFixedQualityBanner:
    """One prominent WARNING banner per fixed run; never on searched runs (Req 5)."""

    def _banner_count(self, caplog: pytest.LogCaptureFixture) -> int:
        return caplog.text.count("FIXED QUALITY MODE")

    def test_banner_single_strategy(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="pyqenc.phases.optimization"):
            phase, _, _ = _make_fixed_phase(tmp_path, strategy_names=["h265-aq+slow"])
            phase.run(dry_run=False)
        assert self._banner_count(caplog) == 1
        assert "CRF=18" in caplog.text
        assert "per-chunk quality search disabled" in caplog.text
        assert "NOT comparable across encoder families" in caplog.text
        # Single strategy: no every-survivor-encodes line.
        assert "fully encode the video" not in caplog.text

    def test_banner_multi_strategy_heads_up(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="pyqenc.phases.optimization"):
            phase, _, _ = _make_fixed_phase(
                tmp_path, strategy_names=["h265-aq+slow", "h264+slow"],
            )
            phase.run(dry_run=True)
        assert self._banner_count(caplog) == 1
        assert "every surviving strategy will fully encode the video" in caplog.text

    def test_banner_emitted_at_warning_level(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        phase, _, _ = _make_fixed_phase(tmp_path, strategy_names=["h265-aq+slow"])
        phase.run(dry_run=False)
        banner_records = [r for r in caplog.records if "FIXED QUALITY MODE" in r.getMessage()]
        assert banner_records and all(r.levelno == logging.WARNING for r in banner_records)

    def test_no_banner_on_searched_runs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        phase, _ = _make_phase(tmp_path, [_S1, _S2], optimize=False, force=False)
        phase.run(dry_run=False)
        assert self._banner_count(caplog) == 0

    def test_banner_lists_per_strategy_knobs_when_not_uniform(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Collapsed config profiles of different values (no -q): the banner
        # falls back to a per-strategy pinned-knob listing.
        from pyqenc.phases.chunking import ChunkingPhase as _CP
        from pyqenc.phases.chunking import ChunkingPhaseResult
        from pyqenc.phases.probe import ProbePhase as _PP
        from pyqenc.phases.probe import ProbePhaseResult

        config_dict = _APP_CONFIG.model_dump()
        config_dict["profiles"]["h265-aq"]["quality_range"] = [18.0, 18.0]
        config_dict["profiles"]["h264"]["quality_range"] = [20.0, 20.0]
        config_dict["encoding"]["strategies"] = ["h265-aq", "h264"]
        config = AppConfig.model_validate(config_dict)
        assert config.encoding.fixed_quality

        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1024)
        job = JobPhase(
            config, {}, source=src, work_dir=tmp_path / "work", force=False,
            cleanup=CleanupLevel.NONE, no_metrics=True, collector=MagicMock(),
        )
        job.run(dry_run=False)
        phases: PhaseRegistry = {JobPhase: job}
        probe = _PP(config, phases, collector=MagicMock(), crop_params=None)
        probe.result = ProbePhaseResult(outcome=PhaseOutcome.COMPLETED, message="stub", stream=None)
        phases[_PP] = probe
        chunking = _CP(config, phases, collector=MagicMock())
        chunking.result = ChunkingPhaseResult(outcome=PhaseOutcome.COMPLETED, message="stub", chunks=[])
        phases[_CP] = chunking

        phase = OptimizationPhase(config, phases=phases, collector=MagicMock())
        with caplog.at_level(logging.WARNING, logger="pyqenc.phases.optimization"):
            phase.run(dry_run=False)
        assert "knob pinned (h265-aq+slow: CRF=18.0, h264+veryslow: CRF=20.0)" in caplog.text

# ---------------------------------------------------------------------------
# Fixed compared-run optimization: pruning, anchor, synthetic set (Req 8)
# ---------------------------------------------------------------------------

def _result(name: str, size: int, metrics: dict[str, float]) -> StrategyTestResult:
    """A StrategyTestResult with metrics (the fixed-mode pruning input)."""
    return StrategyTestResult(strategy=name, total_size=size, metrics=metrics)


class TestDominancePruning:
    """Pareto dominance pruning — selection removes only dominated strategies."""

    _M: ClassVar[dict[str, float]] = {"vmaf_median": 91.0, "vif_median": 84.0}

    def test_h264_pruned_by_h265(self) -> None:
        """Strictly-worse (bigger AND lower metrics) strategy is pruned."""
        results = [
            _result("h265+slow", 1000, {"vmaf_median": 93.0, "vif_median": 90.0}),
            _result("h264+slow", 1200, {"vmaf_median": 91.5, "vif_median": 85.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["h265+slow"]

    def test_av1_vs_h265_both_survive(self) -> None:
        """Smaller-but-worse vs bigger-but-better — neither dominates."""
        results = [
            _result("av1+slow", 1000, {"vmaf_median": 91.0, "vif_median": 84.0}),
            _result("h265+slow", 1400, {"vmaf_median": 93.0, "vif_median": 90.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == [
            "av1+slow", "h265+slow",
        ]

    def test_exact_duplicates_coexist(self) -> None:
        """Equal size and equal metrics both directions — no strict inequality,
        no dominance, both survive."""
        results = [
            _result("a+slow", 1000, dict(self._M)),
            _result("b+slow", 1000, dict(self._M)),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["a+slow", "b+slow"]

    def test_size_tie_metric_advantage_dominates(self) -> None:
        """Equal size, better metrics on one — the better one dominates."""
        results = [
            _result("a+slow", 1000, {"vmaf_median": 90.0}),
            _result("b+slow", 1000, {"vmaf_median": 91.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["b+slow"]

    def test_failed_strategies_excluded(self) -> None:
        """total_size == 0 (failed encodes) never survives."""
        results = [
            _result("a+slow", 0, {}),
            _result("b+slow", 1000, {"vmaf_median": 91.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["b+slow"]

    def test_metric_key_mismatch_incomparable(self) -> None:
        """Partial measurements cannot be honestly compared — both survive."""
        results = [
            _result("a+slow", 1000, {"vmaf_median": 91.0}),
            _result("b+slow", 1400, {"vmaf_median": 93.0, "vif_median": 90.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["a+slow", "b+slow"]

    def test_all_measured_metrics_evaluated(self) -> None:
        """Dominance needs advantage on every metric — a single deficit blocks
        the prune even when every other metric wins."""
        results = [
            _result("a+slow", 1000, {"vmaf_median": 93.0, "vif_median": 83.9}),
            _result("b+slow", 1400, {"vmaf_median": 91.0, "vif_median": 84.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["a+slow", "b+slow"]


class TestAnchorSelection:
    """The anchor is chosen AFTER pruning, from survivors (Req 8.2)."""

    def test_smallest_survivor_anchors(self) -> None:
        results = [
            _result("av1+slow", 1000, {"vmaf_median": 91.0}),
            _result("h265+slow", 1400, {"vmaf_median": 93.0}),
        ]
        survivors = OptimizationPhase._dominance_survivors(results)
        assert OptimizationPhase._select_anchor(
            results, ["av1+slow", "h265+slow"], survivors,
        ) == "av1+slow"

    def test_dominated_size_minimum_not_anchor(self) -> None:
        """The overall size minimum can itself be dominated (equal size,
        worse metrics) — pruning first guarantees the ruler is on the front."""
        results = [
            _result("a+slow", 900, {"vmaf_median": 80.0}),
            _result("b+slow", 900, {"vmaf_median": 85.0}),
            _result("c+slow", 1200, {"vmaf_median": 90.0}),
        ]
        survivors = OptimizationPhase._dominance_survivors(results)
        assert survivors == ["b+slow", "c+slow"]
        assert OptimizationPhase._select_anchor(
            results, ["a+slow", "b+slow", "c+slow"], survivors,
        ) == "b+slow"

    def test_size_tie_breaks_by_resolved_order(self) -> None:
        """Exact-duplicate survivors at equal size: resolved order decides."""
        results = [
            _result("a+slow", 1000, {"vmaf_median": 91.0}),
            _result("b+slow", 1000, {"vmaf_median": 91.0}),
        ]
        survivors = OptimizationPhase._dominance_survivors(results)
        assert survivors == ["a+slow", "b+slow"]
        assert OptimizationPhase._select_anchor(
            results, ["b+slow", "a+slow"], survivors,
        ) == "b+slow"

    def test_unmeasured_survivor_cannot_anchor(self) -> None:
        results = [
            _result("a+slow", 1000, {}),
            _result("b+slow", 1400, {"vmaf_median": 91.0}),
        ]
        survivors = OptimizationPhase._dominance_survivors(results)
        assert OptimizationPhase._select_anchor(
            results, ["a+slow", "b+slow"], survivors,
        ) == "b+slow"

    def test_no_measured_survivors_no_anchor(self) -> None:
        results = [_result("a+slow", 1000, {})]
        assert OptimizationPhase._select_anchor(results, ["a+slow"], ["a+slow"]) is None


class TestSyntheticTargets:
    """The synthetic set mirrors the anchor's min-aggregated metrics (Req 8.3)."""

    def test_all_measured_metrics_covered_sorted(self) -> None:
        anchor = _result(
            "av1+slow", 1000,
            {"vif_median": 84.0, "vmaf_median": 91.2},
        )
        targets = OptimizationPhase._synthetic_targets_from(anchor)
        assert [(t.metric, t.statistic, t.value) for t in targets] == [
            ("vif", "median", 84.0),
            ("vmaf", "median", 91.2),
        ]

    def test_none_anchor_empty_set(self) -> None:
        assert OptimizationPhase._synthetic_targets_from(None) == []


class TestAggregateStrategyMetrics:
    """Min-across-test-chunks aggregation from winner result sidecars."""

    def test_min_across_chunks(self, tmp_path: Path) -> None:
        from pyqenc.phases.encoding import build_encoded_chunk
        from pyqenc.stream_model import EncodedChunk

        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        strategy = _S1
        encoded_chunks: dict[str, dict[str, EncodedChunk]] = {}
        for i, chunk in enumerate(chunks):
            name = f"{chunk.safe_name()}.1920x1080.q18.0"
            strategy_dir = tmp_path / "encoded" / strategy.safe_name()
            strategy_dir.mkdir(parents=True, exist_ok=True)
            (strategy_dir / f"{name}.mkv").write_bytes(b"x" * (100 * (i + 1)))
            # Result sidecar naming: <chunk_id>.<resolution>.yaml (no q part).
            write_yaml_atomic(
                strategy_dir / f"{chunk.safe_name()}.1920x1080.yaml",
                {"metrics": {"vmaf_median": 91.0 + i, "vif_median": 84.0 - i}},
            )
            encoded_chunks.setdefault(chunk.safe_name(), {})[strategy.display_name()] = (
                build_encoded_chunk(
                    chunk=chunk, strategy=strategy, crf=Decimal("18.0"),
                    path=strategy_dir / f"{name}.mkv", resolution="1920x1080",
                    frame_count=24,
                )
            )

        phase, _, _ = _make_fixed_phase(tmp_path, strategy_names=["h265-aq+slow"])
        mins = phase._aggregate_strategy_metrics(
            tmp_path, chunks, strategy, encoded_chunks,
        )
        assert mins == {"vmaf_median": 91.0, "vif_median": 83.0}

    def test_missing_sidecar_chunk_contributes_nothing(self, tmp_path: Path) -> None:
        from pyqenc.phases.encoding import build_encoded_chunk

        chunk = _make_chunk(0.0, 10.0, tmp_path)
        strategy = _S1
        strategy_dir = tmp_path / "encoded" / strategy.safe_name()
        strategy_dir.mkdir(parents=True, exist_ok=True)
        mkv = strategy_dir / f"{chunk.safe_name()}.1920x1080.q18.0.mkv"
        mkv.write_bytes(b"x" * 100)
        # No sidecar next to the winner.
        encoded_chunks = {
            chunk.safe_name(): {
                strategy.display_name(): build_encoded_chunk(
                    chunk=chunk, strategy=strategy, crf=Decimal("18.0"),
                    path=mkv, resolution="1920x1080", frame_count=24,
                ),
            },
        }
        phase, _, _ = _make_fixed_phase(tmp_path, strategy_names=["h265-aq+slow"])
        assert phase._aggregate_strategy_metrics(
            tmp_path, [chunk], strategy, encoded_chunks,
        ) == {}
class TestFixedComparedExecute:
    """The fixed compared-run execute path end to end (mocked encoder pool).

    Scenario (the design table): av1-analog smallest with lower metrics,
    h265-analog bigger with better metrics, h264-analog biggest with metrics
    strictly between — dominated by the h265-analog. Survivors are the first
    two; the anchor is the smallest survivor; the synthetic set mirrors the
    anchor's min-aggregated metrics; tolerance is never consulted.
    """

    def _run_scenario(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        tolerance: float,
        sizes: dict[str, tuple[int, int]],
        metrics: dict[str, dict[str, float]],
    ) -> tuple[OptimizationPhaseResult, Path]:
        """Run a fixed compared optimization with fabricated test winners.

        The encoder pool is replaced by a stub that seeds each strategy's
        winner files + result sidecars (post-wipe) and returns the composed
        encoded-chunk map — sizes come from the fabricated files, metrics
        from the sidecars.
        """
        from pyqenc.phases.encoding import (
            EncodingResult,
            build_encoded_chunk,
        )

        strategy_names = ["h265-aq", "h265", "h265-anime"]
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=[f"{n}+slow" for n in strategy_names],
            optimize=True,
        )
        phase._config.encoding.optimize_tolerance = tolerance
        # Assignment on EncodingConfig invalidates the resolved caches —
        # re-resolve so the run sees the same strategies under the new tolerance.
        phase._config.encoding.resolve(phase._config.codecs, phase._config.profiles)

        chunks = [_make_chunk(float(i * 10), float((i + 1) * 10), tmp_path) for i in range(2)]
        from pyqenc.phases.chunking import ChunkingPhaseResult
        chunking = next(
            ph for cls, ph in phase._phases.items() if cls.__name__ == "ChunkingPhase"
        )
        chunking.result = ChunkingPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            chunks=[Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks],
        )
        # Persist the full chunk set as the test selection (deterministic).
        _persist_optimization(
            work_dir, tmp_path / "source.mkv", [],
            tolerance_pct=tolerance, selected=[],
            test_chunks=[c.safe_name() for c in chunks],
        )

        strategies = phase._config.encoding.resolved_strategies

        def _seed_and_compose() -> EncodingResult:
            result = EncodingResult()
            for chunk_idx, chunk in enumerate(chunks):
                for strategy in strategies:
                    display = strategy.display_name()
                    per_chunk = sizes[display][chunk_idx]
                    strategy_dir = work_dir / "encoded" / strategy.safe_name()
                    strategy_dir.mkdir(parents=True, exist_ok=True)
                    mkv = strategy_dir / f"{chunk.safe_name()}.1920x1080.q18.0.mkv"
                    mkv.write_bytes(b"x" * per_chunk)
                    write_yaml_atomic(
                        strategy_dir / f"{chunk.safe_name()}.1920x1080.yaml",
                        {"crf": "18.0", "targets_met": True, "metrics": metrics[display]},
                    )
                    result.encoded_chunks.setdefault(chunk.safe_name(), {})[display] = (
                        build_encoded_chunk(
                            chunk=chunk, strategy=strategy, crf=Decimal("18.0"),
                            path=mkv, resolution="1920x1080", frame_count=24,
                        )
                    )
            return result

        async def _fake_parallel(**_kwargs: object) -> EncodingResult:
            return _seed_and_compose()

        monkeypatch.setattr(
            "pyqenc.phases.optimization._make_encoder", lambda **_kw: MagicMock(),
        )
        monkeypatch.setattr(
            "pyqenc.phases.encoding._encode_chunks_parallel", _fake_parallel,
        )
        result = phase.run(dry_run=False)
        return result, work_dir

    _SCENARIO_SIZES: ClassVar[dict[str, tuple[int, int]]] = {
        # chunk file sizes; totals: h265-aq 1000 (anchor), h265 1400, h265-anime 1600
        "h265-aq+slow":     (500, 500),
        "h265+slow":        (700, 700),
        "h265-anime+slow":  (800, 800),
    }
    _SCENARIO_METRICS: ClassVar[dict[str, dict[str, float]]] = {
        "h265-aq+slow":     {"vmaf_median": 91.0, "vif_median": 84.0},
        "h265+slow":        {"vmaf_median": 93.1, "vif_median": 90.3},
        "h265-anime+slow":  {"vmaf_median": 91.7, "vif_median": 85.0},
    }

    def test_pruning_anchor_synthetic_and_persistence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result, work_dir = self._run_scenario(
            tmp_path, monkeypatch,
            tolerance=5.0,
            sizes=self._SCENARIO_SIZES,
            metrics=self._SCENARIO_METRICS,
        )
        assert result.outcome is PhaseOutcome.COMPLETED

        survivor_names = [s.display_name() for s in result.selected_strategies]
        assert survivor_names == ["h265-aq+slow", "h265+slow"]

        # The anchor is the smallest survivor; the synthetic set mirrors its
        # min-aggregated metrics (sorted).
        assert [(t.metric, t.statistic, t.value) for t in result.synthetic_targets] == [
            ("vif", "median", 84.0),
            ("vmaf", "median", 91.0),
        ]

        persisted = OptimizationParams.load(work_dir / "optimization.yaml")
        assert persisted is not None
        assert persisted.anchor == "h265-aq+slow"
        assert persisted.selected == ["h265-aq+slow", "h265+slow"]
        assert [
            (t.metric, t.statistic, t.value) for t in persisted.synthetic_targets
        ] == [
            ("vif", "median", 84.0),
            ("vmaf", "median", 91.0),
        ]
        # The per-strategy records carry the aggregated metrics for reuse.
        by_name = {r.strategy: r for r in persisted.strategy_results}
        assert by_name["h265-aq+slow"].metrics["vmaf_median"] == 91.0

    def test_tolerance_not_applied_in_fixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 100% tolerance (searched mode would select everything) must not
        rescue the dominated strategy."""
        result, _ = self._run_scenario(
            tmp_path, monkeypatch,
            tolerance=100.0,
            sizes=self._SCENARIO_SIZES,
            metrics=self._SCENARIO_METRICS,
        )
        survivor_names = [s.display_name() for s in result.selected_strategies]
        assert "h265-anime+slow" not in survivor_names

    def test_comparison_table_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="pyqenc.phases.optimization"):
            self._run_scenario(
                tmp_path, monkeypatch,
                tolerance=5.0,
                sizes=self._SCENARIO_SIZES,
                metrics=self._SCENARIO_METRICS,
            )
        text = caplog.text
        assert "Fixed-quality comparison — ruler: h265-aq+slow (smallest test size)" in text
        # Anchor row shows absolute headline values; others show signed deltas.
        assert "vmaf-median" in text and "vif-median" in text
        assert "dominated by h265+slow" in text
        assert "Survivors (Pareto front): h265-aq+slow, h265+slow — all will be encoded" in text


class TestFixedReuseFromPersisted:
    """Fixed reuse re-derives pruning/anchor/synthetic from persisted results."""

    def test_reuse_reselects_by_pruning_without_reencoding(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        strategy_names = ["h265-aq+slow", "h265+slow"]
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=strategy_names, optimize=True,
        )
        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        from pyqenc.phases.chunking import ChunkingPhaseResult
        chunking = next(
            ph for cls, ph in phase._phases.items() if cls.__name__ == "ChunkingPhase"
        )
        chunking.result = ChunkingPhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub",
            chunks=[Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks],
        )

        # Seed complete winners on disk (dry-run performs no wipe) and the
        # persisted results they correspond to.
        for strategy in phase._config.encoding.resolved_strategies:
            strategy_dir = work_dir / "encoded" / strategy.safe_name()
            strategy_dir.mkdir(parents=True, exist_ok=True)
            for chunk in chunks:
                mkv = strategy_dir / f"{chunk.safe_name()}.1920x1080.q18.0.mkv"
                mkv.write_bytes(b"x" * 64)
                write_yaml_atomic(
                    strategy_dir / f"{chunk.safe_name()}.1920x1080.yaml",
                    {"crf": "18.0", "targets_met": True, "metrics": {}},
                )
        _persist_optimization(
            work_dir, tmp_path / "source.mkv",
            [
                StrategyTestResult(
                    strategy="h265-aq+slow", total_size=1000,
                    metrics={"vmaf_median": 91.0},
                ),
                StrategyTestResult(
                    strategy="h265+slow", total_size=1400,
                    metrics={"vmaf_median": 93.0},
                ),
            ],
            tolerance_pct=5.0, selected=["h265-aq+slow", "h265+slow"],
            test_chunks=[c.safe_name() for c in chunks],
        )

        with caplog.at_level(logging.INFO, logger="pyqenc.phases.optimization"):
            result = phase.run(dry_run=True)

        assert result.outcome is PhaseOutcome.REUSED
        assert [s.display_name() for s in result.selected_strategies] == [
            "h265-aq+slow", "h265+slow",
        ]
        assert [(t.metric, t.statistic, t.value) for t in result.synthetic_targets] == [
            ("vmaf", "median", 91.0),
        ]
        assert "Fixed-quality comparison — ruler: h265-aq+slow" in caplog.text


class TestUncomparedFixedSkipsAnchor:
    """Single-strategy / optimize-off fixed runs skip anchor machinery (Req 8.7)."""

    def test_single_strategy_no_synthetic_set(self, tmp_path: Path) -> None:
        phase, work_dir, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=True,
        )
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED  # all-strategies skip path
        assert result.synthetic_targets == []
        persisted = OptimizationParams.load(work_dir / "optimization.yaml")
        assert persisted is not None
        assert persisted.anchor is None
        assert persisted.synthetic_targets == []
