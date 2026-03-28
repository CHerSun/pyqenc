"""End-to-end tests for complete pipeline execution."""

from pathlib import Path

import pytest

from pyqenc.config import ConfigManager
from pyqenc.models import CropParams, PipelineConfig, QualityTarget
from pyqenc.orchestrator import PipelineOrchestrator
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


def _qt(metric: str, statistic: str, value: float) -> QualityTarget:
    return QualityTarget(metric=metric, statistic=statistic, value=value)


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestCompletePipeline:
    """End-to-end tests for complete pipeline execution."""

    def test_complete_pipeline_dry_run(self, tmp_path):
        """Test complete pipeline in dry-run mode."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )

        orchestrator = PipelineOrchestrator(config)
        result = orchestrator.run(dry_run=True, max_phases=None)
        assert result is not None

    def test_pipeline_with_manual_crop(self, tmp_path):
        """Test pipeline with manual crop parameters."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
            crop_params=CropParams(top=100, bottom=100, left=0, right=0),
        )

        orchestrator = PipelineOrchestrator(config)
        result = orchestrator.run(dry_run=True, max_phases=1)
        assert result is not None

    def test_pipeline_phase_limit(self, tmp_path):
        """Test pipeline execution with phase limit."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )

        orchestrator = PipelineOrchestrator(config)
        result = orchestrator.run(dry_run=True, max_phases=2)
        assert result is not None

    def test_pipeline_resumption_after_interruption(self, tmp_path):
        """Test pipeline can resume after simulated interruption."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )

        orchestrator1 = PipelineOrchestrator(config)
        result1 = orchestrator1.run(dry_run=True, max_phases=1)
        assert result1 is not None

        orchestrator2 = PipelineOrchestrator(config)
        result2 = orchestrator2.run(dry_run=True, max_phases=None)
        assert result2 is not None

    def test_pipeline_configuration_change(self, tmp_path):
        """Test pipeline handles configuration changes (new strategies)."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config1 = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )
        orchestrator1 = PipelineOrchestrator(config1)
        result1 = orchestrator1.run(dry_run=True, max_phases=1)
        assert result1 is not None

        config2 = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default", "medium+h265-aq"],
            optimize=False,
            all_strategies=True,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )
        orchestrator2 = PipelineOrchestrator(config2)
        result2 = orchestrator2.run(dry_run=True, max_phases=None)
        assert result2 is not None

    def test_pipeline_quality_target_change(self, tmp_path):
        """Test pipeline handles quality target changes."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config1 = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 85.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )
        orchestrator1 = PipelineOrchestrator(config1)
        result1 = orchestrator1.run(dry_run=True, max_phases=1)
        assert result1 is not None

        config2 = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 95.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )
        orchestrator2 = PipelineOrchestrator(config2)
        result2 = orchestrator2.run(dry_run=True, max_phases=None)
        assert result2 is not None

    def test_pipeline_with_crop_detection(self, tmp_path):
        """Test pipeline with automatic crop detection."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
            crop_params=None,
        )

        orchestrator = PipelineOrchestrator(config)
        result = orchestrator.run(dry_run=True, max_phases=1)
        assert result is not None

    def test_pipeline_with_no_crop(self, tmp_path):
        """Test pipeline with cropping disabled."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
            crop_params=CropParams(),
        )

        orchestrator = PipelineOrchestrator(config)
        result = orchestrator.run(dry_run=True, max_phases=1)
        assert result is not None


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestPipelineValidation:
    """Tests for pipeline input validation."""

    def test_invalid_source_video(self, tmp_path):
        """Test pipeline with non-existent source video."""
        nonexistent_video = tmp_path / "nonexistent.mkv"
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=nonexistent_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["fast+h265-default"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )

        orchestrator = PipelineOrchestrator(config)
        assert orchestrator is not None

    def test_invalid_strategy(self, tmp_path):
        """Test pipeline with invalid strategy."""
        source_video = get_sample_video_path()
        work_dir = tmp_path / "work"

        config = PipelineConfig(
            source_video=source_video,
            work_dir=work_dir,
            quality_targets=[_qt("vmaf", "min", 90.0)],
            strategies=["invalid+nonexistent"],
            optimize=False,
            all_strategies=False,
            max_parallel=1,
            metrics_sampling=10,
            log_level="info",
        )

        config_manager = ConfigManager()
        with pytest.raises(ValueError):
            config_manager.parse_strategy("invalid+nonexistent")
