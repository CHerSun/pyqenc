"""Integration tests for artifact-based resumption."""

from pathlib import Path

import pytest

from pyqenc.models import (
    AttemptInfo,
    ChunkState,
    PhaseMetadata,
    PhaseState,
    PhaseStatus,
    PipelineState,
    StrategyState,
    VideoMetadata,
)
from pyqenc.progress import ProgressTracker


class TestArtifactBasedResumption:
    """Tests for artifact-based resumption logic."""

    def test_resume_from_interrupted_encoding(self, tmp_path):
        """Test resuming encoding after interruption."""
        tracker = ProgressTracker(tmp_path)

        # Simulate interrupted encoding state
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path="test.mkv"),
            current_phase="encoding",
            phases={
                "extraction": PhaseState(
                    status=PhaseStatus.COMPLETED,
                    timestamp="2026-02-23T10:00:00"
                ),
                "chunking": PhaseState(
                    status=PhaseStatus.COMPLETED,
                    timestamp="2026-02-23T10:15:00"
                ),
                "encoding": PhaseState(
                    status=PhaseStatus.IN_PROGRESS,
                    timestamp="2026-02-23T10:30:00"
                )
            },
            chunks={
                "chunk_001": ChunkState(
                    chunk_id="chunk_001",
                    strategies={
                        "slow+h265-aq": StrategyState(
                            status=PhaseStatus.COMPLETED,
                            attempts=[
                                AttemptInfo(
                                    attempt_number=1,
                                    crf=20.0,
                                    metrics={"vmaf_min": 95.5},
                                    success=True,
                                    file_path=Path("chunk_001.mkv"),
                                    file_size=1024000
                                )
                            ],
                            final_crf=20.0
                        )
                    }
                ),
                "chunk_002": ChunkState(
                    chunk_id="chunk_002",
                    strategies={
                        "slow+h265-aq": StrategyState(
                            status=PhaseStatus.IN_PROGRESS,
                            attempts=[
                                AttemptInfo(
                                    attempt_number=1,
                                    crf=20.0,
                                    metrics={"vmaf_min": 93.0},
                                    success=False
                                )
                            ],
                            final_crf=None
                        )
                    }
                )
            }
        )

        tracker.save_state(state)

        # Load state and verify resumption point
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        assert loaded_state.current_phase == "encoding"

        # Chunk 001 is complete
        chunk_001 = loaded_state.chunks["chunk_001"]
        assert chunk_001.strategies["slow+h265-aq"].status == PhaseStatus.COMPLETED
        assert chunk_001.strategies["slow+h265-aq"].final_crf == 20.0

        # Chunk 002 needs more work
        chunk_002 = loaded_state.chunks["chunk_002"]
        assert chunk_002.strategies["slow+h265-aq"].status == PhaseStatus.IN_PROGRESS
        assert chunk_002.strategies["slow+h265-aq"].final_crf is None
        assert len(chunk_002.strategies["slow+h265-aq"].attempts) == 1

    def test_configuration_change_detection(self, tmp_path):
        """Test detecting configuration changes (new strategies)."""
        tracker = ProgressTracker(tmp_path)

        # Initial state with one strategy
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path="test.mkv"),
            current_phase="encoding",
            phases={
                "encoding": PhaseState(
                    status=PhaseStatus.COMPLETED,
                    timestamp="2026-02-23T10:00:00"
                )
            },
            chunks={
                "chunk_001": ChunkState(
                    chunk_id="chunk_001",
                    strategies={
                        "slow+h265-aq": StrategyState(
                            status=PhaseStatus.COMPLETED,
                            attempts=[],
                            final_crf=20.0
                        )
                    }
                )
            }
        )

        tracker.save_state(state)

        # Simulate adding new strategy
        # In real pipeline, this would be detected by checking if chunk has
        # encoding for all requested strategies
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        chunk = loaded_state.chunks["chunk_001"]

        # Check if new strategy exists
        new_strategy = "veryslow+h265-anime"
        has_new_strategy = new_strategy in chunk.strategies

        assert not has_new_strategy  # New strategy not yet encoded

        # Add new strategy encoding
        tracker.update_chunk(
            "chunk_001",
            new_strategy,
            AttemptInfo(
                attempt_number=1,
                crf=20.0,
                metrics={"vmaf_min": 96.0},
                success=True,
                file_path=Path("chunk_001_new.mkv"),
                file_size=1100000
            )
        )

        # Verify new strategy added
        updated_state = tracker.load_state()
        assert updated_state is not None
        chunk = updated_state.chunks.get("chunk_001")

        assert chunk is not None
        assert new_strategy in chunk.strategies
        assert chunk.strategies[new_strategy].status == PhaseStatus.COMPLETED

    def test_quality_target_change_detection(self, tmp_path):
        """Test detecting quality target changes requiring re-encoding."""
        tracker = ProgressTracker(tmp_path)

        # State with encoding that met old targets
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path="test.mkv"),
            current_phase="encoding",
            phases={
                "encoding": PhaseState(
                    status=PhaseStatus.COMPLETED,
                    timestamp="2026-02-23T10:00:00",
                    metadata=PhaseMetadata(),
                )
            },
            chunks={
                "chunk_001": ChunkState(
                    chunk_id="chunk_001",
                    strategies={
                        "slow+h265-aq": StrategyState(
                            status=PhaseStatus.COMPLETED,
                            attempts=[
                                AttemptInfo(
                                    attempt_number=1,
                                    crf=22.0,
                                    metrics={"vmaf_min": 93.5},  # Met old target
                                    success=True,
                                    file_path=Path("chunk_001.mkv"),
                                    file_size=900000
                                )
                            ],
                            final_crf=22.0
                        )
                    }
                )
            }
        )

        tracker.save_state(state)

        # Simulate new higher quality target
        new_target_value = 95.0

        # Check if existing encoding meets new target
        loaded_state = tracker.load_state()
        assert loaded_state is not None
        chunk = loaded_state.chunks["chunk_001"]
        strategy_state = chunk.strategies["slow+h265-aq"]

        # Get last successful attempt metrics
        last_attempt = strategy_state.attempts[-1]
        actual_vmaf = last_attempt.metrics.get("vmaf_min", 0)

        meets_new_target = actual_vmaf >= new_target_value

        assert not meets_new_target  # 93.5 < 95.0, needs re-encoding

    def test_phase_completion_check(self, tmp_path):
        """Test checking if phase is fully complete."""
        tracker = ProgressTracker(tmp_path)

        # Create state with multiple chunks
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path="test.mkv"),
            current_phase="encoding",
            phases={
                "encoding": PhaseState(
                    status=PhaseStatus.IN_PROGRESS,
                    timestamp="2026-02-23T10:00:00"
                )
            },
            chunks={
                f"chunk_{i:03d}": ChunkState(
                    chunk_id=f"chunk_{i:03d}",
                    strategies={
                        "slow+h265-aq": StrategyState(
                            status=PhaseStatus.COMPLETED if i < 5 else PhaseStatus.IN_PROGRESS,
                            attempts=[],
                            final_crf=20.0 if i < 5 else None
                        )
                    }
                )
                for i in range(10)
            }
        )

        tracker.save_state(state)

        # Check phase completion
        loaded_state = tracker.load_state()
        assert loaded_state is not None

        # Count completed chunks
        total_chunks = len(loaded_state.chunks)
        completed_chunks = sum(
            1 for chunk in loaded_state.chunks.values()
            if all(
                strategy.status == PhaseStatus.COMPLETED
                for strategy in chunk.strategies.values()
            )
        )

        phase_complete = completed_chunks == total_chunks

        assert not phase_complete  # Only 5/10 chunks complete
        assert completed_chunks == 5
        assert total_chunks == 10
