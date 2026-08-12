"""End-to-end tests for complete pipeline execution."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyqenc.app_config import load_app_config
from pyqenc.models import CleanupLevel, CropParams, QualityTarget
from pyqenc.orchestrator import PipelineOrchestrator
from pyqenc.phase import _build_registry
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


def _qt(metric: str, statistic: str, value: float) -> QualityTarget:
    return QualityTarget(metric=metric, statistic=statistic, value=value)


def _make_orchestrator(
    tmp_path: Path,
    source_video: Path,
    *,
    strategies: list[str] | None = None,
    crop_params: CropParams | None = None,
    optimize: bool = False,
    quality_targets: list[str] | None = None,
) -> PipelineOrchestrator:
    """Build a PipelineOrchestrator for testing using the new AppConfig-based API."""
    config = load_app_config()
    config.encoding.optimize = optimize
    if strategies is not None:
        config.encoding.strategies = strategies
        config.encoding._resolved_strategies = None
    if quality_targets is not None:
        config.encoding.quality_targets = quality_targets
        config.encoding._resolved_targets = None
    if quality_targets is not None or strategies is not None:
        config.encoding._resolved_targets   = None
        config.encoding._resolved_strategies = None
        config.encoding.resolve(config.codecs, config.profiles)
    if crop_params is not None:
        config.encoding.crop_params = crop_params

    work_dir  = tmp_path / "work"
    collector = MagicMock()

    registry = _build_registry(
        config     = config,
        source     = source_video,
        work_dir   = work_dir,
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )

    return PipelineOrchestrator(
        registry,
        collector,
        no_metrics = True,
        work_dir   = work_dir,
        cleanup    = CleanupLevel.NONE,
    )


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestCompletePipeline:
    """End-to-end tests for complete pipeline execution."""

    def test_complete_pipeline_dry_run(self, tmp_path: Path) -> None:
        """Test complete pipeline in dry-run mode."""
        source_video = get_sample_video_path()
        orchestrator = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result = orchestrator.run(dry_run=True)
        assert result is not None

    def test_pipeline_with_manual_crop(self, tmp_path: Path) -> None:
        """Test pipeline with manual crop parameters."""
        source_video = get_sample_video_path()
        orchestrator = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
            crop_params     = CropParams(top=100, bottom=100, left=0, right=0),
        )
        result = orchestrator.run(dry_run=True)
        assert result is not None

    def test_pipeline_phase_limit(self, tmp_path: Path) -> None:
        """Test pipeline execution — dry-run stops at first incomplete phase."""
        source_video = get_sample_video_path()
        orchestrator = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result = orchestrator.run(dry_run=True)
        assert result is not None

    def test_pipeline_resumption_after_interruption(self, tmp_path: Path) -> None:
        """Test pipeline can resume after simulated interruption."""
        source_video = get_sample_video_path()

        orchestrator1 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result1 = orchestrator1.run(dry_run=True)
        assert result1 is not None

        orchestrator2 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result2 = orchestrator2.run(dry_run=True)
        assert result2 is not None

    def test_pipeline_configuration_change(self, tmp_path: Path) -> None:
        """Test pipeline handles configuration changes (new strategies)."""
        source_video = get_sample_video_path()

        orchestrator1 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result1 = orchestrator1.run(dry_run=True)
        assert result1 is not None

        orchestrator2 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default", "medium+h265-aq"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result2 = orchestrator2.run(dry_run=True)
        assert result2 is not None

    def test_pipeline_quality_target_change(self, tmp_path: Path) -> None:
        """Test pipeline handles quality target changes."""
        source_video = get_sample_video_path()

        orchestrator1 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:85.0"],
            optimize        = False,
        )
        result1 = orchestrator1.run(dry_run=True)
        assert result1 is not None

        orchestrator2 = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:95.0"],
            optimize        = False,
        )
        result2 = orchestrator2.run(dry_run=True)
        assert result2 is not None

    def test_pipeline_with_crop_detection(self, tmp_path: Path) -> None:
        """Test pipeline with automatic crop detection (crop_params=None)."""
        source_video = get_sample_video_path()
        orchestrator = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        result = orchestrator.run(dry_run=True)
        assert result is not None

    def test_pipeline_with_no_crop(self, tmp_path: Path) -> None:
        """Test pipeline with cropping disabled."""
        source_video = get_sample_video_path()
        orchestrator = _make_orchestrator(
            tmp_path,
            source_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
            crop_params     = CropParams(),
        )
        result = orchestrator.run(dry_run=True)
        assert result is not None


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestPipelineValidation:
    """Tests for pipeline input validation."""

    def test_invalid_source_video(self, tmp_path: Path) -> None:
        """Test pipeline with non-existent source video."""
        nonexistent_video = tmp_path / "nonexistent.mkv"
        orchestrator = _make_orchestrator(
            tmp_path,
            nonexistent_video,
            strategies      = ["fast+h265-default"],
            quality_targets = ["vmaf-min:90.0"],
            optimize        = False,
        )
        assert orchestrator is not None

    def test_invalid_strategy_raises_on_config_build(self, tmp_path: Path) -> None:
        """Test that an invalid strategy raises ValidationError at config load time."""
        from pydantic import ValidationError
        from pyqenc.app_config import AppConfig, load_app_config

        config_dict = load_app_config().model_dump()
        config_dict["encoding"]["strategies"] = ["invalid+nonexistent"]
        with pytest.raises((ValidationError, ValueError)):
            AppConfig.model_validate(config_dict)
