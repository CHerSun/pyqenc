"""Unit tests for scene detection → timestamp-window chunks (direct-from-source).

Covers the window model (spec ``2026-09-25 file-stream-model``, Req 4/9.4):
- chunk-id formatting/parsing round-trips (owned by ``VideoStreamChunk``)
- ``build_chunks``: boundaries + stream duration → windows with
  detector-derived frame counts that telescope to the source total
- ``detect_scenes`` zero-scene fallback (single boundary at frame 0)
- ``ChunkingPhase`` sidecar lifecycle: scenes persisted on execute, reused
  from ``chunking.yaml`` without re-detection, absent → pending
"""

from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest

from pyqenc.app_config import load_app_config
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome, SceneBoundary
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.chunking import (
    ChunkingPhase,
    build_chunks,
    detect_scenes,
)
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState
from pyqenc.stream_model import (
    CropParams,
    ExtendedVideoStream,
    File,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)

_APP_CONFIG = load_app_config(default_only=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stream(frame_count: int = 640, duration: float = 26.67) -> ExtendedVideoStream:
    """An extended video stream: 24000/1001 fps, 1920x1080, 640 frames."""
    return ExtendedVideoStream(
        stream = VideoStream(
            file = File(path="D:/media/source.mkv", file_size_bytes=64),
            info = VideoStreamInfo(
                track_id=0, codec_name="hevc",
                fps=23.976, fps_fraction=Fraction(24000, 1001),
                resolution="1920x1080", duration_seconds=duration,
            ),
        ),
        frame_count = frame_count,
        crop        = CropParams(),
    )


def _boundaries() -> list[SceneBoundary]:
    return [
        SceneBoundary(frame=0,   timestamp_seconds=0.0),
        SceneBoundary(frame=320, timestamp_seconds=13.33),
    ]


def _make_registry(
    work_dir: Path,
    stream: ExtendedVideoStream | None,
) -> PhaseRegistry:
    """A real registry with pre-set Job/Extraction/Probe results."""
    collector = NoOpMetricsCollector()
    config = _APP_CONFIG.model_copy(deep=True)
    source = work_dir / "source.mkv"

    job_result = JobPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "job complete",
        file      = Artifact(payload=File(path=source, file_size_bytes=64), state=ArtifactState.COMPLETE),
        config    = config,
        work_dir  = work_dir,
        source    = source,
    )
    job = JobPhase(config, {}, source=source, work_dir=work_dir, force=False,
                   cleanup=CleanupLevel.NONE, no_metrics=True, collector=collector)
    job.result = job_result

    extraction = ExtractionPhase(config, {}, collector=collector)
    extraction.result = ExtractionPhaseResult(
        outcome=PhaseOutcome.COMPLETED, message="extraction",
        video_stream=(
            Artifact(payload=stream.stream, state=ArtifactState.COMPLETE)
            if stream is not None else None
        ),
    )

    probe = ProbePhase(config, {}, collector=collector)
    probe.result = ProbePhaseResult(
        outcome=PhaseOutcome.COMPLETED, message="probe",
        stream=(
            Artifact(payload=stream, state=ArtifactState.COMPLETE)
            if stream is not None else None
        ),
    )

    registry: PhaseRegistry = {JobPhase: job, ExtractionPhase: extraction, ProbePhase: probe}
    return registry


def _make_phase(work_dir: Path, stream: ExtendedVideoStream | None) -> ChunkingPhase:
    collector = NoOpMetricsCollector()
    config = _APP_CONFIG.model_copy(deep=True)
    registry = _make_registry(work_dir, stream)
    return ChunkingPhase(config, registry, collector=collector)


# ---------------------------------------------------------------------------
# Chunk-id formatting (owned by VideoStreamChunk)
# ---------------------------------------------------------------------------

class TestChunkIdFormatting:
    def test_matches_chunk_name_pattern(self):
        """Output of VideoStreamChunk.format_chunk_id must match CHUNK_NAME_PATTERN."""
        from pyqenc.constants import CHUNK_NAME_PATTERN
        name = VideoStreamChunk.format_chunk_id(0.0, 13.33)
        assert CHUNK_NAME_PATTERN.match(name), f"Pattern mismatch: {name!r}"

    def test_zero_start(self):
        """Zero start timestamp produces correct zero-padded hours/minutes."""
        name = VideoStreamChunk.format_chunk_id(0.0, 13.33)
        assert name.startswith("00꞉00꞉"), f"Unexpected start: {name!r}"

    def test_range_separator_present(self):
        """The range separator '-' separates start and end timestamps."""
        name = VideoStreamChunk.format_chunk_id(0.0, 13.33)
        assert "-" in name, f"Missing range separator in: {name!r}"


# ---------------------------------------------------------------------------
# build_chunks — windows + detector-derived counts
# ---------------------------------------------------------------------------

class TestBuildChunks:
    def test_windows_from_boundaries(self):
        """Each boundary opens a [start, end) window; the last closes against
        the stream duration."""
        chunks = build_chunks(_boundaries(), _stream())
        assert len(chunks) == 2
        assert chunks[0].start_timestamp == 0.0
        assert chunks[0].end_timestamp == 13.33
        assert chunks[1].start_timestamp == 13.33
        assert chunks[1].end_timestamp == 26.67

    def test_frame_counts_telescope_to_source_total(self):
        """Req 9.4: chunk counts are detector boundary differences, the last
        closing against the source total — Σ chunks == source by construction."""
        stream  = _stream(frame_count=640)
        chunks  = build_chunks(_boundaries(), stream)
        assert [c.frame_count for c in chunks] == [320, 320]
        assert sum(c.frame_count for c in chunks) == stream.frame_count

    def test_unknown_source_total_gives_unknown_last_count(self):
        """frame_count=0 (unknown sentinel) on the stream → last chunk count
        is also unknown, never a fabricated number."""
        stream = _stream(frame_count=0)
        chunks = build_chunks(_boundaries(), stream)
        assert chunks[0].frame_count == 320
        assert chunks[1].frame_count == 0

    def test_no_duration_raises(self):
        """Without a stream duration the last window cannot close — a loud
        failure, not a silently wrong window."""
        stream = _stream()
        stream = stream.model_copy(update={
            "stream": stream.stream.model_copy(update={
                "info": stream.stream.info.model_copy(update={"duration_seconds": None}),
            }),
        })
        with pytest.raises(Exception, match="duration"):
            build_chunks(_boundaries(), stream)

    def test_chunk_ids_derive_from_windows(self):
        """The chunk id is a pure function of the window — never stored."""
        chunks = build_chunks(_boundaries(), _stream())
        assert chunks[0].safe_name() == VideoStreamChunk.format_chunk_id(0.0, 13.33)

    def test_as_input_carries_the_window(self):
        """The chunk input is the source + selector + input-side window."""
        chunks = build_chunks(_boundaries(), _stream())
        inp = chunks[0].as_input()
        assert str(inp.path).endswith("source.mkv")
        assert inp.selector == "0:0"
        assert inp.start_seconds == 0.0
        assert inp.duration_seconds == pytest.approx(13.33)


# ---------------------------------------------------------------------------
# detect_scenes — zero-scene fallback
# ---------------------------------------------------------------------------

class TestDetectScenes:
    def test_zero_scenes_produces_single_boundary(self, tmp_path):
        """When the detector returns no scenes, the entire video is one chunk
        (a single boundary at frame 0)."""
        stream = _stream(duration=13.33)
        stream = stream.model_copy(update={
            "stream": stream.stream.model_copy(update={
                "file": File(path=tmp_path / "source.mkv"),
            }),
        })
        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            boundaries = detect_scenes(stream, scene_threshold=27.0, min_scene_length=24)
        assert boundaries == [SceneBoundary(frame=0, timestamp_seconds=0.0)]


# ---------------------------------------------------------------------------
# ChunkingPhase — sidecar lifecycle
# ---------------------------------------------------------------------------

class TestChunkingPhaseLifecycle:
    def test_execute_detects_and_persists_scenes(self, tmp_path):
        """A fresh run detects scenes, writes chunking.yaml (boundaries with
        detector frames), and emits the derived windows."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        phase = _make_phase(work_dir, _stream())

        with patch("pyqenc.phases.chunking.detect_scenes", return_value=_boundaries()):
            result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert len(result.chunks) == 2
        assert all(a.state == ArtifactState.COMPLETE for a in result.chunks)
        assert sum(a.payload.frame_count for a in result.chunks) == 640

        import yaml
        data = yaml.safe_load((work_dir / "chunking.yaml").read_text(encoding="utf-8"))
        assert set(data) == {"scenes"}
        assert [rec["timestamp_seconds"] for rec in data["scenes"]] == [0.0, 13.33]
        assert data["scenes"][1]["frame"] == 320

    def test_reuse_loads_scenes_without_detection(self, tmp_path):
        """Bug prevented: re-running scene detection on every run — cached
        boundaries must be loaded and the phase return REUSED."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.detect_scenes", return_value=_boundaries()):
            phase.run(dry_run=False)

        phase2 = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.detect_scenes") as detect_mock:
            result = phase2.run(dry_run=False)

        detect_mock.assert_not_called()
        assert result.outcome == PhaseOutcome.REUSED
        assert len(result.chunks) == 2

    def test_no_sidecar_is_pending(self, tmp_path):
        """No chunking.yaml → the phase is pending (detection must run)."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.detect_scenes", return_value=_boundaries()):
            result = phase.run(dry_run=True)
        assert result.outcome == PhaseOutcome.PENDING
        assert not (work_dir / "chunking.yaml").exists()

    def test_force_wipe_invalidates_cached_scenes(self, tmp_path):
        """force_wipe clears chunking.yaml so detection re-runs. Legacy
        pre-spec ``chunks/`` leftovers are nobody's concern (no-migration
        policy) and are left in place."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        legacy = work_dir / "chunks"
        legacy.mkdir()
        (legacy / "old.mkv").write_bytes(b"x")

        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.detect_scenes", return_value=_boundaries()):
            phase.run(dry_run=False)
        assert (work_dir / "chunking.yaml").exists()

        # Rebuild the registry with force_wipe set on the job result.
        registry = _make_registry(work_dir, _stream())
        job = registry[JobPhase]
        assert job.result is not None
        job.result.force_wipe = True
        collector = NoOpMetricsCollector()
        config = _APP_CONFIG.model_copy(deep=True)
        phase2 = ChunkingPhase(config, registry, collector=collector)
        with patch("pyqenc.phases.chunking.detect_scenes", return_value=_boundaries()) as detect_mock:
            phase2.run(dry_run=False)

        assert detect_mock.called, "force_wipe must invalidate the cached scenes"
        assert legacy.exists(), "legacy leftovers are deliberately untouched"
        assert (work_dir / "chunking.yaml").exists()
