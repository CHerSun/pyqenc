"""Black border crop detection utility."""
# CHerSun 2026

from __future__ import annotations

import logging
import re

from pyqenc.constants import FFMPEG_ARG_VF, FFMPEG_SELECTOR_PREFIX
from pyqenc.models import CropParams
from pyqenc.stream_model import VideoStream
from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRequest, run_ffmpeg

logger = logging.getLogger(__name__)


def detect_crop_parameters(
    stream: VideoStream,
    sample_count: int = 50,
) -> CropParams:
    """Detect black borders using ffmpeg cropdetect filter.

    Samples multiple frames across the middle of the source (through the
    stream's own input selector) to find conservative crop parameters that
    remove all black borders while preserving maximum content area.

    Always returns a ``CropParams`` instance — all-zero if no borders are found
    or if detection fails for any reason (Req 3.2: auto-detect failure falls
    back to an empty crop with the warning logged here).

    Args:
        stream:       The source's video stream (file + fast-facet info).
        sample_count: Number of frames to sample across the video.

    Returns:
        CropParams with detected border offsets; all-zero if no cropping needed.
    """
    logger.debug("Detecting black borders in %s...", stream.file.path)

    try:
        duration = stream.info.duration_seconds

        if not duration:
            logger.warning("Duration is not available, skipping crop detection")
            return CropParams()

        # Distribute samples across the middle 80 % of the video
        start_time  = duration * 0.1
        step        = duration * 0.8 / (sample_count - 1) if sample_count > 1 else 0
        step_frames = int(step * stream.info.fps) if stream.info.fps else 0
        step_frames = max(min(step_frames, 500), 30)

        request = FFmpegRequest(
            inputs      = [
                FFmpegInput(
                    path           = stream.file.path,
                    selector       = f"{FFMPEG_SELECTOR_PREFIX}{stream.info.track_id}",
                    start_seconds  = start_time,
                ),
            ],
            output_args = (
                FFMPEG_ARG_VF, f"select='not(mod(n\\,{step_frames}))',cropdetect=24:2:0", # cropdetect takes cropdetect=limit:round:skip:reset
                "-vframes", str(sample_count),                             # default round is 16 (safe). minimum round=2 (for crhoma colors)
            ),                                                                   # lowered rounding to 2 to reach the same cropping as handbrake
        )

        result = run_ffmpeg(request)

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

        width, height = stream.info.resolution.split("x", 1) if stream.info.resolution else ("0", "0")
        right  = int(width)  - max_w - left
        bottom = int(height) - max_h - top

        crop = CropParams(top=top, bottom=bottom, left=left, right=right)
        logger.info(f"Cropping: {crop.display()} (detected)")
        return crop

    except (OSError, ValueError) as e:
        logger.error("Failed to detect crop parameters: %s", e)
        return CropParams()
