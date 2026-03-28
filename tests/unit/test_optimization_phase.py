"""Unit tests for OptimizationPhase tolerance re-application from cached results.

Covers requirement 7.7:
- When all strategy results are cached and only tolerance changed, re-select
  without re-encoding.
- Correct strategy selection at various tolerance levels.
- Tolerance of 0% selects exactly one strategy (the best).
- Tolerance of 100% selects all passing strategies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    PipelineConfig,
    QualityTarget,
    Strategy,
)
from pyqenc.phases.optimization import OptimizationPhase, OptimizationPhaseResult
from pyqenc.state import OptimizationParams, StrategyTestResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUALITY_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]

_S1 = Strategy.from_name("slow+h265-aq")
_S2 = Strategy.from_name("slow+h265")
_S3 = Strategy.from_name("veryslow+h265-aq")


def _make_config(
    tmp_path: Path,
    strategies: list[Strategy],
    optimize: bool = True,
    tolerance: float = 5.0,
    force: bool = False,
) -> PipelineConfig:
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * 1024)
    return PipelineConfig(
        source_video                 = src,
        work_dir                     = tmp_path / "work",
        quality_targets              = _QUALITY_TARGETS,
        strategies                   = strategies,
        optimize                     = optimize,
        all_strategies               = not optimize,
        cleanup                      = CleanupLevel.NONE,
        chunking_mode                = ChunkingMode.LOSSLESS,
        force                        = force,
        strategy_selection_tolerance = tolerance,
    )


def _make_phase(config: PipelineConfig) -> OptimizationPhase:
    return OptimizationPhase(config, phases=None)


def _persist_optimization(
    work_dir: Path,
    source: Path,
    strategy_results: list[StrategyTestResult],
    tolerance_pct: float,
    selected: list[Strategy],
    crop: CropParams | None = None,
) -> None:
    """Write optimization.yaml with given results."""
    work_dir.mkdir(parents=True, exist_ok=True)
    OptimizationParams(
        crop             = crop,
        test_chunks      = ["chunk-001", "chunk-002"],
        strategy_results = strategy_results,
        tolerance_pct    = tolerance_pct,
        selected         = selected,
    ).save(work_dir / "optimization.yaml")


def _make_results(sizes: list[int]) -> list[StrategyTestResult]:
    """Create StrategyTestResult list for S1, S2, S3 with given sizes."""
    strategies = [_S1, _S2, _S3]
    return [
        StrategyTestResult(strategy=s, total_size=sz, avg_crf=20.0)
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
        assert selected[0] == _S1

    def test_tolerance_includes_within_threshold(self) -> None:
        # S1=100, S2=104 (4% above best), S3=120 (20% above best)
        # With 5% tolerance: S1 and S2 selected
        results = _make_results([100, 104, 120])
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1 in selected
        assert _S2 in selected
        assert _S3 not in selected

    def test_tolerance_100_selects_all(self) -> None:
        results = _make_results([100, 150, 200])
        selected = OptimizationPhase._apply_tolerance(results, 100.0)
        assert len(selected) == 3

    def test_empty_results_returns_empty(self) -> None:
        assert OptimizationPhase._apply_tolerance([], 5.0) == []

    def test_zero_size_results_excluded(self) -> None:
        """Strategies with total_size=0 (failed) are excluded."""
        results = [
            StrategyTestResult(strategy=_S1, total_size=0,   avg_crf=0.0),
            StrategyTestResult(strategy=_S2, total_size=100, avg_crf=20.0),
        ]
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1 not in selected
        assert _S2 in selected

    def test_exact_threshold_boundary_included(self) -> None:
        """A strategy exactly at the threshold (100% * (1 + tol/100)) is included."""
        # S1=100, S2=105 — exactly at 5% threshold
        results = _make_results([100, 105, 200])
        selected = OptimizationPhase._apply_tolerance(results, 5.0)
        assert _S1 in selected
        assert _S2 in selected


# ---------------------------------------------------------------------------
# Tolerance re-application from cached results (Req 7.7)
# ---------------------------------------------------------------------------

class TestToleranceReapplication:
    """Tests for re-selecting strategies from cached results when tolerance changes."""

    def test_reapplication_returns_reused_outcome(self, tmp_path: Path) -> None:
        """When all results cached and tolerance changed, outcome is REUSED."""
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, tolerance=10.0)
        work_dir = config.work_dir

        # Persist results with old tolerance
        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir  = work_dir,
            source    = config.source_video,
            strategy_results = results,
            tolerance_pct    = 5.0,   # old tolerance
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True

    def test_reapplication_updates_selected_strategies(self, tmp_path: Path) -> None:
        """Re-application selects strategies based on new tolerance, not old."""
        strategies = [_S1, _S2, _S3]
        # New tolerance is 25% — should include S3 (20% above best)
        config = _make_config(tmp_path, strategies, tolerance=25.0)
        work_dir = config.work_dir

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = config.source_video,
            strategy_results = results,
            tolerance_pct    = 5.0,   # old tolerance — only S1, S2 selected
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert _S1 in result.selected_strategies
        assert _S2 in result.selected_strategies
        assert _S3 in result.selected_strategies

    def test_reapplication_persists_new_tolerance(self, tmp_path: Path) -> None:
        """After re-application, optimization.yaml is updated with the new tolerance."""
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, tolerance=10.0)
        work_dir = config.work_dir

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = config.source_video,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        phase.run(dry_run=False)

        # Read back the persisted file
        persisted = OptimizationParams.load(work_dir / "optimization.yaml")
        assert persisted is not None
        assert persisted.tolerance_pct == 10.0

    def test_no_reapplication_when_tolerance_unchanged(self, tmp_path: Path) -> None:
        """When tolerance is unchanged and all results cached, outcome is REUSED (fast path)."""
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, tolerance=5.0)
        work_dir = config.work_dir

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = config.source_video,
            strategy_results = results,
            tolerance_pct    = 5.0,   # same tolerance
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True
        # Selected strategies should match the cached selection
        assert _S1 in result.selected_strategies
        assert _S2 in result.selected_strategies

    def test_reapplication_zero_tolerance_selects_one(self, tmp_path: Path) -> None:
        """Changing tolerance to 0% selects exactly the best strategy."""
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, tolerance=0.0)
        work_dir = config.work_dir

        results = _make_results([100, 104, 120])
        _persist_optimization(
            work_dir         = work_dir,
            source           = config.source_video,
            strategy_results = results,
            tolerance_pct    = 5.0,
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert len(result.selected_strategies) == 1
        assert result.selected_strategies[0] == _S1

    def test_partial_results_not_reapplied(self, tmp_path: Path) -> None:
        """Re-application only triggers when ALL strategies have cached results."""
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, tolerance=10.0)
        work_dir = config.work_dir

        # Only 2 of 3 strategies have results
        partial_results = [
            StrategyTestResult(strategy=_S1, total_size=100, avg_crf=20.0),
            StrategyTestResult(strategy=_S2, total_size=104, avg_crf=20.5),
        ]
        _persist_optimization(
            work_dir         = work_dir,
            source           = config.source_video,
            strategy_results = partial_results,
            tolerance_pct    = 5.0,
            selected         = [_S1, _S2],
        )

        phase = _make_phase(config)
        # With no chunking phase wired, this will fail on dependency check —
        # that's expected; we just verify it does NOT return REUSED from re-application
        result = phase.run(dry_run=False)
        # Should not be a REUSED result from tolerance re-application
        # (it will fail on missing dependencies, which is correct)
        assert result.outcome != PhaseOutcome.REUSED or len(result.strategy_results) == 3


# ---------------------------------------------------------------------------
# All-strategies mode
# ---------------------------------------------------------------------------

class TestAllStrategiesMode:
    """Tests for all-strategies mode (optimize=False)."""

    def test_returns_all_configured_strategies(self, tmp_path: Path) -> None:
        strategies = [_S1, _S2, _S3]
        config = _make_config(tmp_path, strategies, optimize=False)
        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert result.is_complete is True
        assert set(result.selected_strategies) == set(strategies)

    def test_no_strategy_results_in_all_strategies_mode(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, [_S1, _S2], optimize=False)
        phase = _make_phase(config)
        result = phase.run(dry_run=False)

        assert result.strategy_results == []

    def test_scan_returns_all_strategies_silently(self, tmp_path: Path) -> None:
        strategies = [_S1, _S2]
        config = _make_config(tmp_path, strategies, optimize=False)
        phase = _make_phase(config)
        result = phase.scan()

        assert result.is_complete is True
        assert set(result.selected_strategies) == set(strategies)
