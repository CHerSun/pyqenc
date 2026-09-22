"""Behaviour tests for the rewritten :class:`AudioPhase` (Task 8, Req 9.x/10.x).

Each test names the concrete bug it guards against and asserts observable
behaviour — which output files exist on disk and which artifacts the phase
result reports — never internal loop state. A **fake** ``execute_chain``
replaces the real chain executor so no ffmpeg runs; it writes a placeholder byte
to the deterministic ``<stem> chain=<name>.<ext>`` output so completion (read
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

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.app_config import AudioConfig, ChainSpec, FilterInstance, SelectEntry
from pyqenc.audio.chain import ResolvedChain, chain_output_path
from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import AUDIO_OUTPUT_DIR
from pyqenc.models import AudioMetadata, PhaseOutcome
from pyqenc.state import AudioSidecar
from pyqenc.utils.long_path import LongPath

_AUDIO_YAML = "audio.yaml"


def _audio_out(tmp_path: Path, name: str) -> LongPath:
    """Expected output path in the phase's DEDICATED audio dir (``work_dir/audio``)."""
    return LongPath(tmp_path) / AUDIO_OUTPUT_DIR / name


# ---------------------------------------------------------------------------
# Config + track builders
# ---------------------------------------------------------------------------

def _fi(type_id: str, **params) -> FilterInstance:
    """Build a validated config-side ``FilterInstance``."""
    return FilterInstance(type=type_id, **params)  # type: ignore[arg-type]


def _audio_config(chains: list[ChainSpec], select: list[SelectEntry] | None = None) -> AudioConfig:
    """Build an ``AudioConfig`` with a small palette and the given chains/select."""
    palette = {
        "peaknorm":  _fi("peaknorm", target_dbfs=-1.0),
        "peaknorm2": _fi("peaknorm", target_dbfs=-2.0),   # a *different* peaknorm
        "dynaudnorm": _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.95, maxgain=10.0, targetrms=0.0),
        "aac":       _fi("encode", codec="aac", bitrate_per_channel="96k", extension="m4a"),
        "passthrough": _fi("passthrough"),
    }
    return AudioConfig(filters=palette, chains=chains, select=select or [])


def _track(tmp_path: Path, stem: str, *, language: str = "eng", layout: str = "5.1") -> AudioMetadata:
    """Create a real source-track file and its ``AudioMetadata``.

    The file must exist so the source stem is real; the chain output is written
    by the fake executor into the phase's dedicated ``work_dir/audio`` dir.
    """
    src = LongPath(tmp_path) / f"{stem}.mka"
    src.write_bytes(b"\x00" * 16)
    return AudioMetadata(path=src, language=language, layout=ChannelLayout.parse(layout))


# ---------------------------------------------------------------------------
# Phase builder with mocked Job + Extraction dependencies
# ---------------------------------------------------------------------------

def _make_phase(tmp_path: Path, config: AudioConfig, tracks: list[AudioMetadata]) -> object:
    """Return an ``AudioPhase`` wired to mock Job + Extraction results.

    ``work_dir`` is ``tmp_path`` so chain outputs land in the phase's dedicated
    ``tmp_path/audio`` directory (Phase Contract: the phase owns its folder).
    """
    from pyqenc.metrics import NoOpMetricsCollector
    from pyqenc.phases.audio import AudioPhase

    app_config = MagicMock()
    app_config.audio = config

    job_result = MagicMock()
    job_result.work_dir   = tmp_path
    job_result.force_wipe = False
    job_result.config     = app_config

    job_mock = MagicMock()
    job_mock.result = job_result

    extraction_result = MagicMock()
    extraction_result.audio = tracks

    extraction_mock = MagicMock()
    extraction_mock.result = extraction_result

    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase

    registry: dict[type, object] = {}
    phase = AudioPhase(app_config, registry, collector=NoOpMetricsCollector())  # type: ignore[arg-type]
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
        source:     LongPath,
        layout:     ChannelLayout,
        output_dir: LongPath,
        **_kw,
    ) -> LongPath:
        out = chain_output_path(source, resolved.name, resolved.encode.extension, output_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00")
        record.append((resolved.name, source.stem))
        return out
    return _fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProduceAndReuse:
    def test_produces_one_output_per_track_per_chain(self, tmp_path: Path) -> None:
        """Bug: a chain must produce exactly one output per selected track."""
        config = _audio_config([ChainSpec(name="normal", filters=["peaknorm"])])
        tracks = [_track(tmp_path, "t1"), _track(tmp_path, "t2")]
        phase  = _make_phase(tmp_path, config, tracks)

        record: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)):
            result = phase.run()

        assert result.outcome == PhaseOutcome.COMPLETED
        assert sorted(record) == [("normal", "t1"), ("normal", "t2")]
        assert _audio_out(tmp_path, "t1 chain=normal.flac").exists()
        assert _audio_out(tmp_path, "t2 chain=normal.flac").exists()

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
        tracks = [_track(tmp_path, "t1"), _track(tmp_path, "t2")]

        # First run with peaknorm(target=-1.0).
        cfg_v1 = _audio_config([ChainSpec(name="norm", filters=["peaknorm"])])
        phase1 = _make_phase(tmp_path, cfg_v1, tracks)
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory([])):
            phase1.run()
        out1 = _audio_out(tmp_path, "t1 chain=norm.flac")
        out2 = _audio_out(tmp_path, "t2 chain=norm.flac")
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
        drop_out = _audio_out(tmp_path, "t1 chain=drop.flac")
        keep_out = _audio_out(tmp_path, "t1 chain=keep.flac")
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
        nightlong_out = _audio_out(tmp_path, "t1 chain=nightlong.flac")
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


class TestSelection:
    def test_selection_recomputed_picks_right_tracks(self, tmp_path: Path) -> None:
        """Bug: selection must be recomputed each run and drive which tracks process."""
        eng = _track(tmp_path, "t_eng", language="eng")
        rus = _track(tmp_path, "t_rus", language="rus")
        config = _audio_config(
            [ChainSpec(name="normal", filters=["peaknorm"])],
            select=[SelectEntry.model_validate({"for": "lang=eng"})],
        )
        phase = _make_phase(tmp_path, config, [eng, rus])

        record: list[tuple[str, str]] = []
        with patch("pyqenc.phases.audio.execute_chain", _fake_execute_chain_factory(record)):
            phase.run()

        assert record == [("normal", "t_eng")]                    # only eng selected
        assert not _audio_out(tmp_path, "t_rus chain=normal.flac").exists()


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
        sidecar = AudioSidecar.load(LongPath(tmp_path) / _AUDIO_YAML)
        assert sidecar is not None
        assert "normal" in sidecar.signatures


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
        assert not _audio_out(tmp_path, "t1 chain=copy.flac").exists()
