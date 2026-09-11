"""End-to-end tests for complete pipeline execution via the public API."""

from pathlib import Path

import pytest

from pyqenc import api
from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.models import CropParams
from pyqenc.runner import RunResult
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


def _make_config(
    *,
    strategies: list[str],
    targets:    list[str],
) -> AppConfig:
    """Build an ``AppConfig`` with the given strategies/targets through the public path.

    Strategies and targets are injected into the dumped default config and
    re-validated, so ``EncodingConfig.resolve()`` runs fresh (it early-returns
    on an already-resolved instance). Optimisation is disabled to keep the
    encoding phase deterministic for e2e. No private ``_resolved_*`` caches are
    touched — resolution happens through the supported ``model_validate`` path.
    """
    config_dict = load_app_config(default_only=True).model_dump()
    config_dict["encoding"]["strategies"] = strategies
    config_dict["encoding"]["targets"]    = targets
    config_dict["encoding"]["optimize"]   = False
    return AppConfig.model_validate(config_dict)


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
@pytest.mark.slow
class TestCompletePipeline:
    """End-to-end tests for complete pipeline execution."""

    def test_dry_run_reports_work_remaining_on_fresh_work_dir(self, tmp_path: Path) -> None:
        """A dry-run over an unprocessed source must report that work remains.

        Prevents the bug where a dry-run on a fresh work_dir silently reports
        success/complete (empty ``phases_needing_work``), which would let the
        pipeline claim there is nothing to do for a source it never touched.
        """
        config = _make_config(strategies=["h265+fast"], targets=["vmaf-min:90.0"])
        result = api.run_pipeline(
            config,
            get_sample_video_path(),
            tmp_path / "work",
            no_metrics = True,
            dry_run    = True,
        )
        assert isinstance(result, RunResult)
        assert result.success is False
        assert result.phases_needing_work

    def test_manual_crop_accepted_end_to_end(self, tmp_path: Path) -> None:
        """A manual crop override must be accepted through the whole public API.

        Prevents the bug where passing ``crop_params`` breaks the pipeline
        wiring (raising instead of threading the override through to the phases);
        the dry-run must still complete and report remaining work.
        """
        config = _make_config(strategies=["h265+fast"], targets=["vmaf-min:90.0"])
        result = api.run_pipeline(
            config,
            get_sample_video_path(),
            tmp_path / "work",
            no_metrics  = True,
            dry_run     = True,
            crop_params = CropParams(top=100, bottom=100, left=0, right=0),
        )
        assert isinstance(result, RunResult)
        assert result.success is False
        assert result.phases_needing_work


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestPipelineValidation:
    """Tests for pipeline input validation."""

    def test_invalid_source_video(self, tmp_path: Path) -> None:
        """A non-existent source must raise ``FileNotFoundError`` per the API contract.

        Prevents the bug where a missing source is silently accepted and the
        pipeline proceeds against a path that does not exist.
        """
        config = _make_config(strategies=["h265+fast"], targets=["vmaf-min:90.0"])
        with pytest.raises(FileNotFoundError):
            api.run_pipeline(
                config,
                tmp_path / "nonexistent.mkv",
                tmp_path / "work",
                no_metrics = True,
                dry_run    = True,
            )

    def test_invalid_strategy_raises_on_config_build(self, tmp_path: Path) -> None:
        """Test that an invalid strategy raises ValidationError at config load time."""
        from pydantic import ValidationError

        config_dict = load_app_config(default_only=True).model_dump()
        config_dict["encoding"]["strategies"] = ["invalid+nonexistent"]
        with pytest.raises((ValidationError, ValueError)):
            AppConfig.model_validate(config_dict)
