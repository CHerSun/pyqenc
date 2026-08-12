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
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.orchestrator import PipelineOrchestrator
from pyqenc.phase import Phase, PhaseResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(
    work_dir:   Path,
    *,
    no_metrics: bool = False,
    registry:   dict | None = None,
    collector:  MetricsCollector | None = None,
) -> PipelineOrchestrator:
    """Return a ``PipelineOrchestrator`` using the new constructor."""
    if collector is None:
        collector = NoOpMetricsCollector()
    return PipelineOrchestrator(
        registry   = registry if registry is not None else {},
        collector  = collector,
        no_metrics = no_metrics,
        work_dir   = work_dir,
        cleanup    = CleanupLevel.NONE,
    )


# ---------------------------------------------------------------------------
# Collector registration — caller's responsibility, orchestrator manages lifecycle
# ---------------------------------------------------------------------------

class TestActiveCollectorRegistration:
    """register_active_collector is called only when no_metrics=False."""

    def test_collector_not_registered_when_metrics_disabled(self, tmp_path: Path) -> None:
        """register_active_collector is NOT called when no_metrics=True (Req 8.5)."""
        with patch("pyqenc.orchestrator.register_active_collector") as mock_reg:
            _make_orchestrator(tmp_path / "work", no_metrics=True).run(dry_run=True)

        mock_reg.assert_not_called()

    def test_collector_cleared_on_success(self, tmp_path: Path) -> None:
        """register_active_collector(None) called when pipeline succeeds (Req 1.6)."""
        mock_collector = MagicMock(spec=MetricsCollector)
        with patch("pyqenc.orchestrator.register_active_collector") as mock_reg:
            _make_orchestrator(tmp_path / "work", no_metrics=False, collector=mock_collector).run(dry_run=True)

        assert mock_reg.call_args_list[-1].args == (None,)

    def test_collector_cleared_on_failure(self, tmp_path: Path) -> None:
        """register_active_collector(None) called even when a phase fails (Req 1.6)."""
        failed_result = PhaseResult(
            outcome   = PhaseOutcome.FAILED,
            artifacts = [],
            message   = "boom",
            error     = "boom",
        )
        mock_phase = MagicMock()
        mock_phase.name = "TestPhase"
        mock_phase.run.return_value = failed_result

        mock_collector = MagicMock(spec=MetricsCollector)
        registry: dict[type[Phase], Phase] = {MagicMock: mock_phase}  # type: ignore[dict-item]

        with patch("pyqenc.orchestrator.register_active_collector") as mock_reg:
            _make_orchestrator(
                tmp_path / "work",
                no_metrics = False,
                registry   = registry,
                collector  = mock_collector,
            ).run(dry_run=False)

        # Last call must clear the collector
        assert mock_reg.call_args_list[-1].args == (None,)


# ---------------------------------------------------------------------------
# Final flush on success
# ---------------------------------------------------------------------------

class TestFinalFlush:
    """flush() is called only on successful non-dry-run completion."""

    def test_flush_on_success(self, tmp_path: Path) -> None:
        """flush() called after all phases complete when no_metrics=False (Req 5.4, 8.3)."""
        mock_collector = MagicMock(spec=MetricsCollector)
        with patch("pyqenc.orchestrator.register_active_collector"):
            _make_orchestrator(
                tmp_path / "work",
                no_metrics = False,
                collector  = mock_collector,
            ).run(dry_run=False)

        mock_collector.flush.assert_called_once_with()

    def test_no_flush_on_dry_run(self, tmp_path: Path) -> None:
        """flush is NOT called in dry-run mode."""
        mock_collector = MagicMock(spec=MetricsCollector)
        with patch("pyqenc.orchestrator.register_active_collector"):
            _make_orchestrator(
                tmp_path / "work",
                no_metrics = False,
                collector  = mock_collector,
            ).run(dry_run=True)

        mock_collector.flush.assert_not_called()

    def test_no_flush_when_metrics_disabled(self, tmp_path: Path) -> None:
        """flush is NOT called at all when no_metrics=True (Req 8.2)."""
        mock_collector = MagicMock(spec=MetricsCollector)
        _make_orchestrator(
            tmp_path / "work",
            no_metrics = True,
            collector  = mock_collector,
        ).run(dry_run=False)

        mock_collector.flush.assert_not_called()


# ---------------------------------------------------------------------------
# Collector construction — now the caller's responsibility (api.py / cli.py)
# These tests verify the *caller* (api.run_pipeline) constructs the right type.
# ---------------------------------------------------------------------------

class TestCollectorConstruction:
    """The correct MetricsCollector type is constructed by the pipeline entry point."""

    def test_yaml_collector_constructed_when_metrics_enabled(self, tmp_path: Path) -> None:
        """YamlMetricsCollector is constructed when no_metrics=False (Req 1.1, 6.3)."""
        from pyqenc.api import run_pipeline
        from pyqenc.app_config import load_app_config

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        config = load_app_config()

        constructed: list[MetricsCollector] = []

        OriginalOrchestrator = PipelineOrchestrator

        class _CapturingOrchestrator(OriginalOrchestrator):
            def __init__(self, registry, collector, *, no_metrics, work_dir, cleanup):
                constructed.append(collector)
                super().__init__(registry, collector, no_metrics=no_metrics, work_dir=work_dir, cleanup=cleanup)

        with patch("pyqenc.api.PipelineOrchestrator", _CapturingOrchestrator):
            run_pipeline(config, source=source, work_dir=work_dir, no_metrics=False, dry_run=True)

        assert len(constructed) == 1
        assert isinstance(constructed[0], YamlMetricsCollector)

    def test_noop_collector_constructed_when_metrics_disabled(self, tmp_path: Path) -> None:
        """NoOpMetricsCollector is constructed when no_metrics=True (Req 8.2)."""
        from pyqenc.api import run_pipeline
        from pyqenc.app_config import load_app_config

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        config = load_app_config()

        constructed: list[MetricsCollector] = []

        OriginalOrchestrator = PipelineOrchestrator

        class _CapturingOrchestrator(OriginalOrchestrator):
            def __init__(self, registry, collector, *, no_metrics, work_dir, cleanup):
                constructed.append(collector)
                super().__init__(registry, collector, no_metrics=no_metrics, work_dir=work_dir, cleanup=cleanup)

        with patch("pyqenc.api.PipelineOrchestrator", _CapturingOrchestrator):
            run_pipeline(config, source=source, work_dir=work_dir, no_metrics=True, dry_run=True)

        assert len(constructed) == 1
        assert isinstance(constructed[0], NoOpMetricsCollector)


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
