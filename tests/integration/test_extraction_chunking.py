"""Integration tests for extraction → chunking pipeline."""

import json
import subprocess
from pathlib import Path

import pytest
from pyqenc.models import ChunkingMode, CropParams
from pyqenc.phases.chunking import chunk_video
from pyqenc.phases.extraction import extract_streams

from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


def _ffprobe_frame_count(video_file: Path) -> int:
    """Return the frame count of *video_file* via ffprobe null-encode."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "json",
        str(video_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError(f"ffprobe returned no streams for {video_file}")
    return int(streams[0]["nb_read_packets"])


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestExtractionChunkingIntegration:
    """Integration tests for extraction and chunking phases."""

    def test_extraction_to_chunking_flow(self, tmp_path):
        """Test complete flow from extraction to chunking."""
        source_video = get_sample_video_path()
        extract_dir = tmp_path / "extracted"
        chunks_dir = tmp_path / "chunks"

        # Phase 1: Extract streams
        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=True,
            manual_crop=None,
            force=False,
            dry_run=False
        )

        assert extract_result.success
        assert len(extract_result.video_files) > 0
        video_file = extract_result.video_files[0]
        assert video_file.exists()

        # Phase 2: Chunk video
        chunk_result = chunk_video(
            video_file=video_file,
            output_dir=chunks_dir,
            chunking_mode=ChunkingMode.REMUX,
            scene_threshold=0.3,
            min_scene_length=24,
            force=False
        )

        assert chunk_result.success
        assert len(chunk_result.chunks) > 0
        assert chunk_result.total_frames > 0

        # Verify chunks exist
        for chunk_info in chunk_result.chunks:
            assert chunk_info.file_path.exists()
            assert chunk_info.frame_count > 0

    def test_extraction_with_manual_crop(self, tmp_path):
        """Test extraction with manual crop parameters."""
        source_video = get_sample_video_path()
        extract_dir = tmp_path / "extracted"

        manual_crop = "140 140 0 0"

        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=manual_crop,
            force=False,
            dry_run=False
        )

        assert extract_result.success
        assert extract_result.crop_params is not None
        assert extract_result.crop_params.top == 140
        assert extract_result.crop_params.bottom == 140

    def test_extraction_reuse_existing(self, tmp_path):
        """Test extraction reuses existing files."""
        source_video = get_sample_video_path()
        extract_dir = tmp_path / "extracted"

        # First extraction
        result1 = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=False
        )

        assert result1.success
        assert not result1.reused

        # Second extraction (should reuse)
        result2 = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=False
        )

        assert result2.success
        assert result2.reused

    def test_chunking_reuse_existing(self, tmp_path):
        """Test chunking reuses existing chunks."""
        source_video = get_sample_video_path()
        extract_dir = tmp_path / "extracted"
        chunks_dir = tmp_path / "chunks"

        # Extract first
        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=False
        )

        video_file = extract_result.video_files[0]

        # First chunking
        result1 = chunk_video(
            video_file=video_file,
            output_dir=chunks_dir,
            chunking_mode=ChunkingMode.REMUX,
            scene_threshold=0.3,
            min_scene_length=24,
            force=False
        )

        assert result1.success
        assert not result1.reused

        # Second chunking (should reuse)
        result2 = chunk_video(
            video_file=video_file,
            output_dir=chunks_dir,
            chunking_mode=ChunkingMode.REMUX,
            scene_threshold=0.3,
            min_scene_length=24,
            force=False
        )

        assert result2.success
        assert result2.reused

    def test_dry_run_mode(self, tmp_path):
        """Test dry-run mode for extraction and chunking."""
        source_video = get_sample_video_path()
        extract_dir = tmp_path / "extracted"

        # Dry-run extraction
        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=True
        )

        # Should report needs work but not create files
        assert extract_result.needs_work
        assert not extract_dir.exists() or not any(extract_dir.iterdir())


@pytest.mark.slow
@pytest.mark.requires_ffmpeg
@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestFFV1ChunkingIntegration:
    """Integration tests for FFV1 lossless chunking mode."""

    def test_ffv1_chunks_exist_and_are_nonempty(self, tmp_path: Path) -> None:
        """FFV1 lossless chunking produces non-empty chunk files on disk."""
        source_video = get_sample_video_path()
        extract_dir  = tmp_path / "extracted"
        chunks_dir   = tmp_path / "chunks"

        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=False,
        )
        assert extract_result.success, f"Extraction failed: {extract_result}"
        assert extract_result.video_files, "No video files extracted"

        video_file = extract_result.video_files[0]

        chunk_result = chunk_video(
            video_file=video_file,
            output_dir=chunks_dir,
            chunking_mode=ChunkingMode.LOSSLESS,
            scene_threshold=0.3,
            min_scene_length=24,
            force=False,
        )

        assert chunk_result.success, f"Chunking failed: {chunk_result.error}"
        assert chunk_result.chunks, "No chunks produced"

        for chunk_info in chunk_result.chunks:
            assert chunk_info.file_path.exists(), f"Chunk missing: {chunk_info.file_path}"
            assert chunk_info.file_path.stat().st_size > 0, (
                f"Chunk is empty: {chunk_info.file_path}"
            )

    def test_ffv1_chunks_have_expected_frame_counts(self, tmp_path: Path) -> None:
        """ffprobe reports a positive frame count for every FFV1 chunk."""
        source_video = get_sample_video_path()
        extract_dir  = tmp_path / "extracted"
        chunks_dir   = tmp_path / "chunks"

        extract_result = extract_streams(
            source_video=source_video,
            output_dir=extract_dir,
            include=None,
            exclude=None,
            detect_crop=False,
            manual_crop=None,
            force=False,
            dry_run=False,
        )
        assert extract_result.success
        video_file = extract_result.video_files[0]

        chunk_result = chunk_video(
            video_file=video_file,
            output_dir=chunks_dir,
            chunking_mode=ChunkingMode.LOSSLESS,
            scene_threshold=0.3,
            min_scene_length=24,
            force=False,
        )

        assert chunk_result.success
        assert chunk_result.chunks

        for chunk_info in chunk_result.chunks:
            probed_frames = _ffprobe_frame_count(chunk_info.file_path)
            assert probed_frames > 0, (
                f"ffprobe reported 0 frames for chunk {chunk_info.file_path.name}"
            )
            assert probed_frames == chunk_info.frame_count, (
                f"Frame count mismatch for {chunk_info.file_path.name}: "
                f"expected {chunk_info.frame_count}, ffprobe reports {probed_frames}"
            )
