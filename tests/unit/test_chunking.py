"""Unit tests for statistical chunking → timestamp-window chunks.

Covers the window model (spec ``2026-09-25 file-stream-model``, Req 4/9.4):
- chunk-id formatting/parsing round-trips (owned by ``VideoStreamChunk``)
- ``build_chunks``: boundaries + stream duration → windows with
  boundary-derived frame counts that telescope to the source total
- ``ChunkingPhase`` sidecar lifecycle: mode-scoring → split → persist,
  reuse from ``chunking.yaml`` without re-scoring, absent → pending,
  params change → re-detect, zero boundaries → whole-video single chunk

The scoring providers are patched at their module seam (they decode real
video); the splitter itself runs live in every lifecycle test. The
content scorer's frame axis is tested with scenedetect's own classes
replaced by fakes — the external decode boundary.
"""

from collections.abc import Generator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.app_metrics import NoOpAppMetricsCollector
from pyqenc.models import ArtifactState, CleanupLevel, PhaseOutcome, SceneBoundary, SizedFingerprint
from pyqenc.phase import Artifact, PhaseRegistry, RecoveryError
from pyqenc.phases.chunking import (
    ChunkingPhase,
    ChunkingSidecar,
    ScoringResult,
    build_chunks,
    fast_scoring,
)
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.stream_model import (
    CropParams,
    ExtendedVideoStream,
    ProbedFile,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)
from pyqenc.utils.yaml_utils import load_model, write_yaml_atomic

_STUB_SOURCE_FP = SizedFingerprint(token="0" * 32, size=64)
"""Stub source identity — phases read the job File's fingerprint."""

_APP_CONFIG = load_app_config(default_only=True)

_FRAME_COUNT = 640
_FRAME_PERIOD_MS = 41.708  # 23.976 fps, the ms-quantized grid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stream(frame_count: int = _FRAME_COUNT, duration: float = 26.67) -> ExtendedVideoStream:
    """An extended video stream: 24000/1001 fps, 1920x1080, 640 frames."""
    return ExtendedVideoStream(
        stream = VideoStream(
            file = ProbedFile(fingerprint=_STUB_SOURCE_FP, path="D:/media/source.mkv"),
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
        SceneBoundary(frame=320, timestamp_seconds=320 * _FRAME_PERIOD_MS / 1000),
    ]


def _step_trace() -> list[float]:
    """A trace with one clean cut spike at frame 320 — the splitter's
    must-find boundary (height clears the raw content guard)."""
    trace = [2.0] * 640
    trace[320] = 70.0
    return trace


def _extra_step_trace() -> list[float]:
    """A codec-feature trace with one step at frame 480 — fires with no
    content spike (the slow-mode shape)."""
    return [5.0] * 480 + [40.0] * 160


def _write_pts_index(work_dir: Path, frame_count: int = _FRAME_COUNT) -> None:
    """Write the extraction PTS index (ms-quantized grid, no end marker)."""
    index_dir = work_dir / "extracted"
    index_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{round(i * _FRAME_PERIOD_MS)}" for i in range(frame_count)]
    (index_dir / "timestamps.txt").write_text(
        "# timestamp format v2\n" + "\n".join(lines) + "\n", encoding="utf-8",
    )


def _make_registry(
    work_dir: Path,
    stream: ExtendedVideoStream | None,
    *,
    config: AppConfig | None = None,
    force:   bool = False,
) -> PhaseRegistry:
    """A real registry with pre-set Job/Extraction/Probe results."""
    collector = NoOpAppMetricsCollector()
    config = config.model_copy(deep=True) if config is not None else _APP_CONFIG.model_copy(deep=True)
    source = work_dir / "source.mkv"

    job_result = JobPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        message   = "job complete",
        file      = Artifact(payload=ProbedFile(fingerprint=_STUB_SOURCE_FP, path=source), state=ArtifactState.COMPLETE),
        config    = config,
        work_dir  = work_dir,
        force     = force,
            cleanup     = CleanupLevel.NONE,
            no_metrics  = True,
    )
    job = JobPhase(config, {}, source=source, work_dir=work_dir, force=False,
                   cleanup=CleanupLevel.NONE, no_metrics=True, collector=collector)
    job.result = job_result

    extraction = ExtractionPhase(config, {}, collector=collector)
    extraction.result = ExtractionPhaseResult(
        outcome=PhaseOutcome.COMPLETED, message="extraction",
        work_dir=work_dir,
        video_stream=(
            Artifact(payload=stream.stream, state=ArtifactState.COMPLETE)
            if stream is not None else None
        ),
    )

    probe = ProbePhase(config, {}, collector=collector, plan=config.build_encoding_plan(optimize=True, measure=True))
    probe.result = ProbePhaseResult(
        outcome=PhaseOutcome.COMPLETED, message="probe",
        plan=config.build_encoding_plan(optimize=True, measure=True),
        stream=(
            Artifact(payload=stream, state=ArtifactState.COMPLETE)
            if stream is not None else None
        ),
    )

    registry: PhaseRegistry = {JobPhase: job, ExtractionPhase: extraction, ProbePhase: probe}
    return registry


def _make_phase(work_dir: Path, stream: ExtendedVideoStream | None) -> ChunkingPhase:
    collector = NoOpAppMetricsCollector()
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
# build_chunks — windows + boundary-derived counts
# ---------------------------------------------------------------------------

class TestBuildChunks:
    def test_windows_from_boundaries(self):
        """Each boundary opens a [start, end) window; the last closes against
        the stream duration."""
        chunks = build_chunks(_boundaries(), _stream())
        assert len(chunks) == 2
        assert chunks[0].start_timestamp == 0.0
        assert chunks[0].end_timestamp == pytest.approx(320 * _FRAME_PERIOD_MS / 1000)
        assert chunks[1].start_timestamp == pytest.approx(320 * _FRAME_PERIOD_MS / 1000)
        assert chunks[1].end_timestamp == 26.67

    def test_frame_counts_telescope_to_source_total(self):
        """Req 9.4: chunk counts are boundary frame differences, the last
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
        expected_start = 0.0
        expected_end = 320 * _FRAME_PERIOD_MS / 1000
        assert chunks[0].safe_name() == VideoStreamChunk.format_chunk_id(expected_start, expected_end)

    def test_as_input_carries_the_window(self):
        """The chunk input is the source + selector + input-side window."""
        chunks = build_chunks(_boundaries(), _stream())
        inp = chunks[0].as_input()
        assert str(inp.path).endswith("source.mkv")
        assert inp.selector == "0:0"
        assert inp.start_seconds == 0.0
        assert inp.duration_seconds == pytest.approx(320 * _FRAME_PERIOD_MS / 1000)


# ---------------------------------------------------------------------------
# Content scoring — the pipeline's frame axis
# ---------------------------------------------------------------------------

class _FakeStatsManager:
    """scenedetect's StatsManager for a decoder that scored frames 1..N."""

    def __init__(self) -> None:
        self.scored = 0

    def metrics_exist(self, frame: int, keys: list[str]) -> bool:
        return 1 <= frame <= self.scored

    def get_metrics(self, frame: int, keys: list[str]) -> list[float]:
        return [float(frame)]


class _FakeSceneManager:
    """scenedetect's SceneManager: the "decode" scores a fixed number of frames."""

    scored = 0

    def __init__(self, stats_manager: _FakeStatsManager) -> None:
        self._stats = stats_manager

    def add_detector(self, detector: object) -> None:
        return None

    def detect_scenes(self, video: object, show_progress: bool = False) -> None:
        self._stats.scored = _FakeSceneManager.scored


@contextmanager
def _fake_decoder(scored: int) -> Generator[None]:
    """Stand in for the decode: scenedetect scores frames 1..*scored*."""
    _FakeSceneManager.scored = scored
    with (
        patch("pyqenc.phases.chunking.StatsManager", _FakeStatsManager),
        patch("pyqenc.phases.chunking.SceneManager", _FakeSceneManager),
        patch("pyqenc.phases.chunking.open_video", lambda path: object()),
    ):
        yield


class TestContentScoringFrameAxis:
    """The content trace is indexed by the pipeline's frames — the frame
    lines of the extraction PTS index — never by a count taken from the
    decoder."""

    def test_trace_has_one_score_per_pipeline_frame(self):
        """Happy path: given the pipeline's frame count, the trace carries
        exactly that many entries — frame 0 (no predecessor) scores 0 and
        every pipeline frame after it carries the decoder's score."""
        with _fake_decoder(scored=5):
            trace = fast_scoring(_stream(), frame_count=6).traces[0]
        assert trace == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]

    def test_decoder_short_of_the_pipeline_frames_fails(self):
        """Bug prevented: boundaries anchored to the wrong frames — when the
        decoder scores fewer frames than the pipeline's index holds, the
        frames cannot be matched up. The scorer must refuse, naming the
        first frame it has no score for, rather than pad the trace."""
        with _fake_decoder(scored=4), pytest.raises(RecoveryError, match=r"no frame 5 of the 6 frames"):
            fast_scoring(_stream(), frame_count=6)

    def test_decoder_scoring_past_the_pipeline_frames_fails(self):
        """Bug prevented: a duration-derived frame count taken as the number
        of frames — a decoder that reports more frames than the pipeline's
        index holds must be refused: its extra frames are not the
        pipeline's."""
        with _fake_decoder(scored=7), pytest.raises(RecoveryError, match=r"frame 6 and beyond"):
            fast_scoring(_stream(), frame_count=6)


# ---------------------------------------------------------------------------
# ChunkingPhase — sidecar lifecycle
# ---------------------------------------------------------------------------

class TestChunkingPhaseLifecycle:
    def test_execute_scores_splits_and_persists_scenes(self, tmp_path):
        """A fresh run scores (fast provider), splits at the trace's cut
        spike, writes chunking.yaml (PTS-anchored boundaries with frames),
        and emits the derived windows."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())

        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert len(result.chunks) == 2
        assert all(a.state == ArtifactState.COMPLETE for a in result.chunks)
        assert sum(a.payload.frame_count for a in result.chunks) == 640

        sidecar = load_model(work_dir / "chunking.yaml", ChunkingSidecar)
        assert sidecar is not None
        assert sidecar.mode == _APP_CONFIG.chunking.mode
        assert sidecar.min_scene_length == _APP_CONFIG.chunking.min_scene_length
        assert [record.frame for record in sidecar.scenes] == [0, 320]
        # PTS-anchored: the boundary timestamp is the frame's index PTS.
        assert sidecar.scenes[1].timestamp_seconds == pytest.approx(
            round(320 * _FRAME_PERIOD_MS) / 1000,
        )

    def test_execute_uses_slow_provider_extra_traces(self, tmp_path):
        """Slow mode: the codec feature traces add boundaries the content
        trace alone would not find."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)

        collector = NoOpAppMetricsCollector()
        config = _APP_CONFIG.model_copy(deep=True)
        config.chunking.mode = "slow"
        registry = _make_registry(work_dir, _stream(), config=config)
        phase = ChunkingPhase(config, registry, collector=collector)

        with patch("pyqenc.phases.chunking.slow_scoring", return_value=ScoringResult(
            traces=[_step_trace(), _extra_step_trace()],
        )):
            result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert [a.payload.start_timestamp for a in result.chunks] == [
            0.0,
            pytest.approx(round(320 * _FRAME_PERIOD_MS) / 1000),
            pytest.approx(round(480 * _FRAME_PERIOD_MS) / 1000),
        ]

    def test_reuse_loads_scenes_without_rescoring(self, tmp_path):
        """Bug prevented: re-running scoring on every run — cached boundaries
        must be loaded and the phase return REUSED."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())

        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            phase.run(dry_run=False)

        phase2 = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.fast_scoring") as scoring_mock:
            result = phase2.run(dry_run=False)

        scoring_mock.assert_not_called()
        assert result.outcome == PhaseOutcome.REUSED
        assert len(result.chunks) == 2

    def test_no_sidecar_is_pending(self, tmp_path):
        """No chunking.yaml → the phase is pending (scoring must run)."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            result = phase.run(dry_run=True)
        assert result.outcome == PhaseOutcome.PENDING
        assert not (work_dir / "chunking.yaml").exists()

    def test_zero_boundaries_treat_video_as_one_chunk(self, tmp_path):
        """When the splitter yields no interior boundaries (a uniform trace),
        the entire video is one chunk — a single boundary at frame 0."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())

        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[[5.0] * 640],
        )):
            result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.COMPLETED
        assert len(result.chunks) == 1
        assert result.chunks[0].payload.frame_count == 640

    @pytest.mark.parametrize("trace", [
        _step_trace()[:600],           # the decoder stopped early
        _step_trace() + [0.0, 0.0],    # a trace longer than the source has frames
    ], ids=["shorter-than-index", "longer-than-index"])
    def test_trace_length_mismatch_fails_loudly(self, tmp_path, trace):
        """Bug prevented: boundaries anchored to the wrong frames — the
        scoring trace and the PTS index are matched up by frame index, so
        a trace with another frame count means frames are missing (the
        tail silently joining the last chunk) or extra (every score after
        the gap belonging to a different frame). The phase must fail;
        it must never trim the difference and carry on."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())

        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[trace],
        )):
            result = phase.run(dry_run=False)

        assert result.outcome == PhaseOutcome.FAILED
        assert "PTS index (640 frames)" in result.message
        assert f"trace lengths are [{len(trace)}]" in result.message
        assert not (work_dir / "chunking.yaml").exists()

    def test_missing_pts_index_fails_loudly(self, tmp_path):
        """Without the extraction PTS index boundaries cannot be anchored —
        a typed FAILED result, never grid-time guessing."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        phase = _make_phase(work_dir, _stream())  # no timestamps.txt written

        with patch("pyqenc.phases.chunking.fast_scoring") as scoring_mock:
            result = phase.run(dry_run=False)

        scoring_mock.assert_not_called()
        assert result.outcome == PhaseOutcome.FAILED
        assert "PTS index" in result.message

    def test_identity_mismatch_without_permission_fatals(self, tmp_path):
        """C-2: boundaries of the OLD source's timeline are never current —
        an identity mismatch is catastrophic: fatal without --force; with
        the permission the sidecar is discarded and detection re-runs.
        Legacy pre-spec ``chunks/`` leftovers are nobody's concern
        (no-migration policy) and are left in place."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        legacy = work_dir / "chunks"
        legacy.mkdir()
        (legacy / "old.mkv").write_bytes(b"x")

        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            phase.run(dry_run=False)
        assert (work_dir / "chunking.yaml").exists()

        # Corrupt the recorded identity key (a different source).
        sidecar_path = work_dir / "chunking.yaml"
        data = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        data["source"] = str(SizedFingerprint(token="f" * 32, size=_STUB_SOURCE_FP.size))
        write_yaml_atomic(sidecar_path, data)

        # Without permission: fatal.
        denied = _make_phase(work_dir, _stream())
        result = denied.run(dry_run=False)
        assert result.outcome == PhaseOutcome.FAILED
        assert "identity mismatch" in result.message.lower()
        assert "--force" in result.message

        # With permission: discard + re-detect.
        registry = _make_registry(work_dir, _stream(), force=True)
        collector = NoOpAppMetricsCollector()
        config = _APP_CONFIG.model_copy(deep=True)
        phase2 = ChunkingPhase(config, registry, collector=collector)
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )) as scoring_mock:
            phase2.run(dry_run=False)

        assert scoring_mock.called, "the identity wipe must invalidate the cached scenes"
        assert legacy.exists(), "legacy leftovers are deliberately untouched"
        assert (work_dir / "chunking.yaml").exists()

    def test_detection_params_change_redictects_automatically(self, tmp_path):
        """C-1: a min-scene-length change discards the persisted boundaries
        — automatic re-detect (user-initiated config edit, nothing deleted)."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            phase.run(dry_run=False)
        assert (work_dir / "chunking.yaml").exists()

        config = _APP_CONFIG.model_copy(deep=True)
        config.chunking.min_scene_length = 30
        registry = _make_registry(work_dir, _stream(), config=config)
        phase2 = ChunkingPhase(config, registry, collector=NoOpAppMetricsCollector())
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )) as scoring_mock:
            phase2.run(dry_run=False)

        assert scoring_mock.called, "a detection-params change must re-detect"

        sidecar = load_model(work_dir / "chunking.yaml", ChunkingSidecar)
        assert sidecar is not None
        assert sidecar.min_scene_length == 30

    def test_mode_change_redictetects_automatically(self, tmp_path):
        """C-1: a mode change (fast → slow) re-detects under the new regime."""
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True)
        _write_pts_index(work_dir)
        phase = _make_phase(work_dir, _stream())
        with patch("pyqenc.phases.chunking.fast_scoring", return_value=ScoringResult(
            traces=[_step_trace()],
        )):
            phase.run(dry_run=False)

        config = _APP_CONFIG.model_copy(deep=True)
        config.chunking.mode = "slow"
        registry = _make_registry(work_dir, _stream(), config=config)
        phase2 = ChunkingPhase(config, registry, collector=NoOpAppMetricsCollector())
        with patch("pyqenc.phases.chunking.slow_scoring", return_value=ScoringResult(
            traces=[_step_trace(), _extra_step_trace()],
        )) as slow_mock:
            result = phase2.run(dry_run=False)

        assert slow_mock.called, "a mode change must re-detect via the new provider"
        assert result.outcome == PhaseOutcome.COMPLETED
        assert len(result.chunks) == 3
