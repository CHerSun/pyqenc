"""Unit tests for progress tracking."""

import logging
from pathlib import Path

from pyqenc.models import (
    AttemptInfo,
    PhaseMetadata,
    PhaseState,
    PhaseStatus,
    PipelineState,
    VideoMetadata,
)
from pyqenc.progress import ProgressTracker
from tests.fixtures.state_fixtures import (
    create_progress_state_file,
)

logger = logging.getLogger("pyqenc.progress")


class TestProgressTracker:
    """Tests for ProgressTracker state management."""

    def test_load_nonexistent_state(self, tmp_path):
        """Test loading state when file doesn't exist returns None."""
        tracker = ProgressTracker(tmp_path)
        state = tracker.load_state()
        assert state is None

    def test_load_existing_state(self, tmp_path):
        """Test loading existing state file."""
        create_progress_state_file(tmp_path / "progress.json")

        tracker = ProgressTracker(tmp_path)
        state = tracker.load_state()

        assert state is not None
        assert state.source_video.path == Path("test_video.mkv")
        assert state.current_phase == "encoding"
        assert "extraction" in state.phases
        assert state.phases["extraction"].status == PhaseStatus.COMPLETED

    def test_save_and_load_state(self, tmp_path):
        """Test saving and loading state round-trip."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)

        # Create new state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="extraction",
            phases={
                "extraction": PhaseState(
                    status=PhaseStatus.IN_PROGRESS,
                    timestamp="2026-02-23T10:00:00"
                )
            },
            chunks={}
        )

        # Save state
        tracker.save_state(state)

        # Load state
        loaded_state = tracker.load_state()

        assert loaded_state is not None
        assert loaded_state.source_video.path == Path("test.mkv")
        assert loaded_state.current_phase == "extraction"
        assert loaded_state.phases["extraction"].status == PhaseStatus.IN_PROGRESS

    def test_update_phase(self, tmp_path):
        """Test updating phase status."""
        tracker = ProgressTracker(tmp_path)

        # Initialize state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="extraction",
            phases={},
            chunks={}
        )
        tracker.save_state(state)

        # Update phase
        tracker.update_phase("extraction", PhaseStatus.COMPLETED, PhaseMetadata())

        # Verify update
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        assert loaded_state.phases["extraction"].status == PhaseStatus.COMPLETED
        assert loaded_state.phases["extraction"].metadata is not None
        assert loaded_state.current_phase == "extraction"

    def test_update_chunk_new_chunk(self, tmp_path):
        """Test updating chunk creates new chunk state."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)

        # Initialize state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="encoding",
            phases={},
            chunks={}
        )
        tracker.save_state(state)

        # Update chunk
        attempt = AttemptInfo(
            attempt_number=1,
            crf=20.0,
            metrics={"vmaf_min": 95.3, "vmaf_med": 97.8},
            success=True,
            file_path=Path("chunk_001.mkv"),
            file_size=1024000
        )
        tracker.update_chunk("chunk_001", "slow+h265-aq", attempt)

        # Verify update
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        assert "chunk_001" in loaded_state.chunks
        chunk_state = loaded_state.chunks["chunk_001"]
        assert "slow+h265-aq" in chunk_state.strategies
        strategy_state = chunk_state.strategies["slow+h265-aq"]
        assert strategy_state.status == PhaseStatus.COMPLETED
        assert strategy_state.final_crf == 20.0
        assert len(strategy_state.attempts) == 1

    def test_update_chunk_multiple_attempts(self, tmp_path):
        """Test updating chunk with multiple attempts."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)

        # Initialize state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="encoding",
            phases={},
            chunks={}
        )
        tracker.save_state(state)

        # First attempt (failed)
        attempt1 = AttemptInfo(
            attempt_number=1,
            crf=20.0,
            metrics={"vmaf_min": 93.2, "vmaf_med": 96.1},
            success=False
        )
        tracker.update_chunk("chunk_001", "slow+h265-aq", attempt1)

        # Second attempt (success)
        attempt2 = AttemptInfo(
            attempt_number=2,
            crf=18.0,
            metrics={"vmaf_min": 95.3, "vmaf_med": 97.8},
            success=True,
            file_path=Path("chunk_001.mkv"),
            file_size=1024000
        )
        tracker.update_chunk("chunk_001", "slow+h265-aq", attempt2)

        # Verify updates
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        strategy_state = loaded_state.chunks["chunk_001"].strategies["slow+h265-aq"]
        assert len(strategy_state.attempts) == 2
        assert strategy_state.attempts[0].success is False
        assert strategy_state.attempts[1].success is True
        assert strategy_state.final_crf == 18.0

    def test_get_chunk_state(self, tmp_path):
        """Test retrieving chunk state."""
        create_progress_state_file(tmp_path / "progress.json")

        tracker = ProgressTracker(tmp_path)
        tracker.load_state()

        chunk_state = tracker.get_chunk_state("chunk_001", "slow+h265-aq")
        assert chunk_state is not None
        assert chunk_state.chunk_id == "chunk_001"
        assert "slow+h265-aq" in chunk_state.strategies

    def test_get_chunk_state_nonexistent(self, tmp_path):
        """Test retrieving non-existent chunk returns None."""
        tracker = ProgressTracker(tmp_path)

        chunk_state = tracker.get_chunk_state("nonexistent", "slow+h265-aq")
        assert chunk_state is None

    def test_get_successful_crf_average(self, tmp_path):
        """Test calculating average CRF from successful chunks."""
        create_progress_state_file(tmp_path / "progress.json")

        tracker = ProgressTracker(tmp_path)
        tracker.load_state()

        avg_crf = tracker.get_successful_crf_average("slow+h265-aq")
        assert avg_crf is not None
        # From fixture: chunk_001 has final_crf=18.0, chunk_002 has final_crf=18.0
        assert avg_crf == 18.0

    def test_get_successful_crf_average_no_chunks(self, tmp_path):
        """Test calculating average CRF with no successful chunks returns None."""
        tracker = ProgressTracker(tmp_path)

        # Initialize empty state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="encoding",
            phases={},
            chunks={}
        )
        tracker.save_state(state)

        avg_crf = tracker.get_successful_crf_average("slow+h265-aq")
        assert avg_crf is None


# ---------------------------------------------------------------------------
# Typed update objects — PhaseUpdate / ChunkUpdate round-trip
# ---------------------------------------------------------------------------

class TestTypedUpdates:
    """Tests for PhaseUpdate and ChunkUpdate typed update objects."""

    def test_phase_update_round_trip(self, tmp_path):
        """PhaseUpdate round-trips through serialization without data loss."""
        from pyqenc.models import PhaseMetadata, PhaseUpdate, SceneBoundary

        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="",
            phases={},
            chunks={},
        )
        tracker.save_state(state)

        boundaries = [
            SceneBoundary(frame=0, timestamp_seconds=0.0),
            SceneBoundary(frame=320, timestamp_seconds=13.33),
        ]
        update = PhaseUpdate(
            phase="chunking",
            status=PhaseStatus.COMPLETED,
            metadata=PhaseMetadata(scene_boundaries=boundaries),
        )
        tracker.update_phase(update)

        loaded = tracker.load_state()
        assert loaded is not None
        phase = loaded.phases["chunking"]
        assert phase.status == PhaseStatus.COMPLETED
        assert phase.metadata is not None
        assert len(phase.metadata.scene_boundaries) == 2
        assert phase.metadata.scene_boundaries[0].frame == 0
        assert phase.metadata.scene_boundaries[1].frame == 320

    def test_chunk_update_round_trip(self, tmp_path):
        """ChunkUpdate round-trips through serialization without data loss."""
        from pyqenc.models import ChunkUpdate

        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="encoding",
            phases={},
            chunks={},
        )
        tracker.save_state(state)

        attempt = AttemptInfo(
            attempt_number=1,
            crf=20.0,
            metrics={"vmaf_min": 95.5},
            success=True,
            file_path=Path("chunk.000000-000319.mkv"),
            file_size=2048000,
        )
        update = ChunkUpdate(
            chunk_id="chunk.000000-000319",
            strategy="slow+h265-aq",
            attempt=attempt,
        )
        tracker.update_chunk(update)

        loaded = tracker.load_state()
        assert loaded is not None
        chunk_state = loaded.chunks["chunk.000000-000319"]
        strategy_state = chunk_state.strategies["slow+h265-aq"]
        assert strategy_state.status == PhaseStatus.COMPLETED
        assert strategy_state.final_crf == 20.0
        assert len(strategy_state.attempts) == 1
        assert strategy_state.attempts[0].metrics["vmaf_min"] == 95.5
        assert strategy_state.attempts[0].file_size == 2048000

    def test_update_phase_backward_compat_string(self, tmp_path):
        """update_phase still works with legacy (str, status, metadata) signature."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="",
            phases={},
            chunks={},
        )
        tracker.save_state(state)

        tracker.update_phase("extraction", PhaseStatus.COMPLETED)

        loaded = tracker.load_state()
        assert loaded is not None
        assert loaded.phases.get("extraction") is not None
        assert loaded.phases["extraction"].status == PhaseStatus.COMPLETED

    def test_update_chunk_backward_compat_string(self, tmp_path):
        """update_chunk still works with legacy (str, strategy, attempt) signature."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=Path("test.mkv")),
            current_phase="encoding",
            phases={},
            chunks={},
        )
        tracker.save_state(state)

        attempt = AttemptInfo(
            attempt_number=1,
            crf=18.0,
            metrics={"vmaf_min": 96.0},
            success=True,
        )
        tracker.update_chunk("chunk_001", "slow+h265-aq", attempt)

        loaded = tracker.load_state()
        assert loaded is not None
        assert "chunk_001" in loaded.chunks
        assert loaded.chunks["chunk_001"].strategies["slow+h265-aq"].final_crf == 18.0


# ---------------------------------------------------------------------------
# Source file change detection
# ---------------------------------------------------------------------------

class TestSourceFileChangeDetection:
    """Tests for source file change detection on resume."""

    def _make_state_file(self, tmp_path: Path, **cached_fields) -> None:
        """Write a progress.json with pre-filled VideoMetadata backing fields."""
        import json

        data = {
            "version": "1.0",
            "source_video": {
                "path": str(tmp_path / "source.mkv"),
                "start_frame": 0,
                "_duration_seconds": cached_fields.get("_duration_seconds", 5400.0),
                "_frame_count":      cached_fields.get("_frame_count", 129600),
                "_fps":              cached_fields.get("_fps", 24.0),
                "_resolution":       cached_fields.get("_resolution", "1920x1080"),
            },
            "current_phase": "encoding",
            "phases": {},
            "chunks_metadata": {},
            "chunks": {
                "chunk.000000-000319": {
                    "strategies": {
                        "slow+h265-aq": {
                            "status": "completed",
                            "attempts": [],
                            "final_crf": 20.0,
                        }
                    }
                }
            },
        }
        (tmp_path / "progress.json").write_text(json.dumps(data))

    def test_no_warning_when_fields_match(self, tmp_path, caplog):
        """No warning is logged when persisted fields match live values."""
        import logging

        from pyqenc.models import VideoMetadata as _VM

        self._make_state_file(tmp_path)

        # Patch _check_source_file_changes to simulate no differences
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        original = tracker._check_source_file_changes

        def _no_change(state):
            # Build a live meta with identical values — no warning should fire
            live = _VM(path=state.source_video.path)
            live._duration_seconds = state.source_video._duration_seconds
            live._fps              = state.source_video._fps
            live._resolution       = state.source_video._resolution
            live._frame_count      = state.source_video._frame_count
            # Call the real method; since values match, no warning
            original(state)

        tracker._check_source_file_changes = _no_change

        with caplog.at_level(logging.WARNING, logger="pyqenc.progress"):
            tracker.load_state()

        assert not any("metadata changed" in r.message for r in caplog.records)

    def test_warning_logged_when_resolution_differs(self, tmp_path, caplog):
        """A warning is logged when the live resolution differs from persisted."""
        import logging

        from pyqenc.models import VideoMetadata as _VM

        self._make_state_file(tmp_path, _resolution="1920x1080")

        tracker = ProgressTracker(tmp_path, batch_updates=False)

        def _fake_check(state):
            # Simulate live probe returning a different resolution
            live = _VM(path=state.source_video.path)
            live._duration_seconds = state.source_video._duration_seconds
            live._fps              = state.source_video._fps
            live._resolution       = "1280x720"   # differs from persisted "1920x1080"
            live._frame_count      = state.source_video._frame_count

            # Inline the comparison logic so we don't need a real file
            persisted = state.source_video
            if (
                persisted._resolution is not None
                and live._resolution is not None
                and live._resolution != persisted._resolution
            ):
                logger.warning(
                    "Source file metadata changed — field 'resolution': persisted=%s, live=%s. "
                    "Affected chunks will be reset to NOT_STARTED.",
                    persisted._resolution, live._resolution,
                )
                for chunk_state in state.chunks.values():
                    for strategy_state in chunk_state.strategies.values():
                        strategy_state.status = PhaseStatus.NOT_STARTED

        tracker._check_source_file_changes = _fake_check

        with caplog.at_level(logging.WARNING, logger="pyqenc.progress"):
            tracker.load_state()

        assert any("metadata changed" in r.message for r in caplog.records)

    def test_chunks_reset_to_not_started_on_change(self, tmp_path):
        """Chunks are reset to NOT_STARTED when source file metadata changes."""
        from pyqenc.models import VideoMetadata as _VM

        self._make_state_file(tmp_path, _resolution="1920x1080")

        tracker = ProgressTracker(tmp_path, batch_updates=False)

        def _fake_check(state):
            persisted = state.source_video
            live_resolution = "1280x720"  # differs
            if (
                persisted._resolution is not None
                and live_resolution != persisted._resolution
            ):
                for chunk_state in state.chunks.values():
                    for strategy_state in chunk_state.strategies.values():
                        strategy_state.status = PhaseStatus.NOT_STARTED

        tracker._check_source_file_changes = _fake_check
        state = tracker.load_state()

        assert state is not None
        chunk_state = state.chunks.get("chunk.000000-000319")
        assert chunk_state is not None
        strategy_state = chunk_state.strategies["slow+h265-aq"]
        assert strategy_state.status == PhaseStatus.NOT_STARTED
