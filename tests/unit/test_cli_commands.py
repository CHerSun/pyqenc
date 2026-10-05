"""CLI command-surface tests (2026-10-05 cli-intent-commands, Req 1, 9.4, 9.5, 10).

The six-command set (removed subcommands absent), the declarative pipeline
table's parser shapes, and the handlers' api call contracts. The api
boundaries are patched; the config load is pinned to the bundled defaults so
the tests never read a developer's user config.
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


def _pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    for spec in cli._PIPELINE_SUBCOMMANDS:
        cli._create_pipeline_subcommand(subparsers, spec)
    return parser


class TestCommandSet:
    def test_intent_commands_present(self, tmp_path) -> None:
        for name in ("auto", "video", "audio"):
            args = _pipeline_parser().parse_args([name, str(tmp_path / "src.mkv")])
            assert args.subcommand == name

    def test_removed_commands_absent(self, tmp_path) -> None:
        """Req 1: chunk/encode/merge are removed with no compatibility path."""
        parser = _pipeline_parser()
        for name in ("chunk", "encode", "merge"):
            with pytest.raises(SystemExit):
                parser.parse_args([name, str(tmp_path / "src.mkv")])

    def test_video_carries_the_video_chain_groups(self, tmp_path) -> None:
        args = _pipeline_parser().parse_args(
            ["video", str(tmp_path / "src.mkv"), "--targets", "vmaf-med:97", "--crop", "0,0"],
        )
        assert args.targets == "vmaf-med:97"
        assert args.crop == "0,0"
        assert hasattr(args, "scene_threshold")  # chunking group present

    def test_audio_has_no_video_groups(self, tmp_path) -> None:
        args = _pipeline_parser().parse_args(["audio", str(tmp_path / "src.mkv")])
        for absent in ("crop", "targets", "strategies", "scene_threshold"):
            assert not hasattr(args, absent), absent


class TestPipelineHandlerContracts:
    """One template handler over the declarative table (Req 10).

    The spec table binds the real api functions at import time, so the
    injected seam is the parsed namespace's spec: its runner is swapped for
    a capturing fake via ``dataclasses.replace`` (the production table
    itself is never patched).
    """

    @staticmethod
    def _run(name: str, tmp_path, run_success: bool = True):
        from dataclasses import replace

        source = tmp_path / "src.mkv"
        source.write_bytes(b"\x00" * 16)
        args = _pipeline_parser().parse_args(
            [name, str(source), "-y", "--work-dir", str(tmp_path / "work")],
        )
        captured: dict = {}

        def fake_runner(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(
                success=run_success,
                error=None if run_success else "boom",
                output_files=[tmp_path / "out.mkv"] if run_success and name == "video" else [],
            )

        args.spec = replace(args.spec, runner=fake_runner)
        with patch.object(cli, "load_app_config", return_value=_DEFAULT_CONFIG):
            rc = cli._cmd_pipeline(args)
        return rc, captured

    def test_video_calls_its_runner_with_plan(self, tmp_path) -> None:
        rc, captured = self._run("video", tmp_path)
        assert rc == 0
        assert "plan" in captured and "crop_params" in captured

    def test_audio_calls_its_runner_without_plan(self, tmp_path) -> None:
        rc, captured = self._run("audio", tmp_path)
        assert rc == 0
        assert "plan" not in captured and "crop_params" not in captured

    def test_failure_returns_nonzero(self, tmp_path) -> None:
        rc, _ = self._run("video", tmp_path, run_success=False)
        assert rc == 1


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
