"""Integration tests for the chunking scoring providers (real decodes).

The providers wrap real external work — a PySceneDetect decode (which
spawns ffmpeg) and a probe encode + measurement pass — so these smoke
tests run against a real, uncut video named by ``PYQENC_REAL_SAMPLE``
and stay skipped otherwise. They need cleanly produced real content:
every decoder the pipeline uses must agree on its frame count.
"""

import subprocess
from fractions import Fraction
from math import isfinite
from pathlib import Path

import pytest

from pyqenc.constants import TIMESTAMPS_FILENAME
from pyqenc.phases.chunking import fast_scoring, slow_scoring
from pyqenc.stream_model import (
    CropParams,
    ExtendedVideoStream,
    ProbedFile,
    SizedFingerprint,
    VideoStream,
    VideoStreamInfo,
)
from pyqenc.utils.timestamps import count_frames
from tests.fixtures.video_fixtures import get_real_sample_path, real_sample_exists

_STUB_FP = SizedFingerprint(token="0" * 32, size=64)
_SKIP = "PYQENC_REAL_SAMPLE does not name an existing video"


def _probe(sample: Path) -> tuple[float, float]:
    """The sample's average video frame rate and container duration (seconds)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate:format=duration",
         "-of", "default=nw=1:nk=1", sample],
        capture_output=True, text=True, check=True,
    )
    rate, duration = (line.strip() for line in result.stdout.splitlines() if line.strip())
    return float(Fraction(rate)), float(duration)


def _pipeline_frame_count(sample: Path, work: Path) -> int:
    """The frame count the way the pipeline establishes it: extraction's
    PTS index (mkvextract timecodes), read by the timestamps module."""
    work.mkdir(parents=True, exist_ok=True)
    index = work / TIMESTAMPS_FILENAME
    subprocess.run(
        ["mkvextract", str(sample), "timecodes_v2", f"0:{index}"],
        capture_output=True, check=True,
    )
    frame_count = count_frames(index)
    assert frame_count is not None, "mkvextract wrote an index the pipeline cannot read"
    return frame_count


def _real_stream(sample: Path, fps: float, duration: float) -> ExtendedVideoStream:
    """The sample as an extended stream (the providers read its path and track id)."""
    return ExtendedVideoStream(
        stream = VideoStream(
            file = ProbedFile(fingerprint=_STUB_FP, path=sample),
            info = VideoStreamInfo(
                track_id=0, codec_name="h264",
                fps=fps, fps_fraction=Fraction(fps),
                resolution="1920x1080", duration_seconds=duration,
            ),
        ),
        frame_count=0,  # unknown sentinel — the providers do not read it
        crop        = CropParams(),
    )


@pytest.mark.skipif(not real_sample_exists(), reason=_SKIP)
@pytest.mark.slow
@pytest.mark.integration
class TestFastScoring:
    def test_content_trace_over_the_real_sample(self, tmp_path):
        """Given the pipeline's frame count (the extraction PTS index's frame
        lines), one decode produces a content-score trace with exactly one
        entry per frame: the first frame has no predecessor and scores 0;
        later frames carry finite, non-negative scores. The detector's cut
        logic plays no role."""
        sample = get_real_sample_path()
        fps, duration = _probe(sample)
        frame_count = _pipeline_frame_count(sample, tmp_path / "index")
        result = fast_scoring(_real_stream(sample, fps, duration), frame_count)
        assert len(result.traces) == 1
        trace = result.traces[0]
        assert len(trace) == frame_count
        assert trace[0] == 0.0
        assert all(isfinite(value) and value >= 0.0 for value in trace)
        assert any(value > 0.0 for value in trace)


@pytest.mark.skipif(not real_sample_exists(), reason=_SKIP)
@pytest.mark.slow
@pytest.mark.integration
class TestSlowScoring:
    def test_probe_traces_over_the_real_sample(self, tmp_path):
        """The probe encode + measurement pass produce four finite traces —
        the content score first, then psnr, ssim, and log-bits — with one
        entry per pipeline frame and the same length: on a cleanly produced
        video every decoder the pipeline uses agrees with the frame count
        of the extraction PTS index."""
        sample = get_real_sample_path()
        fps, duration = _probe(sample)
        frame_count = _pipeline_frame_count(sample, tmp_path / "index")
        result = slow_scoring(
            _real_stream(sample, fps, duration), tmp_path, duration, fps, frame_count,
        )
        assert len(result.traces) == 4
        for trace in result.traces:
            assert len(trace) == frame_count
            assert all(isfinite(value) for value in trace)
        content, log_bits = result.traces[0], result.traces[3]
        assert content[0] == 0.0
        assert any(value > 0.0 for value in log_bits)
        # The probe file lands in the chunking workspace, not the work root.
        assert (tmp_path / "chunking" / "probe.mkv").exists()
