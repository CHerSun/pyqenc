"""Unit tests for OptimizationPhase selection and recovery currency.

Covers:
- Live selection: the fast exit (all winner pairs COMPLETE) computes the
  selection from the CURRENT tolerance over the persisted rows — tolerance
  changes cost nothing and are never persisted.
- Plan-scoped derivation (TODO §99): rows for strategies outside the run's
  plan never reach selection, display, or the rewritten sidecar.
- The per-pair ledger (presence-based to-test, both modes).
- Fixed-mode entry (cleanup guard, winner wipe, banner) and the fixed
  compared-run machinery (pruning, anchor, synthetic set).
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
    EncodingPlan,
    Fingerprint,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import (
    OptimizationPhase,
    OptimizationPhaseResult,
    StrategySummaryRow,
    current_optimization_sidecar,
    load_optimization_sidecar,
)
from pyqenc.state import ArtifactState, ProbeFacet
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUALITY_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]

_APP_CONFIG = load_app_config(default_only=True)

# Resolve a few specific strategies for use in tests.
_ALL_STRATEGIES = _APP_CONFIG.resolve_encoding().strategies
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
) -> tuple[JobPhase, Path, EncodingPlan]:
    """Create and run a JobPhase so that result is populated for downstream phases."""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * 1024)
    work_dir = tmp_path / "work"

    config = _APP_CONFIG.model_copy(deep=True)
    config.encoding.optimize = optimize
    config.encoding.optimize_tolerance = tolerance
    plan = config.resolve_encoding(
        targets    = ["vmaf-min:93.0"],
        strategies = [f"{s.profile}+{s.preset}" for s in strategies],
    )

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
    return job, work_dir, plan


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

    job, work_dir, plan = _make_job_phase(
        tmp_path, strategies, optimize=optimize, tolerance=tolerance,
        force=force, cleanup=cleanup,
    )
    config = job._config  # already resolved AppConfig
    phases: PhaseRegistry = {_JP: job}

    probe = _PP(config, phases, collector=MagicMock(), crop_params=None, plan=plan)
    probe.result = ProbePhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "probe complete",
        plan      = plan,
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
    phase: OptimizationPhase,
    summary:    list[StrategySummaryRow],
    *,
    test_chunks: list[str] | None = None,
) -> None:
    """Write an optimization.yaml whose keys MATCH the phase's live inputs.

    Built through the production composer from the phase's own dependency
    results (job source identity, probe facet, chunk set, plan) so no
    invalidation fires — the persisted ``summary`` is what the test varies.
    """
    from pyqenc.models import id_set_fingerprint

    job    = phase._phases[JobPhase]
    probe  = next(
        ph for cls, ph in phase._phases.items() if cls.__name__ == "ProbePhase"
    )
    chunking = next(
        ph for cls, ph in phase._phases.items() if cls.__name__ == "ChunkingPhase"
    )
    assert probe.result is not None and job.result is not None
    assert chunking.result is not None
    assert job.result is not None
    work_dir = job.result.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    current_optimization_sidecar(
        plan        = probe.result.plan,
        source      = job.result.source_fingerprint,
        chunks      = id_set_fingerprint(
            a.payload.safe_name() for a in chunking.result.chunks
        ),
        probe       = ProbeFacet.from_probe(probe.result),
        sampling    = phase._config.measurement.sampling,
        test_chunks = test_chunks if test_chunks is not None else ["chunk-001", "chunk-002"],
        summary     = summary,
    ).save(work_dir / "optimization.yaml")


def _seed_winner(
    work_dir: Path,
    strategy: Strategy,
    chunk,
    *,
    size_bytes: int = 64,
) -> None:
    """Fabricate a COMPLETE winner pair (file + result sidecar) on disk."""
    from pyqenc.constants import ENCODED_OUTPUT_DIR
    from pyqenc.phases.encoding import EncodingResultSidecar
    from pyqenc.stream_model import EncodedChunk as _EC

    strategy_dir = work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name()
    strategy_dir.mkdir(parents=True, exist_ok=True)
    winner = strategy_dir / _EC.format_winner_file_name(chunk.safe_name())
    winner.write_bytes(b"x" * size_bytes)
    write_yaml_atomic(
        strategy_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
        EncodingResultSidecar(
            resolution="1920x1080", crf=Decimal(20),
            metrics={}, targets_met=True,
        ).model_dump(exclude_none=True),
    )


def _wire_chunks(phase: OptimizationPhase, chunks: list) -> None:
    """Wire a real chunk set into the phase's ChunkingPhase dependency."""
    from pyqenc.phases.chunking import ChunkingPhaseResult
    chunking = next(
        ph for cls, ph in phase._phases.items() if cls.__name__ == "ChunkingPhase"
    )
    chunking.result = ChunkingPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "chunking complete",
        chunks    = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks],
    )


def _make_results(sizes: list[int]) -> list[StrategySummaryRow]:
    """Create StrategySummaryRow list for S1, S2, S3 with given sizes."""
    strategies = [_S1, _S2, _S3]
    return [
        StrategySummaryRow(strategy=s.display_name(), total_size=sz)
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
            StrategySummaryRow(strategy=_S1.display_name(), total_size=0),
            StrategySummaryRow(strategy=_S2.display_name(), total_size=100),
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
# Fast exit: live selection over persisted rows (the only sanctioned reader)
# ---------------------------------------------------------------------------

class TestFastExitLiveSelection:
    """All winner pairs COMPLETE → REUSED with the selection computed live.

    Bug guarded (supersedes the old tolerance-reapply class): a tolerance
    change used to be persisted-decision currency — a cheap re-select
    execution plus a sidecar rewrite. Selection is now a pure derivation
    from the current tolerance over the persisted rows: a change costs
    nothing (no phase work, no write).
    """

    def _setup(self, tmp_path: Path, tolerance: float):
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=tolerance)
        chunks = [
            _make_chunk(float(i * 10), float((i + 1) * 10), tmp_path)
            for i in range(2)
        ]
        _wire_chunks(phase, chunks)
        for s in strategies:
            for chunk in chunks:
                _seed_winner(work_dir, s, chunk)
        _persist_optimization(
            phase, _make_results([100, 104, 120]),
            test_chunks=[c.safe_name() for c in chunks],
        )
        return phase, work_dir

    def test_tolerance_applied_live(self, tmp_path: Path) -> None:
        """Tolerance 25% selects all three — recomputed live, no re-encoding."""
        phase, _ = self._setup(tmp_path, tolerance=25.0)
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED
        assert [s.display_name() for s in result.selected_strategies] == [
            s.display_name() for s in [_S1, _S2, _S3]
        ]

    def test_zero_tolerance_selects_best_only(self, tmp_path: Path) -> None:
        phase, _ = self._setup(tmp_path, tolerance=0.0)
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED
        assert result.selected_strategies == [_S1]

    def test_fast_exit_writes_nothing(self, tmp_path: Path) -> None:
        """The fast exit is read-only — the sidecar stays byte-identical."""
        phase, work_dir = self._setup(tmp_path, tolerance=10.0)
        sidecar = work_dir / "optimization.yaml"
        before = sidecar.read_bytes()
        phase.run(dry_run=False)
        assert sidecar.read_bytes() == before

    def test_missing_winner_keeps_work_pending(self, tmp_path: Path) -> None:
        """Presence rules: a strategy without complete pairs is to-test."""
        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=5.0)
        chunks = [_make_chunk(0.0, 10.0, tmp_path)]
        _wire_chunks(phase, chunks)
        for s in (strategies[0], strategies[1]):
            _seed_winner(work_dir, s, chunks[0])
        _persist_optimization(
            phase, _make_results([100, 104, 120]),
            test_chunks=[c.safe_name() for c in chunks],
        )
        result = phase.run(dry_run=True)
        assert result.outcome is PhaseOutcome.PENDING


# ---------------------------------------------------------------------------
# §99: selection derives from live, plan-scoped state — never stale rows
# ---------------------------------------------------------------------------

class TestSelectionFromLiveState:
    """Rows for strategies outside the run's plan never reach selection.

    Bug guarded (TODO §99, live 2026-10-05): the execution path merged
    persisted rows unscoped, ranked a stale smallest-size strategy first,
    selected it, and the silent name-drop then left Encoding with an empty
    selection. Rows outside the plan are now unread on every path.
    """

    def test_stale_rows_never_selected_on_fast_exit(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Plan-scoped fast exit: a tiny stale row cannot win selection and
        is not even displayed."""
        plan_strategies = [_S1, _S3]
        phase, work_dir = _make_phase(tmp_path, plan_strategies, tolerance=5.0)
        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        _wire_chunks(phase, chunks)
        for s in plan_strategies:
            for chunk in chunks:
                _seed_winner(work_dir, s, chunk)
        # Stale table from a previous run's strategy set: the old bug picked
        # the tiny stale row ("smallest size") and crashed downstream.
        _persist_optimization(
            phase,
            [
                StrategySummaryRow(strategy=_S2.display_name(), total_size=100),
                StrategySummaryRow(strategy="h264+veryslow", total_size=110),
                StrategySummaryRow(strategy=_S1.display_name(), total_size=200),
                StrategySummaryRow(strategy=_S3.display_name(), total_size=300),
            ],
            test_chunks=[c.safe_name() for c in chunks],
        )

        with caplog.at_level(logging.INFO, logger="pyqenc.phases.optimization"):
            result = phase.run(dry_run=False)

        assert result.outcome is PhaseOutcome.REUSED
        assert result.selected_strategies == [_S1]
        assert _S2.display_name() not in caplog.text
        assert "h264+veryslow" not in caplog.text

    def test_processing_rewrites_table_without_stale_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bug guarded (laundering): the mid-run save used to persist the
        unfiltered cache back — stale rows survived into the new sidecar.
        The final table now derives from the live ledger, plan-scoped."""
        from unittest.mock import MagicMock as _MM

        from pyqenc.phases.encoding import EncodingResult

        strategies = [_S1, _S2, _S3]
        phase, work_dir = _make_phase(tmp_path, strategies, tolerance=5.0)
        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        _wire_chunks(phase, chunks)
        # Winners already on disk for S1/S2; S3 is pending this run.
        for s in (strategies[0], strategies[1]):
            for chunk in chunks:
                _seed_winner(work_dir, s, chunk, size_bytes=100 if s is _S1 else 105)
        _persist_optimization(
            phase,
            [
                StrategySummaryRow(strategy="h264+veryslow", total_size=90),
                StrategySummaryRow(strategy=_S1.display_name(), total_size=200),
                StrategySummaryRow(strategy=_S2.display_name(), total_size=210),
            ],
            test_chunks=[c.safe_name() for c in chunks],
        )

        async def _fake_parallel(**_kwargs: object) -> EncodingResult:
            for chunk in chunks:
                _seed_winner(work_dir, _S3, chunk, size_bytes=150)
            return EncodingResult()

        monkeypatch.setattr(
            "pyqenc.phases.optimization._make_encoder", lambda **_kw: _MM(),
        )
        monkeypatch.setattr(
            "pyqenc.phases.encoding._encode_chunks_parallel", _fake_parallel,
        )

        result = phase.run(dry_run=False)

        assert result.outcome is PhaseOutcome.COMPLETED
        plan_names = {s.display_name() for s in strategies}
        assert {s.display_name() for s in result.selected_strategies} <= plan_names
        persisted = load_optimization_sidecar(work_dir / "optimization.yaml")
        assert persisted is not None
        assert {r.strategy for r in persisted.summary} == plan_names
        # Sizes derive from the live winner files (2 chunks x seeded bytes).
        by_name = {r.strategy: r for r in persisted.summary}
        assert by_name[_S1.display_name()].total_size == 200
        assert by_name[_S3.display_name()].total_size == 300

    def test_missing_table_rows_derive_only(self, tmp_path: Path) -> None:
        """All pairs complete but the table misses a plan strategy → sizes
        derive live from the ledger, a fresh table is saved, nothing encodes
        (COMPLETED without any encoder mocks is itself the proof)."""
        plan_strategies = [_S1, _S2]
        phase, work_dir = _make_phase(tmp_path, plan_strategies, tolerance=5.0)
        chunks = [_make_chunk(0.0, 10.0, tmp_path), _make_chunk(10.0, 20.0, tmp_path)]
        _wire_chunks(phase, chunks)
        for s in plan_strategies:
            for chunk in chunks:
                _seed_winner(work_dir, s, chunk, size_bytes=100 if s is _S1 else 110)
        # Table covers only S1 — S2's row is missing.
        _persist_optimization(
            phase,
            [StrategySummaryRow(strategy=_S1.display_name(), total_size=1)],
            test_chunks=[c.safe_name() for c in chunks],
        )

        result = phase.run(dry_run=False)

        assert result.outcome is PhaseOutcome.COMPLETED
        assert result.selected_strategies == [_S1]  # 200 vs 220 at 5% tolerance
        persisted = load_optimization_sidecar(work_dir / "optimization.yaml")
        assert persisted is not None
        by_name = {r.strategy: r for r in persisted.summary}
        assert by_name[_S1.display_name()].total_size == 200
        assert by_name[_S2.display_name()].total_size == 220

    def test_missing_sidecar_wipes_winners(self, tmp_path: Path) -> None:
        """No sidecar → winner currency is unknown → conservative wipe +
        replay from attempts (the pairs become pending again)."""
        from pyqenc.constants import ENCODED_OUTPUT_DIR

        plan_strategies = [_S1, _S2]
        phase, work_dir = _make_phase(tmp_path, plan_strategies, tolerance=5.0)
        chunks = [_make_chunk(0.0, 10.0, tmp_path)]
        _wire_chunks(phase, chunks)
        for s in plan_strategies:
            _seed_winner(work_dir, s, chunks[0])
        assert (work_dir / ENCODED_OUTPUT_DIR).exists()

        result = phase.run(dry_run=True)

        assert result.outcome is PhaseOutcome.PENDING
        assert not (work_dir / ENCODED_OUTPUT_DIR / _S1.safe_name()).exists()


# ---------------------------------------------------------------------------
# Selection belt — programmatically-impossible states, caught at assembly
# ---------------------------------------------------------------------------

class TestSelectionBelt:
    """Contract asserts in ``_make_result``: selection ⊆ plan, non-empty on success."""

    def test_selection_outside_plan_raises(self, tmp_path: Path) -> None:
        phase, _ = _make_phase(tmp_path, [_S1, _S2])
        phase._selected_names = ["not-a-strategy"]
        with pytest.raises(AssertionError, match="escaped the plan"):
            phase._make_result(PhaseOutcome.COMPLETED, [], "message")

    def test_empty_selection_raises_on_success(self, tmp_path: Path) -> None:
        phase, _ = _make_phase(tmp_path, [_S1, _S2])
        phase._selected_names = []
        with pytest.raises(AssertionError, match="at least one strategy"):
            phase._make_result(PhaseOutcome.COMPLETED, [], "message")

    def test_failed_result_allows_empty_selection(self, tmp_path: Path) -> None:
        phase, _ = _make_phase(tmp_path, [_S1, _S2])
        result = phase._make_result(PhaseOutcome.FAILED, [], "error")
        assert result.selected_strategies == []


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
                file=File(fingerprint=_STUB_SOURCE_FP, path=tmp_path / "source.mkv", file_size_bytes=64),
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
        phase, work_dir = _make_phase(tmp_path, [_S1, _S2, _S3][:n_strategies])
        _wire_chunks(phase, chunks)
        # Persist the full chunk set as the test selection so recovery is
        # deterministic (the fresh random pick stays covered by the e2e run).
        _persist_optimization(
            phase, [],
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
        winner = strategy_dir / _EC.format_winner_file_name(chunk.safe_name())
        winner.write_bytes(b"x" * 16)
        write_yaml_atomic(
            strategy_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
            EncodingResultSidecar(
                resolution="1920x1080", crf=Decimal(20),
                metrics={}, targets_met=True,
            ).model_dump(exclude_none=True),
        )

        phase2, _, _ = self._phase_with_chunks(tmp_path, 1, chunks)
        recovery2 = phase2._recover()
        assert [a.state for a in recovery2.artifacts] == [ArtifactState.COMPLETE]
        # The pair is complete, but the persisted table carries no rows —
        # sizes/selection must be re-derived, so work IS pending.
        assert recovery2.pending is True



# ---------------------------------------------------------------------------
# Fixed-mode entry: cleanup guard, winner wipe, banner (Req 5-7)
# ---------------------------------------------------------------------------

from decimal import Decimal

from pyqenc.constants import ENCODED_OUTPUT_DIR

_STUB_SOURCE_FP = Fingerprint(token="0" * 32, size=64)
"""Stub source identity — phases read the job File's fingerprint."""



def _make_fixed_phase(
    tmp_path: Path,
    *,
    strategy_names: list[str],
    cleanup: CleanupLevel = CleanupLevel.NONE,
    optimize: bool = True,
) -> tuple[OptimizationPhase, Path, EncodingPlan]:
    """An OptimizationPhase harness whose run plan pins the knob via -q semantics.

    The override is applied exactly as ``_build_config`` applies it: passed to
    ``resolve_encoding`` together with the strategy patterns —
    ``plan.fixed_quality`` derives True for every matched strategy.
    """
    from pyqenc.phases.chunking import ChunkingPhase as _CP
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.job import JobPhase as _JP
    from pyqenc.phases.probe import ProbePhase as _PP
    from pyqenc.phases.probe import ProbePhaseResult



    config = _APP_CONFIG.model_copy(deep=True)
    config.encoding.optimize = optimize
    plan = config.resolve_encoding(
        strategies = strategy_names,
        quality    = (Decimal("18"), Decimal("18")),
    )
    assert plan.fixed_quality, "harness must derive a fixed run"

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

    probe = _PP(config, phases, collector=MagicMock(), crop_params=None, plan=plan)
    probe.result = ProbePhaseResult(
        outcome=PhaseOutcome.COMPLETED, message="stub", plan=plan, stream=None,
    )
    phases[_PP] = probe

    chunking = _CP(config, phases, collector=MagicMock())
    chunking.result = ChunkingPhaseResult(outcome=PhaseOutcome.COMPLETED, message="stub", chunks=[])
    phases[_CP] = chunking

    phase = OptimizationPhase(config, phases=phases, collector=MagicMock())
    return phase, work_dir, plan


def _seed_encoded_winner(work_dir: Path, strategy: Strategy) -> Path:
    """Create an ``encoded/<strategy>/`` winner (file + sidecar) to observe wipes."""
    strategy_dir = work_dir / ENCODED_OUTPUT_DIR / strategy.safe_name()
    strategy_dir.mkdir(parents=True, exist_ok=True)
    winner = strategy_dir / "chunk-001.1920x1080.q18.mkv"
    winner.write_bytes(b"x" * 32)
    (strategy_dir / "chunk-001.1920x1080.yaml").write_text("crf: 18\n", encoding="utf-8")
    return strategy_dir


class TestRunBoundaryGuard:
    """Fixed + cleanup >= INTERMEDIATE is a run-configuration contradiction,
    validated at the plan boundary (api) — never a phase skip (Req 55)."""

    @staticmethod
    def _plan(fixed: bool) -> EncodingPlan:
        if fixed:
            return _APP_CONFIG.resolve_encoding(
                strategies=["h265-aq+slow"],
                quality=(Decimal(18), Decimal(18)),
            )
        return _APP_CONFIG.resolve_encoding(
            targets=["vmaf-min:93.0"], strategies=["h265-aq+slow", "h264+slow"],
        )

    @pytest.mark.parametrize("level", [CleanupLevel.INTERMEDIATE, CleanupLevel.ALL])
    def test_guard_rejects_fixed_plus_cleanup(self, level: CleanupLevel) -> None:
        from pyqenc.api import _validate_run_configuration

        with pytest.raises(ValueError, match="re-derivation substrate"):
            _validate_run_configuration(self._plan(fixed=True), level)

    def test_guard_passes_fixed_without_cleanup(self) -> None:
        from pyqenc.api import _validate_run_configuration

        _validate_run_configuration(self._plan(fixed=True), CleanupLevel.NONE)

    def test_guard_never_applies_to_searched_runs(self) -> None:
        from pyqenc.api import _validate_run_configuration

        _validate_run_configuration(self._plan(fixed=False), CleanupLevel.ALL)

    def test_guard_ignores_audio_only_runs(self) -> None:
        """A planless (audio-only) closure has no fixed run to contradict."""
        from pyqenc.api import _validate_run_configuration

        _validate_run_configuration(None, CleanupLevel.ALL)


class TestWinnerWipeSemantics:
    """Winner wipes are CONDITION-driven now: the §99 unknown-currency wipe
    (sidecar missing + winners present) is mode-independent; a CURRENT
    sidecar wipes nothing (the pending gate alone decides — O-1/O-2)."""

    def test_missing_sidecar_wipes_on_all_strategies_path(self, tmp_path: Path) -> None:
        # Single fixed strategy + optimize on → all-strategies skip path.
        phase, work_dir, plan = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=True,
        )
        seeded = _seed_encoded_winner(work_dir, plan.strategies[0])
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED  # all-strategies skip result
        assert not seeded.exists(), "no sidecar vouches for the winner — wiped"

    def test_missing_sidecar_wipes_on_searched_runs_too(self, tmp_path: Path) -> None:
        """The conservative §99 wipe is mode-independent — searched winners
        without a sidecar are equally unknown currency."""
        phase, work_dir = _make_phase(tmp_path, [_S1], optimize=False, force=False)
        seeded = _seed_encoded_winner(work_dir, _S1)
        phase.run(dry_run=False)
        assert not seeded.exists()

    def test_current_sidecar_wipes_nothing_on_fixed_rerun(self, tmp_path: Path) -> None:
        """O-2: a fixed rerun with the SAME pinned map performs no
        invalidation — winners stay (the pending gate decides reuse)."""
        phase, work_dir, plan = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=True,
        )
        seeded = _seed_encoded_winner(work_dir, plan.strategies[0])
        # First run persists the current keys (§99 wipes the seeded winner —
        # so seed again after the sidecar exists).
        phase.run(dry_run=False)
        seeded = _seed_encoded_winner(work_dir, plan.strategies[0])

        phase2, _, _ = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=True,
        )
        phase2.run(dry_run=False)

        assert seeded.exists(), "same pinned map + current sidecar → no wipe"

    def test_wipe_skipped_on_dry_run(self, tmp_path: Path) -> None:
        phase, work_dir, plan = _make_fixed_phase(
            tmp_path, strategy_names=["h265-aq+slow"], optimize=False,
        )
        seeded = _seed_encoded_winner(work_dir, plan.strategies[0])
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
        plan = config.resolve_encoding()
        assert plan.fixed_quality

        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1024)
        job = JobPhase(
            config, {}, source=src, work_dir=tmp_path / "work", force=False,
            cleanup=CleanupLevel.NONE, no_metrics=True, collector=MagicMock(),
        )
        job.run(dry_run=False)
        phases: PhaseRegistry = {JobPhase: job}
        probe = _PP(config, phases, collector=MagicMock(), crop_params=None, plan=plan)
        probe.result = ProbePhaseResult(
            outcome=PhaseOutcome.COMPLETED, message="stub", plan=plan, stream=None,
        )
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

def _result(name: str, size: int, metrics: dict[str, float]) -> StrategySummaryRow:
    """A StrategySummaryRow with metrics (the fixed-mode pruning input)."""
    return StrategySummaryRow(strategy=name, total_size=size, metrics=metrics)


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
        """Dominance needs advantage on every compared statistic — a single
        deficit blocks the prune even when every other compared stat wins."""
        results = [
            _result("a+slow", 1000, {"vmaf_median": 93.0, "vif_median": 83.9}),
            _result("b+slow", 1400, {"vmaf_median": 91.0, "vif_median": 84.0}),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["a+slow", "b+slow"]

    def test_stability_stats_do_not_sway_dominance(self) -> None:
        """std/max-style statistics are outside the comparison set — a
        strategy that wins only those stays dominated."""
        results = [
            _result("a+slow", 1000, {
                "vmaf_p10": 90.0, "vmaf_median": 93.0, "vmaf_std": 1.0, "vmaf_max": 95.0,
            }),
            _result("b+slow", 1400, {
                "vmaf_p10": 88.0, "vmaf_median": 91.0, "vmaf_std": 2.0, "vmaf_max": 99.0,
            }),
        ]
        assert OptimizationPhase._dominance_survivors(results) == ["a+slow"]


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

    def test_stability_only_survivor_cannot_anchor(self) -> None:
        """A survivor measured only on stats outside the comparison set has
        an empty ruler — it must not anchor over a compared survivor."""
        results = [
            _result("a+slow", 1000, {"vmaf_std": 1.5, "vmaf_max": 98.0}),
            _result("b+slow", 1400, {"vmaf_p10": 88.0, "vmaf_median": 91.0}),
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

    def test_comparison_stats_only_sorted(self) -> None:
        """The ruler carries only the fixed-mode comparison statistics (p10,
        median) per metric — stability/shape stats stay out."""
        anchor = _result(
            "av1+slow", 1000,
            {
                "vif_p10": 81.0, "vif_median": 84.0, "vif_max": 92.0,
                "vmaf_p10": 88.5, "vmaf_median": 91.2, "vmaf_std": 1.0,
            },
        )
        targets = OptimizationPhase._synthetic_targets_from(anchor)
        assert [(t.metric, t.statistic, t.value) for t in targets] == [
            ("vif", "p10", 81.0),
            ("vif", "median", 84.0),
            ("vmaf", "p10", 88.5),
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
        encoded_chunks: dict[str, list[EncodedChunk]] = {}
        for i, chunk in enumerate(chunks):
            name = EncodedChunk.format_winner_file_name(chunk.safe_name())
            strategy_dir = tmp_path / "encoded" / strategy.safe_name()
            strategy_dir.mkdir(parents=True, exist_ok=True)
            (strategy_dir / name).write_bytes(b"x" * (100 * (i + 1)))
            # Result sidecar naming: the winner stem swap.
            write_yaml_atomic(
                strategy_dir / EncodedChunk.format_winner_sidecar_name(chunk.safe_name()),
                {"metrics": {"vmaf_median": 91.0 + i, "vif_median": 84.0 - i}},
            )
            encoded_chunks.setdefault(strategy.display_name(), []).append(
                build_encoded_chunk(
                    chunk=chunk, strategy=strategy,
                    path=strategy_dir / name, resolution="1920x1080",
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
        from pyqenc.stream_model import EncodedChunk as _EC

        chunk = _make_chunk(0.0, 10.0, tmp_path)
        strategy = _S1
        strategy_dir = tmp_path / "encoded" / strategy.safe_name()
        strategy_dir.mkdir(parents=True, exist_ok=True)
        mkv = strategy_dir / _EC.format_winner_file_name(chunk.safe_name())
        mkv.write_bytes(b"x" * 100)
        # No sidecar next to the winner.
        encoded_chunks = {
            strategy.display_name(): [
                build_encoded_chunk(
                    chunk=chunk, strategy=strategy,
                    path=mkv, resolution="1920x1080", frame_count=24,
                ),
            ],
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
        from pyqenc.stream_model import EncodedChunk as _EC

        strategy_names = ["h265-aq", "h265", "h265-anime"]
        phase, work_dir, plan = _make_fixed_phase(
            tmp_path, strategy_names=[f"{n}+slow" for n in strategy_names],
            optimize=True,
        )
        phase._config.encoding.optimize_tolerance = tolerance

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
            phase, [],
            test_chunks=[c.safe_name() for c in chunks],
        )

        strategies = plan.strategies

        def _seed_and_compose() -> EncodingResult:
            result = EncodingResult()
            for chunk_idx, chunk in enumerate(chunks):
                for strategy in strategies:
                    display = strategy.display_name()
                    per_chunk = sizes[display][chunk_idx]
                    strategy_dir = work_dir / "encoded" / strategy.safe_name()
                    strategy_dir.mkdir(parents=True, exist_ok=True)
                    mkv = strategy_dir / _EC.format_winner_file_name(chunk.safe_name())
                    mkv.write_bytes(b"x" * per_chunk)
                    write_yaml_atomic(
                        strategy_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
                        {"crf": "18.0", "targets_met": True, "metrics": metrics[display]},
                    )
                    result.encoded_chunks.setdefault(display, []).append(
                        build_encoded_chunk(
                            chunk=chunk, strategy=strategy,
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
        # Compared stats (p10, median) carry the dominance structure:
        # h265 beats h265-anime on every compared stat at smaller size
        # (dominates it); h265-aq is smallest with the weakest metrics
        # (incomparable with both). std/max are noise outside the set.
        "h265-aq+slow": {
            "vmaf_p10": 87.0, "vmaf_median": 91.0, "vmaf_std": 1.1, "vmaf_max": 96.0,
            "vif_p10": 81.0, "vif_median": 84.0, "vif_max": 91.0,
        },
        "h265+slow": {
            "vmaf_p10": 89.5, "vmaf_median": 93.1, "vmaf_std": 1.0, "vmaf_max": 95.0,
            "vif_p10": 87.5, "vif_median": 90.3, "vif_max": 92.0,
        },
        "h265-anime+slow": {
            "vmaf_p10": 88.5, "vmaf_median": 92.0, "vmaf_std": 2.0, "vmaf_max": 99.0,
            "vif_p10": 83.0, "vif_median": 85.0, "vif_max": 94.0,
        },
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
        # min-aggregated COMPARED stats only (sorted) — std/max stay out.
        assert result.anchor == "h265-aq+slow"
        assert [(t.metric, t.statistic, t.value) for t in result.synthetic_targets] == [
            ("vif", "p10", 81.0),
            ("vif", "median", 84.0),
            ("vmaf", "p10", 87.0),
            ("vmaf", "median", 91.0),
        ]

        # The sidecar persists facts only — selection/anchor are live
        # derivations and the synthetic ruler re-derives from
        # strategy_results on read (no synthetic_targets field).
        persisted = load_optimization_sidecar(work_dir / "optimization.yaml")
        assert persisted is not None
        assert not hasattr(persisted, "synthetic_targets")
        # The per-strategy records keep the FULL aggregated metrics for reuse
        # (data retention — re-selecting the comparison set never re-measures).
        by_name = {r.strategy: r for r in persisted.summary}
        assert by_name["h265-aq+slow"].metrics["vmaf_median"] == 91.0
        assert by_name["h265-aq+slow"].metrics["vmaf_std"] == 1.1
        # Re-derivation from the persisted facts reproduces the ruler verbatim.
        anchor_result = next(
            r for r in persisted.summary if r.strategy == result.anchor
        )
        assert [
            (t.metric, t.statistic, t.value)
            for t in OptimizationPhase._synthetic_targets_from(anchor_result)
        ] == [
            ("vif", "p10", 81.0),
            ("vif", "median", 84.0),
            ("vmaf", "p10", 87.0),
            ("vmaf", "median", 91.0),
        ]

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
        # The stat convention is stated once; metric columns are one per metric.
        assert "anchor p10..median, others Δp10/Δmedian vs anchor" in text
        # Anchor row: bare size (baseline) + p10..median ranges.
        assert "87.0..91.0" in text and "81.0..84.0" in text
        # Non-anchor rows: size with folded ratio + paired deltas.
        assert "(1.40×)" in text
        assert "+2.5/+2.1" in text  # h265 vmaf Δp10/Δmedian vs anchor
        assert "dominated by h265+slow" in text
        assert "Survivors (Pareto front): h265-aq+slow, h265+slow — all will be encoded" in text


class TestFixedReuseFromPersisted:
    """Fixed reuse re-derives pruning/anchor/synthetic from persisted results."""

    def test_reuse_reselects_by_pruning_without_reencoding(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        strategy_names = ["h265-aq+slow", "h265+slow"]
        phase, work_dir, plan = _make_fixed_phase(
            tmp_path, strategy_names=strategy_names, optimize=True,
        )
        from pyqenc.stream_model import EncodedChunk as _EC

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
        for strategy in plan.strategies:
            strategy_dir = work_dir / "encoded" / strategy.safe_name()
            strategy_dir.mkdir(parents=True, exist_ok=True)
            for chunk in chunks:
                mkv = strategy_dir / _EC.format_winner_file_name(chunk.safe_name())
                mkv.write_bytes(b"x" * 64)
                write_yaml_atomic(
                    strategy_dir / _EC.format_winner_sidecar_name(chunk.safe_name()),
                    {"crf": "18.0", "targets_met": True, "metrics": {}},
                )
        _persist_optimization(
            phase,
            [
                StrategySummaryRow(
                    strategy="h265-aq+slow", total_size=1000,
                    metrics={"vmaf_median": 91.0},
                ),
                StrategySummaryRow(
                    strategy="h265+slow", total_size=1400,
                    metrics={"vmaf_median": 93.0},
                ),
            ],
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
        assert result.anchor is None
        persisted = load_optimization_sidecar(work_dir / "optimization.yaml")
        assert persisted is not None
        # Selection and anchor are live derivations — not sidecar fields.
        assert "selected" not in persisted.model_dump()
        assert "anchor" not in persisted.model_dump()
