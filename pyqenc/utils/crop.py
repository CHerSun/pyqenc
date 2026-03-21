"""Black border crop detection utility."""
# CHerSun 2026

from __future__ import annotations

import logging
import os
import re
from os import PathLike
from pathlib import Path

from pyqenc.models import CropParams, VideoMetadata
from pyqenc.utils.ffmpeg_runner import run_ffmpeg

logger = logging.getLogger(__name__)


def detect_crop_parameters(
    video_file: VideoMetadata,
    sample_count: int = 50,
) -> CropParams:
    """Detect black borders using ffmpeg cropdetect filter.

    Samples multiple frames across the video to find conservative crop parameters
    that remove all black borders while preserving maximum content area.

    Always returns a ``CropParams`` instance — all-zero if no borders are found
    or if detection fails for any reason.

    Args:
        video_file:   Video metadata for the file to analyse.
        sample_count: Number of frames to sample across the video.

    Returns:
        CropParams with detected border offsets; all-zero if no cropping needed.
    """
    logger.debug("Detecting black borders in %s...", video_file.path)

    try:
        duration = video_file.duration_seconds

        if not duration:
            logger.warning("Duration is not available, skipping crop detection")
            return CropParams()

        # Distribute samples across the middle 80 % of the video
        start_time  = duration * 0.1
        step        = duration * 0.8 / (sample_count - 1) if sample_count > 1 else 0
        step_frames = (
            int(video_file.frame_count * 0.8 / sample_count) if video_file.frame_count
            else int(step * video_file.fps)                   if video_file.fps
            else 0
        )
        step_frames = max(min(step_frames, 500), 30)

        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", video_file.path,
            "-vf", f"select='not(mod(n\\,{step_frames}))',cropdetect=24:16:0",
            "-vframes", str(sample_count),
            "-f", "null",
            "-",
        ]

        result = run_ffmpeg(cmd, output_file=None)

        # Parse lines like: [Parsed_cropdetect_0 @ ...] w:1920 h:800 x:0 y:140
        crop_detections: list[tuple[int, int, int, int]] = []
        for line in result.stderr_lines:
            if "cropdetect" in line and "x1:" in line:
                match = re.search(r"w:(\d+)\s+h:(\d+)\s+x:(\d+)\s+y:(\d+)", line)
                if match:
                    w, h, x, y = map(int, match.groups())
                    crop_detections.append((w, h, x, y))

        logger.debug("Got %d crop samples.", len(crop_detections))

        if not crop_detections:
            logger.warning("No black borders detected")
            return CropParams()

        # Most conservative crop: smallest detected content area
        max_w = max(d[0] for d in crop_detections)
        max_h = max(d[1] for d in crop_detections)
        min_x = min(d[2] for d in crop_detections)
        min_y = min(d[3] for d in crop_detections)

        left = min_x
        top  = min_y

        width, height = video_file.resolution.split("x", 1) if video_file.resolution else ("0", "0")
        right  = int(width)  - max_w - left
        bottom = int(height) - max_h - top

        crop = CropParams(top=top, bottom=bottom, left=left, right=right)
        logger.info(f"Cropping: {crop.display()} (detected)")
        return crop

    except Exception as e:
        logger.error("Failed to detect crop parameters: %s", e)
        return CropParams()
