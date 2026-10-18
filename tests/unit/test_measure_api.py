"""Behavior tests for the standalone measure API wiring (cli -> api -> phase).

Bug guarded: the ``measure`` subcommand crashed with ``TypeError`` before any
work — parameter names drifted apart across the cli/api/run_measure boundaries
(the ``sampling`` parameter threading), uncovered by other tests.
"""

from pathlib import Path
from unittest import mock

from pyqenc.api import measure_quality


class TestMeasureQualityWiring:
    def test_reaches_run_measure_with_resolved_sampling(self, tmp_path: Path) -> None:
        """measure_quality forwards the caller's sampling into run_measure."""
        source = tmp_path / "source.mkv"
        source.write_bytes(b"stub")
        captured: dict[str, object] = {}

        async def _fake_run_measure(**kwargs: object) -> str:
            captured.update(kwargs)
            return "measure-result"

        with mock.patch("pyqenc.api.run_measure", _fake_run_measure):
            result = measure_quality(
                source_video     = source,
                work_dir         = tmp_path,
                sampling         = 5,
                distorted_videos = [],
            )

        assert result == "measure-result"
        assert captured["sampling"] == 5
        assert captured["source_video"] == source
        assert captured["work_dir"] == tmp_path
        assert captured["distorted_videos"] == []

    def test_interval_mode_parses_before_dispatch(self, tmp_path: Path) -> None:
        """A screenshot interval string is parsed to seconds before run_measure."""
        source = tmp_path / "source.mkv"
        source.write_bytes(b"stub")
        captured: dict[str, object] = {}

        async def _fake_run_measure(**kwargs: object) -> str:
            captured.update(kwargs)
            return "measure-result"

        with mock.patch("pyqenc.api.run_measure", _fake_run_measure):
            measure_quality(
                source_video        = source,
                work_dir            = tmp_path,
                sampling            = 1,
                distorted_videos    = [],
                screenshot_interval = "30s",
            )

        assert captured["screenshot_interval"] == 30.0
