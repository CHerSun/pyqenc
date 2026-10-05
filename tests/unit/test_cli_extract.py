"""CLI `extract` command tests (2026-10-05 cli-intent-commands, Req 9.4/9.5).

Parser shape (no cleanup/crop/plan arguments) and the handler's materialize
call. The api boundary is patched; the config load is pinned to the bundled
defaults so the test never reads a developer's user config.
"""

# CHerSun 2026

import argparse
from unittest.mock import MagicMock, patch

import pytest

from pyqenc import cli
from pyqenc.app_config import load_app_config

_DEFAULT_CONFIG = load_app_config(default_only=True)


def _extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    cli._create_extract_subcommand(subparsers)
    return parser


class TestExtractParserShape:
    def test_basic_defaults(self, tmp_path) -> None:
        args = _extract_parser().parse_args(["extract", str(tmp_path / "src.mkv")])
        assert args.execute is False
        assert args.force is False
        assert args.no_metrics is False
        assert args.include is None and args.exclude is None

    def test_no_cleanup_crop_or_plan_arguments(self, tmp_path) -> None:
        """Req 9.5: materialized files are the product — no `--cleanup`;
        no probe is reached — no crop; no plan — no quality args."""
        args = _extract_parser().parse_args(["extract", str(tmp_path / "src.mkv")])
        for absent in ("cleanup", "crop", "targets", "strategies", "quality", "scene_threshold"):
            assert not hasattr(args, absent), absent

    def test_cleanup_flag_rejected(self, tmp_path) -> None:
        with pytest.raises(SystemExit):
            _extract_parser().parse_args(["extract", str(tmp_path / "src.mkv"), "--cleanup"])


class TestExtractHandler:
    def _run(self, tmp_path, *, run_success: bool = True):
        source = tmp_path / "src.mkv"
        source.write_bytes(b"\x00" * 16)
        args = _extract_parser().parse_args(
            ["extract", str(source), "-y", "--exclude", "video-", "--work-dir", str(tmp_path / "work")],
        )
        captured: dict = {}

        def fake_extract_streams(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(success=run_success, error=None if run_success else "boom")

        with (
            patch.object(cli, "load_app_config", return_value=_DEFAULT_CONFIG),
            patch.object(cli, "extract_streams", side_effect=fake_extract_streams),
        ):
            rc = cli._cmd_extract(args)
        return rc, captured

    def test_calls_materialize_run_without_cleanup_or_plan(self, tmp_path) -> None:
        rc, captured = self._run(tmp_path)

        assert rc == 0
        assert captured["materialize"] is True
        assert captured["dry_run"] is False
        assert captured["config"].extraction.exclude == "video-"
        assert "cleanup" not in captured
        assert "crop_params" not in captured
        assert "plan" not in captured

    def test_failure_returns_nonzero(self, tmp_path) -> None:
        rc, _ = self._run(tmp_path, run_success=False)
        assert rc == 1
