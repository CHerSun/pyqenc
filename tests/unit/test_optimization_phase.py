"""Unit tests for OptimizationPhase tolerance re-application from cached results.

Covers requirement 7.7:
- When all strategy results are cached and only tolerance changed, re-select
  without re-encoding.
- Correct strategy selection at various tolerance levels.
- Tolerance of 0% selects exactly one strategy (the best).
- Tolerance of 100% selects all passing strategies.
"""

from pathlib import Path
from unittest.mock import MagicMock

from pyqenc.app_config import load_app_config
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase
from pyqenc.state import (
    ArtifactState,
    OptimizationParams,
    ProbeState,
    StrategyTestResult,
)

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
        cleanup    = CleanupLevel.NONE,
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

    job, work_dir = _make_job_phase(tmp_path, strategies, optimize=optimize, tolerance=tolerance, force=force)
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

