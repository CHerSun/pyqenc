"""Behavior tests for the standalone measure API wiring (cli -> api -> phase).

Bug guarded: the ``measure`` subcommand crashed with ``TypeError`` before any
work — parameter names drifted apart across the cli/api/run_measure boundaries
(the ``sampling`` parameter threading), uncovered by other tests.  The
``measure`` opt-out threading rides the same seam.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.api import measure_quality
from pyqenc.app_config import load_app_config


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
                concurrency      = 1,
                distorted_videos = [],
            )

        assert result == "measure-result"
        assert captured["sampling"] == 5
        assert captured["concurrency"] == 1
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
                concurrency         = 1,
                distorted_videos    = [],
                screenshot_interval = "30s",
            )

        assert captured["screenshot_interval"] == 30.0

    def test_measure_opt_out_reaches_run_measure(self, tmp_path: Path) -> None:
        """measure=False requests a screenshots-only run; the default measures.

        Bug guarded: the opt-out flag accepted by the CLI but dropped at the
        api/phase boundary would silently measure anyway — the very cost the
        flag exists to skip.
        """
        source = tmp_path / "source.mkv"
        source.write_bytes(b"stub")
        captured: dict[str, object] = {}

        async def _fake_run_measure(**kwargs: object) -> str:
            captured.update(kwargs)
            return "measure-result"

        with mock.patch("pyqenc.api.run_measure", _fake_run_measure):
            measure_quality(
                source_video     = source,
                work_dir         = tmp_path,
                sampling         = 1,
                concurrency      = 1,
                distorted_videos = [tmp_path / "out.mkv"],
                measure          = False,
            )
            assert captured["measure"] is False

            captured.clear()
            measure_quality(
                source_video     = source,
                work_dir         = tmp_path,
                sampling         = 1,
                concurrency      = 1,
                distorted_videos = [tmp_path / "out.mkv"],
            )
        assert captured["measure"] is True

    @settings(max_examples=25, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        measure     = st.booleans(),
        sampling    = st.integers(min_value=1, max_value=50),
        concurrency = st.integers(min_value=1, max_value=16),
    )
    def test_properties_wiring_passes_caller_values_verbatim(
        self, tmp_path: Path, measure: bool, sampling: int, concurrency: int,
    ) -> None:
        """Every caller-set decision reaches run_measure unchanged."""
        source = tmp_path / "source.mkv"
        source.write_bytes(b"stub")
        captured: dict[str, object] = {}

        async def _fake_run_measure(**kwargs: object) -> str:
            captured.update(kwargs)
            return "measure-result"

        with mock.patch("pyqenc.api.run_measure", _fake_run_measure):
            measure_quality(
                source_video     = source,
                work_dir         = tmp_path,
                sampling         = sampling,
                concurrency      = concurrency,
                distorted_videos = [],
                measure          = measure,
            )

        assert captured["measure"] is measure
        assert captured["sampling"] == sampling
        assert captured["concurrency"] == concurrency

    def test_missing_source_raises_before_dispatch(self, tmp_path: Path) -> None:
        """A missing source video fails up front with the precise exception."""
        calls: list[dict[str, object]] = []

        async def _fake_run_measure(**kwargs: object) -> str:
            calls.append(kwargs)
            return "measure-result"

        with (
            mock.patch("pyqenc.api.run_measure", _fake_run_measure),
            pytest.raises(FileNotFoundError),
        ):
            measure_quality(
                source_video     = tmp_path / "absent.mkv",
                work_dir         = tmp_path,
                sampling         = 1,
                concurrency      = 1,
                distorted_videos = [],
                measure          = False,
            )
        assert calls == []


class TestMeasureCliFlag:
    def test_no_measure_flag_reaches_the_api(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`measure <source> <outputs> -N` asks the API for a screenshots-only run.

        Bug guarded: a flag wired to the parser but not passed through
        `_cmd_measure` fails as AttributeError (exit 1) or silently measures.
        """
        source = tmp_path / "source.mkv"
        source.write_bytes(b"stub")
        captured: dict[str, object] = {}

        def _fake_measure_quality(**kwargs: object) -> str:
            captured.update(kwargs)
            return "measure-result"

        from pyqenc.cli import main

        monkeypatch.setattr(
            "pyqenc.cli.load_app_config", lambda: load_app_config(default_only=True),
        )
        monkeypatch.setattr("pyqenc.cli.measure_quality", _fake_measure_quality)

        monkeypatch.setattr(
            sys, "argv",
            ["pyqenc", "measure", str(source), str(tmp_path / "out.mkv"),
             "--work-dir", str(tmp_path), "-N"],
        )
        exit_code = main()
        assert exit_code == 0
        assert captured["measure"] is False
        assert captured["source_video"] == source

        captured.clear()
        monkeypatch.setattr(
            sys, "argv",
            ["pyqenc", "measure", str(source), str(tmp_path / "out.mkv"),
             "--work-dir", str(tmp_path)],
        )
        exit_code = main()
        assert exit_code == 0
        assert captured["measure"] is True
