"""
FFmpeg wrapper utilities for the quality-based encoding pipeline.

This module provides helper functions for common FFmpeg operations including
scene detection, video segmentation, frame counting, and crop detection.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyqenc.models import CropParams

logger = logging.getLogger(__name__)


class FFmpegError(Exception):
    """Base exception for FFmpeg-related errors."""
    pass


class SceneDetectionError(FFmpegError):
    """Error during scene detection."""
    pass


class SegmentationError(FFmpegError):
    """Error during video segmentation."""
    pass


class FrameCountError(FFmpegError):
    """Error during frame count verification."""
    pass


@dataclass
class SceneChange:
    """Scene change detection result.

    Attributes:
        frame: Frame number where scene change occurs
        timestamp: Timestamp in seconds
        score: Scene change score (0.0-1.0)
    """

    frame: int
    timestamp: float
    score: float


def detect_scenes(
    video_file: Path,
    threshold: float = 0.3,
    min_scene_length: int = 24
) -> list[SceneChange]:
    """Detect scene changes in video using ffmpeg scenedetect filter.

    Args:
        video_file: Path to video file to analyze
        threshold: Scene detection sensitivity (0.0-1.0, lower = more sensitive)
        min_scene_length: Minimum frames between scene changes

    Returns:
        List of SceneChange objects representing detected scene boundaries

    Raises:
        SceneDetectionError: If scene detection fails
    """
    logger.debug(f"Detecting scenes in {video_file.name} (threshold={threshold})")

    try:
        # Use ffmpeg with scenedetect filter
        cmd = [
            "ffmpeg",
            "-i", str(video_file),
            "-vf", f"scenedetect=t={threshold}:s=1",
            "-f", "null",
            "-"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=300  # 5 minute timeout for scene detection
        )

        # Parse scene changes from stderr
        # Looking for lines like: [Parsed_scenedetect_0 @ ...] scene_score=0.456 frame=123 pts=5.12
        scenes = []
        for line in result.stderr.split('\n'):
            if 'scenedetect' in line and 'scene_score=' in line:
                # Extract scene information
                score_match = re.search(r'scene_score=([\d.]+)', line)
                frame_match = re.search(r'frame=(\d+)', line)
                pts_match = re.search(r'pts=([\d.]+)', line)

                if score_match and frame_match and pts_match:
                    score = float(score_match.group(1))
                    frame = int(frame_match.group(1))
                    timestamp = float(pts_match.group(1))

                    scenes.append(SceneChange(
                        frame=frame,
                        timestamp=timestamp,
                        score=score
                    ))

        # Filter scenes by minimum length
        if min_scene_length > 0 and len(scenes) > 1:
            filtered_scenes = [scenes[0]]  # Always keep first scene
            for scene in scenes[1:]:
                if scene.frame - filtered_scenes[-1].frame >= min_scene_length:
                    filtered_scenes.append(scene)
            scenes = filtered_scenes

        logger.debug(f"Detected {len(scenes)} scene changes")
        return scenes

    except subprocess.TimeoutExpired as e:
        raise SceneDetectionError(f"Scene detection timed out after 5 minutes") from e
    except subprocess.CalledProcessError as e:
        raise SceneDetectionError(f"FFmpeg scene detection failed: {e.stderr}") from e
    except Exception as e:
        raise SceneDetectionError(f"Scene detection error: {e}") from e


def segment_video(
    video_file: Path,
    output_dir: Path,
    scene_changes: list[SceneChange],
    crop_params: CropParams | None = None,
    output_pattern: str = "chunk_{:04d}.mkv"
) -> list[Path]:
    """Split video into segments at scene boundaries with frame-perfect accuracy.

    Args:
        video_file: Path to source video file
        output_dir: Directory for output chunks
        scene_changes: List of scene change points
        crop_params: Optional crop parameters to apply during segmentation
        output_pattern: Filename pattern for chunks (must contain one format placeholder)

    Returns:
        List of paths to created chunk files

    Raises:
        SegmentationError: If video segmentation fails
    """
    logger.debug(f"Segmenting video into {len(scene_changes) + 1} chunks")

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Build filter chain
        filters = []
        if crop_params and not crop_params.is_empty():
            filters.append(crop_params.to_ffmpeg_filter())

        filter_str = ",".join(filters) if filters else None

        # Create segment file with timestamps
        segment_file = output_dir / "segments.txt"
        with open(segment_file, 'w') as f:
            # Write segment timestamps
            prev_time = 0.0
            for i, scene in enumerate(scene_changes):
                # Each segment goes from prev_time to scene.timestamp
                f.write(f"file '{output_pattern.format(i)}'\n")
                prev_time = scene.timestamp
            # Last segment
            f.write(f"file '{output_pattern.format(len(scene_changes))}'\n")

        # Use ffmpeg segment muxer for frame-accurate splitting
        # We'll split at each scene change
        chunk_files = []

        # Get video duration and frame rate first
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration,r_frame_rate",
            "-of", "json",
            str(video_file)
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_result.stdout)

        # Split video at each scene boundary
        timestamps = [0.0] + [s.timestamp for s in scene_changes]

        for i in range(len(timestamps)):
            chunk_path = output_dir / output_pattern.format(i)
            chunk_files.append(chunk_path)

            # Determine start and duration
            start_time = timestamps[i]
            if i < len(timestamps) - 1:
                duration = timestamps[i + 1] - start_time
                duration_args = ["-t", str(duration)]
            else:
                # Last chunk goes to end
                duration_args = []

            # Build ffmpeg command for this chunk
            cmd = ["ffmpeg"]

            # Only add -ss if not starting from beginning
            if start_time > 0:
                cmd.extend(["-ss", str(start_time)])

            cmd.extend([
                "-fflags", "+genpts",  # Generate presentation timestamps
                "-i", str(video_file),
                *duration_args,
                "-map", "0:v:0",  # Only video stream
                "-c:v", "copy" if filter_str is None else "libx264",  # Copy if no filters
            ])

            if filter_str:
                cmd.extend(["-vf", filter_str])
                cmd.extend(["-preset", "ultrafast"])  # Fast encoding for chunking

            cmd.extend([
                "-avoid_negative_ts", "make_zero",
                "-y",  # Overwrite
                str(chunk_path)
            ])

            logger.debug(f"Creating chunk {i}: {chunk_path.name}")
            logger.debug(f"FFmpeg command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=600  # 10 minute timeout per chunk
            )
            logger.debug(f"Chunk {i} created successfully")

        # Clean up segment file
        segment_file.unlink(missing_ok=True)

        logger.debug(f"Created {len(chunk_files)} chunks")
        return chunk_files

    except subprocess.TimeoutExpired as e:
        raise SegmentationError(f"Video segmentation timed out") from e
    except subprocess.CalledProcessError as e:
        raise SegmentationError(f"FFmpeg segmentation failed: {e.stderr}") from e
    except Exception as e:
        raise SegmentationError(f"Video segmentation error: {e}") from e


def get_frame_count(
    video_file: Path,
    tracker:   "ProgressTracker | None" = None,
    is_source: bool = False,
    video_meta: "VideoMetadata | None" = None,
) -> int:
    """Get total frame count of video file with caching support.

    Uses ffmpeg with null output and progress tracking for fast, reliable frame
    counting.  Results are cached in the progress tracker if provided.

    After the ffprobe run the parsed JSON is passed to
    ``video_meta.populate_from_ffprobe()`` (if a ``VideoMetadata`` instance is
    supplied) so that duration, fps, and resolution are opportunistically filled
    without a second probe call.

    Args:
        video_file: Path to video file.
        tracker:    Optional progress tracker for caching.
        is_source:  If True, this is the source video (cache in source_video metadata).
        video_meta: Optional VideoMetadata instance to populate opportunistically.

    Returns:
        Total number of frames in video.

    Raises:
        FrameCountError: If frame count cannot be determined.
    """
    from pyqenc.models import VideoMetadata as _VideoMetadata
    from pyqenc.utils.ffmpeg_wrapper import run_ffmpeg_with_progress

    # Check cache first
    if tracker and is_source:
        meta = tracker.get_source_metadata()
        if meta and meta.frame_count is not None:
            logger.debug("Using cached frame count for source: %d", meta.frame_count)
            return meta.frame_count

    try:
        # --- ffprobe for fast metadata (duration / fps / resolution) ---
        duration: float | None = None
        ffprobe_data: dict | None = None

        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration,r_frame_rate,width,height",
                "-of", "json",
                str(video_file),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            ffprobe_data = json.loads(result.stdout)

            # Opportunistically populate the VideoMetadata instance
            if video_meta is not None:
                video_meta.populate_from_ffprobe(ffprobe_data)

            streams = ffprobe_data.get("streams", [])
            if streams:
                raw_dur = streams[0].get("duration")
                if raw_dur is not None:
                    try:
                        duration = float(raw_dur)
                    except (ValueError, TypeError):
                        pass

        except Exception as e:
            logger.debug("Failed to get video metadata via ffprobe: %s", e)

        # --- ffmpeg null-encode for frame count ---
        logger.info("Counting frames in %s ...", video_file.name)
        null_cmd = [
            "ffmpeg",
            "-i", str(video_file),
            "-map", "0:v:0",
            "-c", "copy",
            "-f", "null",
            "-",
        ]

        final_result = None
        for progress in run_ffmpeg_with_progress(
            null_cmd,
            expected_duration=duration,
            timeout=600,
            video_meta=video_meta,
        ):
            if hasattr(progress, "success"):
                final_result = progress
            elif progress.frame > 0 and progress.frame % 10_000 == 0:
                logger.debug("Counted %d frames (%.1fx speed)...", progress.frame, progress.speed)

        if final_result and final_result.success and final_result.final_progress:
            frame_count = final_result.final_progress.frame
            logger.debug("Frame count for %s: %d", video_file.name, frame_count)

            if tracker and is_source:
                tracker.update_source_metadata(
                    frame_count=frame_count,
                    duration_seconds=video_meta._duration_seconds if video_meta else None,
                    fps=video_meta._fps if video_meta else None,
                    resolution=video_meta._resolution if video_meta else None,
                )

            return frame_count

        error_msg = final_result.error_message if final_result else "Unknown error"
        raise FrameCountError(f"Frame counting failed: {error_msg}")

    except FrameCountError:
        raise
    except Exception as e:
        raise FrameCountError(f"Frame count error: {e}") from e


def verify_frame_counts(
    source_file: Path,
    chunk_files: list[Path],
    expected_total: int | None = None
) -> tuple[bool, int, int]:
    """Verify that sum of chunk frame counts matches source.

    Args:
        source_file: Path to source video file
        chunk_files: List of chunk file paths
        expected_total: Expected total frame count (if known, skips source counting)

    Returns:
        Tuple of (matches, source_frames, total_chunk_frames)

    Raises:
        FrameCountError: If frame counting fails
    """
    logger.debug("Verifying frame counts...")

    # Get source frame count if not provided
    if expected_total is None:
        source_frames = get_frame_count(source_file)
    else:
        source_frames = expected_total

    # Count frames in all chunks
    total_chunk_frames = 0
    for chunk_file in chunk_files:
        chunk_frames = get_frame_count(chunk_file)
        total_chunk_frames += chunk_frames
        logger.debug(f"  {chunk_file.name}: {chunk_frames} frames")

    matches = (source_frames == total_chunk_frames)

    if matches:
        logger.debug(f"Frame count verification passed: {source_frames} frames")
    else:
        logger.warning(
            f"Frame count mismatch: source={source_frames}, "
            f"chunks total={total_chunk_frames}, "
            f"difference={abs(source_frames - total_chunk_frames)}"
        )

    return matches, source_frames, total_chunk_frames


def detect_crop_parameters(
    video_file: Path,
    sample_count: int = 10,
    skip_seconds: int = 60
) -> CropParams | None:
    """Detect black borders using ffmpeg cropdetect filter.

    Samples multiple frames across the video to find conservative crop parameters
    that remove all black borders while preserving maximum content area.

    Args:
        video_file: Path to video file to analyze
        sample_count: Number of frames to sample across video
        skip_seconds: Seconds to skip at start (avoid intro/credits)

    Returns:
        CropParams if borders detected, None if no cropping needed

    Raises:
        FFmpegError: If crop detection fails
    """
    logger.debug(f"Detecting black borders in {video_file.name}...")

    try:
        # Get video duration first
        duration_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_file)
        ]
        duration_result = subprocess.run(
            duration_cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        duration = float(duration_result.stdout.strip())

        # Calculate sample positions (skip first skip_seconds, then distribute evenly)
        start_time = skip_seconds
        end_time = duration - 10  # Skip last 10 seconds too
        if end_time <= start_time:
            # Video too short, just sample from middle
            sample_positions = [duration / 2]
        else:
            step = (end_time - start_time) / (sample_count - 1) if sample_count > 1 else 0
            sample_positions = [start_time + i * step for i in range(sample_count)]

        # Collect crop detections from all samples
        crop_detections = []

        for pos in sample_positions:
            # Run cropdetect at this position
            cmd = [
                "ffmpeg",
                "-ss", str(pos),
                "-i", str(video_file),
                "-vf", "cropdetect=24:16:0",  # threshold=24, round=16, reset=0
                "-vframes", "5",              # analyze 5 frames to get a few outputs
                "-f", "null",
                "-"
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,  # cropdetect writes to stderr, not an error
                timeout=30
            )

            # Parse cropdetect output from stderr
            # Looking for lines like: [Parsed_cropdetect_0 @ ...] x1:0 x2:1919 y1:140 y2:939
            for line in result.stderr.split('\n'):
                if 'cropdetect' in line and 'x1:' in line:
                    # Extract crop values
                    match = re.search(r'x1:(\d+)\s+x2:(\d+)\s+y1:(\d+)\s+y2:(\d+)', line)
                    if match:
                        x1, x2, y1, y2 = map(int, match.groups())
                        crop_detections.append((x1, x2, y1, y2))
                        logger.debug(f"Sample at {pos:.1f}s: x1={x1} x2={x2} y1={y1} y2={y2}")

        if not crop_detections:
            logger.debug("No black borders detected")
            return None

        # Find most conservative crop (largest area that removes all borders)
        # This means: max x1, min x2, max y1, min y2
        max_x1 = max(d[0] for d in crop_detections)
        min_x2 = min(d[1] for d in crop_detections)
        max_y1 = max(d[2] for d in crop_detections)
        min_y2 = min(d[3] for d in crop_detections)

        # Convert to top/bottom/left/right format
        left = max_x1
        right = 0  # Will calculate from width
        top = max_y1
        bottom = 0  # Will calculate from height

        # Get video dimensions to calculate right and bottom
        dims_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(video_file)
        ]
        dims_result = subprocess.run(
            dims_cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        dims_data = json.loads(dims_result.stdout)
        width = dims_data['streams'][0]['width']
        height = dims_data['streams'][0]['height']

        right = width - min_x2 - 1
        bottom = height - min_y2 - 1

        # Check if crop is significant (at least 2 pixels on any side)
        if max(left, right, top, bottom) < 2:
            logger.debug("Detected borders too small, no cropping needed")
            return None

        crop = CropParams(top=top, bottom=bottom, left=left, right=right)
        logger.info(
            f"Detected black borders: {top} top, {bottom} bottom, "
            f"{left} left, {right} right "
            f"(removing {top+bottom}px vertical, {left+right}px horizontal)"
        )

        return crop

    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"Crop detection timed out") from e
    except subprocess.CalledProcessError as e:
        raise FFmpegError(f"FFmpeg crop detection failed: {e.stderr}") from e
    except Exception as e:
        raise FFmpegError(f"Crop detection error: {e}") from e
