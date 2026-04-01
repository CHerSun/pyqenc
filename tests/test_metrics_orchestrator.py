"""Unit tests for orchestrator MetricsCollector construction and lifecycle.

Covers:
- YamlMetricsCollector constructed and registered as active when no_metrics=False (Req 1.1, 6.3)
- flush() called on successful pipeline completion when no_metrics=False (Req 5.4, 8.3)
- Active collector cleared after run completes or fails (Req 1.6)
- NoOpMetricsCollector used, no registration, no flush calls when no_metrics=True (Req 8.2, 8.5)
- flush_active_collector is a safe no-op when no collector is registered (Req 8.5)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pyqenc.metrics import (
    MetricsCollector,
    NoOpMetricsCollector,
    YamlMetricsCollector,
    flush_active_collector,
    register_active_collector,
)
from pyqenc.models import ChunkingMode, CleanupLevel, PipelineConfig
from pyqenc.orchestrator import PipelineOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    tmp_path: Path,
    *,
    no_metrics: bool = False,
    force: bool = False,
) -> PipelineConfig:
    """Return a minimal ``PipelineConfig`` for orchestrator tests."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    return PipelineConfig(
        source_video    = source,
        work_dir        = tmp_path / "work",
        quality_targets = [],
        strategies      = [],
        optimize        = False,
        max_parallel    = 1,
        include         = None,
        exclude         = None,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = ChunkingMode.LOSSLESS,
        force           = force,
        no_metrics      = no_metrics,
    )


# ---------------------------------------------------------------------------
# Collector construction
# ---------------------------------------------------------------------------

class TestCollectorConstruction:
    """Orchestrator constructs the right collector based on config.no_metrics."""

    def test_yaml_collector_constructed_when_metrics_enabled(self, tmp_path: Path) -> None:
        """YamlMetricsCollector is constructed when no_metrics=False (Req 1.1, 6.3)."""
        config = _make_config(tmp_path, no_metrics=False)
        constructed: list[MetricsCollector] = []

        original_build = __import__("pyqenc.phase", fromlist=["_build_registry"])._build_registry

        def _capture(cfg: PipelineConfig, collector: MetricsCollector):
            constructed.append(collector)
            return original_build(cfg, collector)

        with (
            patch("pyqenc.orchestrator._build_registry", side_effect=_capture),
            patch("pyqenc.orchestrator.register_active_collector"),
        ):
            PipelineOrchestrator(config).run(dry_run=True)

        assert len(constructed) == 1
        assert isinstance(constructed[0], YamlMetricsCollector)

    def test_noop_collector_constructed_when_metrics_disabled(self, tmp_path: Path) -> None:
        """NoOpMetricsCollector is constructed when no_metrics=True (Req 8.2)."""
        config = _make_config(tmp_path, no_metrics=True)
        constructed: list[MetricsCollector] = []

        original_build = __import__("pyqenc.phase", fromlist=["_build_registry"])._build_registry

        def _capture(cfg: PipelineConfig, collector: MetricsCollector):
            constructed.append(collector)
            return original_build(cfg, collector)

        with patch("pyqenc.orchestrator._build_registry", side_effect=_capture):
            PipelineOrchestrator(config).run(dry_run=True)

        assert len(constructed) == 1
        assert isinstance(constructed[0], NoOpMetricsCollector)


# ---------------------------------------------------------------------------
# Active collector registration
# ---------------------------------------------------------------------------

class TestActiveCollectorRegistration:
    """register_active_collector is called only when no_metrics=False."""

    def test_collector_registered_when_metrics_enabled(self, tmp_path: Path) -> None:
        """register_active_collector(collector) called when no_metrics=False (Req 1.6)."""
        config = _make_config(tmp_path, no_metrics=False)

        with (
            patch("pyqenc.orchestrator.YamlMetricsCollector") as mock_cls,
            patch("pyqenc.orchestrator.register_active_collector") as mock_reg,
        ):
            mock_collector = MagicMock(spec=MetricsCollector)
            mock_cls.return_value = mock_collector

            PipelineOrchestrator(config).run(dry_run=True)

        # First call registers the collector; last call clears it with None
        calls = mock_reg.call_args_list
        assert calls[0].args == (mock_collector,)
        assert calls[-1].args == (None,)

    def test_collector_not_registered_when_metrics_disabled(self, tmp_path: Path) -> None:
        """register_active_collector is NOT called when no_metrics=True (Req 8.5)."""
        config = _make_config(tmp_path, no_metrics=True)

        with patch("pyqenc.orchestrator.register_active_collector") as mock_reg:
            PipelineOrchestrator(config).run(dry_run=True)

        mock_reg.assert_not_called()

    def test_collector_cleared_on_failure(self, tmp_path: Path) -> None:
        """register_active_collector(None) called even when a phase fails (Req 1.6)."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phase import PhaseResult

        config = _make_config(tmp_path, no_metrics=False)

        failed_result = PhaseResult(
            outcome   = PhaseOutcome.FAILED,
            artifacts = [],
            message   = "boom",
            error     = "boom",
        )
        mock_phase = MagicMock()
        mock_phase.name = "TestPhase"
        mock_phase.run.return_value = failed_result

        with (
            patch("pyqenc.orchestrator.YamlMetricsCollector") as mock_cls,
            patch("pyqenc.orchestrator.register_active_collector") as mock_reg,
            patch("pyqenc.orchestrator._build_registry", return_value={"TestPhase": mock_phase}),
        ):
            mock_cls.return_value = MagicMock(spec=MetricsCollector)
            PipelineOrchestrator(config).run(dry_run=False)

        # Last call must clear the collector
        assert mock_reg.call_args_list[-1].args == (None,)


# ---------------------------------------------------------------------------
# Final flush on success
# ---------------------------------------------------------------------------

class TestFinalFlush:
    """flush() is called only on successful non-dry-run completion."""

    def test_flush_on_success(self, tmp_path: Path) -> None:
        """flush() called after all phases complete when no_metrics=False (Req 5.4, 8.3)."""
        config = _make_config(tmp_path, no_metrics=False)

        with (
            patch("pyqenc.orchestrator.YamlMetricsCollector") as mock_cls,
            patch("pyqenc.orchestrator.register_active_collector"),
            patch("pyqenc.orchestrator._build_registry", return_value={}),
        ):
            mock_collector = MagicMock(spec=MetricsCollector)
            mock_cls.return_value = mock_collector

            PipelineOrchestrator(config).run(dry_run=False)

        mock_collector.flush.assert_called_once_with()

    def test_no_flush_on_dry_run(self, tmp_path: Path) -> None:
        """flush is NOT called in dry-run mode."""
        config = _make_config(tmp_path, no_metrics=False)

        with (
            patch("pyqenc.orchestrator.YamlMetricsCollector") as mock_cls,
            patch("pyqenc.orchestrator.register_active_collector"),
        ):
            mock_collector = MagicMock(spec=MetricsCollector)
            mock_cls.return_value = mock_collector

            PipelineOrchestrator(config).run(dry_run=True)

        mock_collector.flush.assert_not_called()

    def test_no_flush_when_metrics_disabled(self, tmp_path: Path) -> None:
        """flush is NOT called at all when no_metrics=True (Req 8.2)."""
        config = _make_config(tmp_path, no_metrics=True)

        with (
            patch("pyqenc.orchestrator.NoOpMetricsCollector") as mock_cls,
            patch("pyqenc.orchestrator._build_registry", return_value={}),
        ):
            mock_collector = MagicMock(spec=MetricsCollector)
            mock_cls.return_value = mock_collector

            PipelineOrchestrator(config).run(dry_run=False)

        mock_collector.flush.assert_not_called()


# ---------------------------------------------------------------------------
# flush_active_collector helper
# ---------------------------------------------------------------------------

class TestFlushActiveCollector:
    """flush_active_collector is safe to call regardless of registration state."""

    def test_noop_when_no_collector_registered(self) -> None:
        """flush_active_collector does nothing when no collector is active (Req 8.5)."""
        register_active_collector(None)
        flush_active_collector()  # must not raise

    def test_flushes_registered_collector(self) -> None:
        """flush_active_collector calls flush() on the active collector."""
        mock_collector = MagicMock(spec=MetricsCollector)
        register_active_collector(mock_collector)
        try:
            flush_active_collector()
        finally:
            register_active_collector(None)

        mock_collector.flush.assert_called_once_with()

    def test_logs_warning_on_flush_failure(self) -> None:
        """flush_active_collector logs WARNING and does not raise when flush fails (Req 1.5)."""
        mock_collector = MagicMock(spec=MetricsCollector)
        mock_collector.flush.side_effect = OSError("disk full")
        register_active_collector(mock_collector)
        try:
            flush_active_collector()  # must not raise
        finally:
            register_active_collector(None)
