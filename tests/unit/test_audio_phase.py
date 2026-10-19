"""Behaviour tests for the rewritten :class:`AudioPhase` (Task 8, Req 9.x/10.x).

Each test names the concrete bug it guards against and asserts observable
behaviour — which output files exist on disk and which artifacts the phase
result reports — never internal loop state. A **fake** ``execute_chain``
replaces the real chain executor so no ffmpeg runs; it writes a placeholder byte
to the deterministic ``<stream safe name> chain=<name>.<ext>`` output so completion (read
from disk) behaves exactly as production.

Bugs guarded:

- A changed chain param not invalidating + reproducing that chain's outputs
  across all tracks (stale outputs surviving a config edit).
- A removed chain's outputs not being cleaned up.
- Substring deletion nuking an unrelated chain (``chain=night`` deleting
  ``chain=nightlong``).
- An unchanged chain being needlessly reprocessed instead of reused.
- Selection not being recomputed each run (processing the wrong tracks).
- A passthrough chain crashing the whole phase instead of failing one output.
- The sidecar not committed before producing (resume re-invalidation loop).
"""
# CHerSun 2026

import asyncio
import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.app_config import AudioConfig, ChainSpec, FilterInstance, SelectEntry
from pyqenc.audio.chain import ResolvedChain, chain_output_path
from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import (
    AUDIO_OUTPUT_DIR,
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
)
from pyqenc.models import PhaseOutcome, SizedFingerprint
from pyqenc.phases.audio import AudioPhase, AudioSidecar
from pyqenc.stream_model import AudioStream, AudioStreamInfo, ProbedFile
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.yaml_utils import load_model

_STUB_SOURCE_FP = SizedFingerprint(token="0" * 32, size=64)
"""Stub source identity — phases read the job File's fingerprint."""


_AUDIO_YAML = "audio.yaml"


def _chain_out(tmp_path: Path, track: AudioStream, chain: str, ext: str = "flac") -> Path:
    """Expected chain-output path for *track* — the phase's deterministic name."""
    return chain_output_path(track, chain, ext, LongPath(tmp_path) / AUDIO_OUTPUT_DIR)


# ---------------------------------------------------------------------------
# Config + track builders
# ---------------------------------------------------------------------------

def _fi(type_id: str, **params) -> FilterInstance:
    """Build a validated config-side ``FilterInstance``."""
    return FilterInstance(type=type_id, **params)


def _audio_config(chains: list[ChainSpec], select: list[SelectEntry] | None = None) -> AudioConfig:
    """Build an ``AudioConfig`` with a small palette and the given chains/select."""
    palette = {
        "peaknorm":  _fi("peaknorm", target_dbfs=-1.0),
        "peaknorm2": _fi("peaknorm", target_dbfs=-2.0),   # a *different* peaknorm
        "dynaudnorm": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.95, maxgain=10.0, targetrms=0.0),
        "aac":       _fi("encode", codec="aac", bitrate_per_channel="96k", extension="m4a"),
        "passthrough": _fi("passthrough"),
    }
    return AudioConfig(filters=palette, chains=chains, select=select or [], concurrency=1)


def _track(tmp_path: Path, stem: str, *, track_id: int = 1, language: str = "eng", layout: str = "5.1") -> AudioStream:
    """Create a real source file and its ``AudioStream``.

    The file must exist so the source is real; the chain output is written
    by the fake executor into the phase's dedicated ``work_dir/audio`` dir.
    ``track_id`` disambiguates multi-track fixtures — the chain output name is
    derived from the stream's safe name, not the source stem.
    """
    src = LongPath(tmp_path) / f"{stem}.mkv"
    src.write_bytes(b"\x00" * 16)
    return AudioStream(
        file = ProbedFile(fingerprint=_STUB_SOURCE_FP, path=src),
        info = AudioStreamInfo(
            track_id=track_id, codec_name="flac", language=language,
            layout=ChannelLayout.parse(layout),
        ),
    )


# ---------------------------------------------------------------------------
# Phase builder with mocked Job + Extraction dependencies
# ---------------------------------------------------------------------------

def _make_phase(tmp_path: Path, config: AudioConfig, tracks: list[AudioStream]) -> AudioPhase:
    """Return an ``AudioPhase`` wired to mock Job + Extraction results.

    ``work_dir`` is ``tmp_path`` so chain outputs land in the phase's dedicated
    ``tmp_path/audio`` directory (Phase Contract: the phase owns its folder).
    """
    from pyqenc.metrics import NoOpMetricsCollector

    app_config = MagicMock()
    app_config.audio = config

    job_result = MagicMock()
    job_result.work_dir   = tmp_path
    job_result.force = False
    job_result.file.payload.fingerprint = _STUB_SOURCE_FP
    job_result.config     = app_config

    job_mock = MagicMock()
    job_mock.result = job_result

    from pyqenc.phase import Artifact, PhaseRegistry
    from pyqenc.state import ArtifactState

    extraction_result = MagicMock()
    extraction_result.audio_streams = [
        Artifact(payload=t, state=ArtifactState.COMPLETE) for t in tracks
    ]

    extraction_mock = MagicMock()
    extraction_mock.result = extraction_result

    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase



    registry: PhaseRegistry = {}
    phase = AudioPhase(app_config, registry, collector=NoOpMetricsCollector())
    registry[JobPhase]        = job_mock
    registry[ExtractionPhase] = extraction_mock
    return phase


# ---------------------------------------------------------------------------
# Fake chain executor — writes the deterministic output, no ffmpeg
# ---------------------------------------------------------------------------

def _fake_execute_chain_factory(record: list[tuple[str, str]]):
    """Return an async fake for ``execute_chain`` that writes the output file.

    Records each ``(chain_name, source_stem)`` it produces so tests can assert
    exactly which (track, chain) jobs actually ran.
    """
    async def _fake(
        resolved:   ResolvedChain,
        stream:     AudioStream,
        output_dir: Path,
        **_kw,
    ) -> Path:
        out = chain_output_path(stream, resolved.name, resolved.encode.extension, output_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00")
        record.append((resolved.name, stream.file.path.stem))
        return out
    return _fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProduceAndReuse:
    def test_produces_one_output_per_track_per_chain(self, tmp_path: Path) -> None:
        """Bug: a chain must produce exactly one output per selected track."""
        config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
        tracks = [_track(tmp_path, "t1", track_id=1), _track(tmp_path, "t2", track_id=2)]
        phase  = _make_phase(tmp_path, config, tracks)

        record: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)):
            result = phase.run()

        assert result.outcome == PhaseOutcome.COMPLETED
        assert sorted(record) == [("normal", "t1"), ("normal", "t2")]
        assert _chain_out(tmp_path, tracks[0], "normal").exists()
        assert _chain_out(tmp_path, tracks[1], "normal").exists()
        # Bug guarded: each track's output carries its OWN stream name —
        # identical names across tracks silently overwrite each other.
        assert _chain_out(tmp_path, tracks[0], "normal") != _chain_out(tmp_path, tracks[1], "normal")

    def test_unchanged_chain_is_reused_not_reprocessed(self, tmp_path: Path) -> None:
        """Bug: an unchanged chain with its output present must NOT be re-run."""
        config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
        tracks = [_track(tmp_path, "t1")]

        # First run produces the output and commits the sidecar.
        phase1 = _make_phase(tmp_path, config, tracks)
        rec1: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(rec1)):
            phase1.run()
        assert rec1 == [("normal", "t1")]

        # Second run: nothing changed and the file exists → reuse, no production.
        phase2 = _make_phase(tmp_path, config, tracks)
        rec2: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(rec2)):
            result = phase2.run()

        assert rec2 == []                              # nothing re-produced
        assert result.outcome == PhaseOutcome.REUSED


class TestInvalidation:
    def test_changed_param_reprocesses_that_chain_across_tracks(self, tmp_path: Path) -> None:
        """Bug: editing a chain's filter param must delete + reproduce its outputs.

        The reprocess must cover every selected track, not just one.
        """
        tracks = [_track(tmp_path, "t1", track_id=1), _track(tmp_path, "t2", track_id=2)]

        # First run with peaknorm(target=-1.0).
        cfg_v1 = _audio_config([ChainSpec(name="norm", filters=["peaknorm"])])
        phase1 = _make_phase(tmp_path, cfg_v1, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        out1 = _chain_out(tmp_path, tracks[0], "norm")
        out2 = _chain_out(tmp_path, tracks[1], "norm")
        assert out1.exists() and out2.exists()

        # Second run: the chain now references a DIFFERENT peaknorm (target=-2.0).
        cfg_v2 = _audio_config([ChainSpec(name="norm", filters=["peaknorm2"])])
        phase2 = _make_phase(tmp_path, cfg_v2, tracks)
        rec2: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(rec2)):
            result = phase2.run()

        assert sorted(rec2) == [("norm", "t1"), ("norm", "t2")]   # both reproduced
        assert result.outcome == PhaseOutcome.COMPLETED

    def test_removed_chain_outputs_cleaned_up(self, tmp_path: Path) -> None:
        """Bug: a chain removed from config must have its outputs cleaned up."""
        tracks = [_track(tmp_path, "t1")]

        cfg_v1 = _audio_config([
            ChainSpec(name="keep", filters=["peaknorm"]),
            ChainSpec(name="drop", filters=["dynaudnorm"]),
        ])
        phase1 = _make_phase(tmp_path, cfg_v1, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        drop_out = _chain_out(tmp_path, tracks[0], "drop")
        keep_out = _chain_out(tmp_path, tracks[0], "keep")
        assert drop_out.exists() and keep_out.exists()

        # Second run: 'drop' is gone from config.
        cfg_v2 = _audio_config([ChainSpec(name="keep", filters=["peaknorm"])])
        phase2 = _make_phase(tmp_path, cfg_v2, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase2.run()

        assert not drop_out.exists()      # cleaned up
        assert keep_out.exists()          # untouched

    def test_exact_name_deletion_spares_prefixed_chain(self, tmp_path: Path) -> None:
        """Bug: invalidating ``night`` must not delete ``nightlong`` (substring match)."""
        tracks = [_track(tmp_path, "t1")]

        cfg_v1 = _audio_config([
            ChainSpec(name="night",     filters=["peaknorm"]),
            ChainSpec(name="nightlong", filters=["dynaudnorm"]),
        ])
        phase1 = _make_phase(tmp_path, cfg_v1, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        nightlong_out = _chain_out(tmp_path, tracks[0], "nightlong")
        assert nightlong_out.exists()

        # Change ONLY 'night' (different peaknorm). 'nightlong' is unchanged.
        cfg_v2 = _audio_config([
            ChainSpec(name="night",     filters=["peaknorm2"]),
            ChainSpec(name="nightlong", filters=["dynaudnorm"]),
        ])
        phase2 = _make_phase(tmp_path, cfg_v2, tracks)
        rec2: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(rec2)):
            phase2.run()

        assert nightlong_out.exists()                     # NOT deleted by substring
        assert rec2 == [("night", "t1")]                  # only 'night' reproduced

    def test_extension_change_old_output_swept(self, tmp_path: Path) -> None:
        """Bug guarded (listing protocol, 2026-10-10): a chain change that
        flips the output extension must sweep the OLD-extension file —
        the sweep works from the listing + live expected names, with no
        persisted extension fact to remember the old name."""
        tracks = [_track(tmp_path, "t1")]

        cfg_v1 = _audio_config([ChainSpec(name="norm", filters=["peaknorm"])])
        phase1 = _make_phase(tmp_path, cfg_v1, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        old_out = _chain_out(tmp_path, tracks[0], "norm")
        assert old_out.exists()

        # Simulate the extension flip: the same track×chain output name,
        # different extension — what a changed encode would leave behind.
        stale = old_out.with_suffix(".mka")
        stale.write_bytes(b"stale old-extension bytes")

        cfg_v2 = _audio_config([ChainSpec(name="norm", filters=["peaknorm2"])])
        phase2 = _make_phase(tmp_path, cfg_v2, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase2.run()

        assert not stale.exists(), "old-extension leftover must be swept"
        assert old_out.exists(), "the reproduced (current-extension) output stays"

    def test_deselected_track_output_swept(self, tmp_path: Path) -> None:
        """Bug guarded (uniform leftover policy, 2026-10-10): outputs of a
        track that left the selection are leftovers of the same class as
        removed chains' outputs — swept, not retained (previously they
        lingered only because the old mechanism could not name them)."""
        eng = _track(tmp_path, "t_eng", track_id=1, language="eng")
        rus = _track(tmp_path, "t_rus", track_id=2, language="rus")

        cfg_all = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
        )
        phase1 = _make_phase(tmp_path, cfg_all, [eng, rus])
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        rus_out = _chain_out(tmp_path, rus, "normal")
        assert rus_out.exists()

        cfg_eng = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
            select=[SelectEntry.model_validate({"for": "lang=eng"})],
        )
        phase2 = _make_phase(tmp_path, cfg_eng, [eng, rus])
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase2.run()

        assert not rus_out.exists(), "deselected-track output must be swept"
        assert _chain_out(tmp_path, eng, "normal").exists()


class TestSelection:
    def test_selection_recomputed_picks_right_tracks(self, tmp_path: Path) -> None:
        """Bug: selection must be recomputed each run and drive which tracks process."""
        eng = _track(tmp_path, "t_eng", track_id=1, language="eng")
        rus = _track(tmp_path, "t_rus", track_id=2, language="rus")
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
            select=[SelectEntry.model_validate({"for": "lang=eng"})],
        )
        phase = _make_phase(tmp_path, config, [eng, rus])

        record: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)):
            phase.run()

        assert record == [("normal", "t_eng")]                    # only eng selected
        assert not _chain_out(tmp_path, rus, "normal").exists()


class TestResumability:
    def test_sidecar_committed_before_producing(self, tmp_path: Path) -> None:
        """Bug: sidecar written only after completion → perpetual re-invalidation.

        The sidecar must already reflect the current resolved chains by the time
        production starts, so a crash mid-run resumes rather than re-invalidates.
        We assert the sidecar is committed even when production is interrupted
        (the fake raises before writing any file).
        """
        config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
        tracks = [_track(tmp_path, "t1")]
        phase  = _make_phase(tmp_path, config, tracks)

        async def _boom(*_a, **_k):
            raise RuntimeError("simulated crash during production")

        with patch("pyqenc.phases.audio.execute_chain", _boom), pytest.raises(RuntimeError):
            phase.run()

        # Sidecar was committed in _recover, BEFORE the crashing production.
        sidecar = load_model(LongPath(tmp_path) / _AUDIO_YAML, AudioSidecar)
        assert sidecar is not None
        assert "normal" in sidecar.chains


class TestPassthrough:
    def test_passthrough_fails_one_output_not_the_phase(self, tmp_path: Path) -> None:
        """Bug: a passthrough chain must fail loudly as one output, not crash the phase."""
        config = _audio_config([ChainSpec(name="copy", filters=["passthrough"])])
        tracks = [_track(tmp_path, "t1")]
        phase  = _make_phase(tmp_path, config, tracks)

        # Do NOT patch execute_chain — the real executor raises NotImplementedError
        # from the passthrough filter, which the phase must surface as a failed
        # output (not an unhandled crash).
        result = phase.run()

        assert result.outcome == PhaseOutcome.FAILED
        assert not _chain_out(tmp_path, tracks[0], "copy").exists()


class TestBoundedExecution:
    """The bounded runner integration: order, per-chain dotted timing, and
    the concurrency key-params announcement.

    **Validates: concurrency spec Req 6–8, 14**
    """

    def test_key_params_announce_concurrency_only_above_one(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Concurrency appears in the key-params block only when > 1.

        Bug condition: interleaved per-job log lines at concurrency > 1
        read as a bug without an announced cause, while a constant
        "Concurrency: 1" line on every run is pure noise.
        """
        import logging

        tracks = [_track(tmp_path, "t1")]

        def _run(work_dir: str, concurrency: int) -> None:
            config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
            config.concurrency = concurrency
            (tmp_path / work_dir).mkdir(exist_ok=True)
            phase = _make_phase(tmp_path / work_dir, config, tracks)
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="pyqenc.phases.audio"):
                phase.run()

        # fmt_key_value_table logs under its own module logger, so the level
        # is set at root rather than on the phase logger.
        with (
            patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])),
            caplog.at_level(logging.INFO),
        ):
            _run("w1", 1)
            assert "Concurrency" not in caplog.text

            _run("w2", 2)
            assert "Concurrency:" in caplog.text
            assert "2" in caplog.text

    def test_jobs_run_in_pending_order_at_every_concurrency(self, tmp_path: Path) -> None:
        """Bug guarded: raising audio.concurrency must not change the job order.

        A bound > 1 that started jobs out of pending-list order would make
        run logs and output appearance machine-dependent (Req 7).
        """
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"]),
             ChainSpec(name="night",  filters=["peaknorm2", "aac"])],
        )
        config.concurrency = 3
        tracks = [_track(tmp_path, "t1", track_id=1), _track(tmp_path, "t2", track_id=2)]
        phase  = _make_phase(tmp_path, config, tracks)

        record: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)):
            result = phase.run()

        assert result.outcome == PhaseOutcome.COMPLETED
        # Pending order is the classified (track, chain) enumeration —
        # identical at bound 1 and 3.
        assert record == [
            ("normal", "t1"), ("night", "t1"),
            ("normal", "t2"), ("night", "t2"),
        ]

    def test_per_chain_dotted_timing_recorded(self, tmp_path: Path) -> None:
        """Bug guarded (Req 14, ex-TODO §12): each chain's work is timed
        under its own ``audio.<chain>`` dotted key.

        A missing per-chain span leaves the audio phase with a single
        top-level wall-time entry — no way to see which chain dominates.
        """
        import yaml

        from pyqenc.metrics import YamlMetricsCollector

        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"]),
             ChainSpec(name="night",  filters=["peaknorm2", "aac"])],
        )
        tracks = [_track(tmp_path, "t1")]
        registry: dict = {}

        from pyqenc.phase import Artifact
        from pyqenc.state import ArtifactState

        job_result = MagicMock()
        job_result.work_dir = tmp_path
        job_result.force = False
        job_result.file.payload.fingerprint = _STUB_SOURCE_FP
        app_config = MagicMock()
        app_config.audio = config
        job_result.config = app_config
        job_mock = MagicMock()
        job_mock.result = job_result
        extraction_result = MagicMock()
        extraction_result.audio_streams = [
            Artifact(payload=t, state=ArtifactState.COMPLETE) for t in tracks
        ]
        extraction_mock = MagicMock()
        extraction_mock.result = extraction_result

        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        collector = YamlMetricsCollector(tmp_path)
        phase = AudioPhase(app_config, registry, collector=collector)
        registry[JobPhase] = job_mock
        registry[ExtractionPhase] = extraction_mock

        # Sub-second spans truncate to zero rows at report time (TODO §3) —
        # sleep past one second so the dotted entries survive the dump.
        async def _slow_fake(resolved: ResolvedChain, stream: AudioStream, output_dir: Path) -> Path:
            await asyncio.sleep(1.05)
            fake = _fake_execute_chain_factory([])
            return await fake(resolved, stream, output_dir)

        with patch("pyqenc.phases.audio.execute_chain", _slow_fake):
            result = phase.run()

        assert result.outcome == PhaseOutcome.COMPLETED
        collector.flush()
        raw = yaml.safe_load((tmp_path / "metrics.yaml").read_text(encoding="utf-8-sig"))
        dotted = raw["time_distribution"]["dotted"]
        keys = {e["key"] for group in dotted.values() for e in group["breakdown"]}
        assert {"audio.normal", "audio.night"} <= keys


class TestSelectionTable:
    """The run-log selection summary: the per-track table + the selector reason.

    **Validates: TODO §45 — the audio phase's log answers "which streams were
    picked up, in what state, and why".**
    """

    @staticmethod
    def _run_logged(
        work_dir: Path,
        config:   AudioConfig,
        tracks:   list[AudioStream],
        caplog,
    ) -> list[tuple[str, str]]:
        """Run the phase and leave its INFO+ log in *caplog*; return the record.

        The record of executed (chain, stem) jobs lets callers derive the
        selected set from the run itself.
        """
        record: list[tuple[str, str]] = []
        phase = _make_phase(work_dir, config, tracks)
        caplog.clear()
        with (
            patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)),
            caplog.at_level(logging.INFO),
        ):
            phase.run()
        return record

    @staticmethod
    def _name_lines(caplog, track: AudioStream) -> list[str]:
        """The summary lines naming *track* — exactly one row per track."""
        return [
            line for line in caplog.text.splitlines()
            if track.display_name() in line
        ]

    def test_table_shows_want_per_track_and_selector_reason(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Bug: the run log never said which audio tracks were picked up.

        With a selector configured, every extracted track appears in the
        summary exactly once — selected rows carry the success glyph,
        deselected rows the failure glyph — and the selector's ``for``
        pattern is logged as the reason for the split.
        """
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
            select=[SelectEntry(for_="lang=eng")],
        )
        tracks = [
            _track(tmp_path, "t1", track_id=1, language="eng"),
            _track(tmp_path, "t2", track_id=2, language="rus"),
        ]
        self._run_logged(tmp_path, config, tracks, caplog)

        for track, wanted in zip(tracks, (True, False)):
            lines = self._name_lines(caplog, track)
            assert len(lines) == 1
            if wanted:
                assert SUCCESS_SYMBOL_MINOR in lines[0]
            else:
                assert SUCCESS_SYMBOL_MINOR not in lines[0]
                assert FAILURE_SYMBOL_MINOR in lines[0]
        assert "for=lang=eng" in caplog.text

    def test_present_glyph_marks_fully_reused_tracks(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Bug: per-track reuse was invisible — a track whose every chain
        output is already on disk must show both success glyphs on a
        re-run that produces nothing."""
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"]),
             ChainSpec(name="night",  filters=["peaknorm2", "aac"])],
        )
        tracks = [_track(tmp_path, "t1", track_id=1)]
        self._run_logged(tmp_path, config, tracks, caplog)
        record = self._run_logged(tmp_path, config, tracks, caplog)

        assert record == []                       # pure reuse
        lines = self._name_lines(caplog, tracks[0])
        assert len(lines) == 1
        assert lines[0].count(SUCCESS_SYMBOL_MINOR) == 2   # want + present

    def test_empty_select_picks_everything_without_entry_lines(
        self, tmp_path: Path, caplog,
    ) -> None:
        """With no select config every track is wanted and no selector
        entry lines appear — the pick-everything fallback needs no reason."""
        config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
        tracks = [_track(tmp_path, "t1", track_id=1), _track(tmp_path, "t2", track_id=2)]
        self._run_logged(tmp_path, config, tracks, caplog)

        assert "for=" not in caplog.text
        for track in tracks:
            lines = self._name_lines(caplog, track)
            assert len(lines) == 1
            assert SUCCESS_SYMBOL_MINOR in lines[0]

    def test_no_tracks_in_inventory_warns_and_skips_selector(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Bug: a source without audio streams printed an empty summary —
        a warning belongs there instead, no selector reason (nothing was
        selected), and the phase still finishes green (nothing pending →
        REUSED by the template's mapping)."""
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
            select=[SelectEntry(for_="lang=eng")],
        )
        with (
            patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])),
            caplog.at_level(logging.INFO),
        ):
            result = _make_phase(tmp_path, config, []).run()

        assert result.is_complete
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert "for=lang=eng" not in caplog.text

    @settings(
        deadline=None,
        max_examples=25,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        langs =st.lists(st.sampled_from(["eng", "rus", "jpn"]), min_size=0, max_size=5),
        picked=st.sampled_from(["eng", "rus", "jpn"]),
    )
    def test_properties_table_covers_every_track_exactly_once(
        self, langs: list[str], picked: str, caplog,
    ) -> None:
        """Property: whatever the inventory and the picked language, the
        summary lists every extracted track exactly once, and exactly the
        selector-picked tracks carry the success glyph.

        caplog is reset per example (function-scoped fixture, manually
        cleared); the phase runs in a fresh temp dir per example.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tracks = [
                _track(tmp_path, f"t{i}", track_id=i + 1, language=lang)
                for i, lang in enumerate(langs)
            ]
            config = _audio_config(
                [ChainSpec(name="normal", filters=["peaknorm"])],
                select=[SelectEntry(for_=f"lang={picked}")],
            )
            record = self._run_logged(tmp_path, config, tracks, caplog)

            selected_stems = {stem for _, stem in record}
            wanted_lines = 0
            for track in tracks:
                lines = self._name_lines(caplog, track)
                assert len(lines) == 1
                if SUCCESS_SYMBOL_MINOR in lines[0]:
                    wanted_lines += 1
            assert wanted_lines == len(selected_stems)
