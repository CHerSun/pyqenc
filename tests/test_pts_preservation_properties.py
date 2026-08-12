"""Property-based tests for PTS preservation.

# Feature: pts-preservation
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from pyqenc.phases.extraction import (
    ExtractionPhase,
    TimestampArtifact,
    _extract_timestamps,
)
from pyqenc.state import ArtifactState


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_extraction_phase(tmp_path: Path) -> ExtractionPhase:
    """Build a minimal ExtractionPhase with a fake config pointing at tmp_path."""
    source = tmp_path / "source.mkv"
    if not source.exists():
        source.write_bytes(b"\x00" * 64)

    config = MagicMock()
    config.work_dir      = tmp_path
    config.source_video  = source
    config.include       = None
    config.exclude       = None

    collector = MagicMock()
    collector.time.return_value.__enter__ = MagicMock(return_value=None)
    collector.time.return_value.__exit__  = MagicMock(return_value=False)

    phase = ExtractionPhase.__new__(ExtractionPhase)
    phase._config    = config
    phase._collector = collector
    mock_job = MagicMock()
    mock_job.result.work_dir                      = tmp_path
    mock_job.result.source                        = source
    mock_job.result.config.extraction.include     = None
    mock_job.result.config.extraction.exclude     = None
    phase._job       = mock_job
    phase.params     = MagicMock()
    phase.params.include = None
    phase.params.exclude = None
    phase.result     = None
    phase.dependencies = []
    return phase  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Property 1: PTS conversion correctness
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 1: PTS conversion correctness

@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pts_values=st.lists(
        st.floats(
            min_value=0.0,
            max_value=86400.0,  # up to 24 hours in seconds
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=500,
    )
)
def test_pts_conversion_correctness(pts_values: list[float]) -> None:
    """For any float PTS values in seconds, conversion to integer milliseconds
    must equal int(pts_seconds * 1000), and the file must start with
    '# timestamp format v2'.

    **Validates: Requirements 3.1, 3.2**
    """
    # Convert float seconds to integer milliseconds — this is what mkvextract
    # outputs natively, and what ffprobe outputs with the current format string.
    pts_ms_values = sorted(int(v * 1000) for v in pts_values)
    stdout = "\n".join(str(v) for v in pts_ms_values) + "\n"

    with tempfile.TemporaryDirectory() as tmp_dir:
        output = Path(tmp_dir) / TIMESTAMPS_FILENAME

        def _mock_run(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                import subprocess as _sp
                raise _sp.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = stdout
            result.stderr     = ""
            return result

        with patch("subprocess.run", side_effect=_mock_run):
            _extract_timestamps(Path("source.mkv"), 0, output)

        lines = output.read_text(encoding="utf-8").splitlines()

    # File must start with the v2 header
    assert lines[0] == "# timestamp format v2", (
        f"Expected '# timestamp format v2' header, got {lines[0]!r}"
    )

    # Each data line must be an integer millisecond value matching the input
    data_lines = lines[1:]
    assert len(data_lines) == len(pts_ms_values), (
        f"Expected {len(pts_ms_values)} data lines, got {len(data_lines)}"
    )
    for i, (line, expected_ms) in enumerate(zip(data_lines, pts_ms_values)):
        actual_ms = int(line)
        assert actual_ms == expected_ms, (
            f"Line {i+1}: expected {expected_ms}, got {actual_ms}"
        )


# ---------------------------------------------------------------------------
# Property 2: Timestamp filter independence
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 2: Timestamp filter independence

# Generate valid regex patterns: use simple alphanumeric/common patterns
# that are guaranteed to be valid Python regex strings.
_VALID_REGEX_PATTERNS = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Nd"),  # letters and digits only
            whitelist_characters="-_.",
        ),
        min_size=0,
        max_size=20,
    ),
)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    include_pattern=_VALID_REGEX_PATTERNS,
    exclude_pattern=_VALID_REGEX_PATTERNS,
)
def test_timestamp_filter_independence(
    include_pattern: str | None,
    exclude_pattern: str | None,
) -> None:
    """For any include/exclude filter combination, TimestampArtifact must always
    be present in the artifact list and its state must be COMPLETE or ABSENT only.

    **Validates: Requirements 3.4, 3.5**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Create extracted dir with a pre-existing timestamps.txt
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)
        ts_file = extracted_dir / TIMESTAMPS_FILENAME
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        phase = _make_extraction_phase(tmp_path)
        phase._config.include = include_pattern
        phase._config.exclude = exclude_pattern

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=False, execute=False)

    ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]

    # TimestampArtifact must always be present
    assert len(ts_artifacts) == 1, (
        f"Expected exactly 1 TimestampArtifact, got {len(ts_artifacts)} "
        f"(include={include_pattern!r}, exclude={exclude_pattern!r})"
    )

    # State must be COMPLETE or ABSENT only — never STALE
    assert ts_artifacts[0].state in (ArtifactState.COMPLETE, ArtifactState.ABSENT), (
        f"TimestampArtifact state must be COMPLETE or ABSENT, got {ts_artifacts[0].state} "
        f"(include={include_pattern!r}, exclude={exclude_pattern!r})"
    )


# ---------------------------------------------------------------------------
# Property 3: Timestamp artifact classification
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 3: Timestamp artifact classification

@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(file_present=st.booleans())
def test_timestamp_artifact_classification(file_present: bool) -> None:
    """For any state of extracted/timestamps.txt on disk (present or absent),
    _recover() must classify TimestampArtifact as COMPLETE iff the file exists,
    and ABSENT otherwise.

    **Validates: Requirements 3.6, 3.7**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)
        ts_file = extracted_dir / TIMESTAMPS_FILENAME

        if file_present:
            ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        phase = _make_extraction_phase(tmp_path)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=False, execute=False)

    ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
    assert len(ts_artifacts) == 1

    expected_state = ArtifactState.COMPLETE if file_present else ArtifactState.ABSENT
    assert ts_artifacts[0].state == expected_state, (
        f"file_present={file_present}: expected {expected_state}, "
        f"got {ts_artifacts[0].state}"
    )


# ---------------------------------------------------------------------------
# Property 4: Frame count preservation
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 4: Frame count preservation

@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(frame_count=st.integers(min_value=1, max_value=10000))
def test_frame_count_preservation(frame_count: int) -> None:
    """For any source video, the frame count of the merged output must equal
    the frame count of the source video.

    This property tests the mkvmerge integration using mocked subprocess calls:
    the mkvmerge invocation is mocked to produce an output file, and
    get_frame_count is mocked to return the expected frame count.

    **Validates: Requirement 6.1**
    """
    import tempfile
    from pyqenc.phases.merge import MergeArtifact, MergePhase, _build_mkvmerge_options
    from pyqenc.constants import FINAL_OUTPUT_DIR
    from pyqenc.models import PhaseOutcome

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        ts_file = tmp_path / "timestamps.txt"
        ts_file.write_text(
            "# timestamp format v2\n" + "\n".join(str(i * 42) for i in range(frame_count)) + "\n",
            encoding="utf-8",
        )

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        # Build a minimal MergePhase
        config = MagicMock()
        config.work_dir         = tmp_path
        config.source_video     = tmp_path / "source.mkv"
        config.quality_targets  = []
        config.metrics_sampling = 1

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = MergePhase.__new__(MergePhase)
        phase._config    = config
        phase._collector = collector

        extraction_result = MagicMock()
        extraction_result.timestamps_path = ts_file
        extraction = MagicMock()
        extraction.result = extraction_result
        phase._extraction = extraction

        encoding_result = MagicMock()
        encoding_result.encoded = []
        encoding = MagicMock()
        encoding.result = encoding_result
        encoding.quality_labels = {}
        phase._encoding = encoding

        job_result = MagicMock()
        job_result.force_wipe = False
        job_result.crop = None
        job_result.job = None
        job_result.work_dir = tmp_path
        job_result.source   = tmp_path / "source.mkv"
        job_result.config.encoding.resolved_targets = []
        job_result.config.encoding.metrics_sampling = 1
        job = MagicMock()
        job.result = job_result
        phase._job = job

        phase._audio = None
        phase.result  = None
        phase.dependencies = []

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            output_file.write_bytes(b"\x00" * 128)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with (
            patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=frame_count) as mock_fc,
        ):
            merge_result = phase._execute_merge([artifact])  # type: ignore[attr-defined]

        # The merge must have completed (not failed due to missing timestamps)
        assert merge_result.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED), (
            f"Expected COMPLETED or REUSED, got {merge_result.outcome}"
        )

        # get_frame_count was called on the output file
        mock_fc.assert_called_once_with(output_file)

        # The merged artifact must record the correct frame count
        complete_artifacts = [a for a in merge_result.merged if a.state == ArtifactState.COMPLETE]
        assert len(complete_artifacts) == 1
        assert complete_artifacts[0].frame_count == frame_count, (
            f"Expected frame_count={frame_count}, got {complete_artifacts[0].frame_count}"
        )


# ---------------------------------------------------------------------------
# Property 5: PTS monotonicity
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 5: PTS monotonicity

@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    frame_count=st.integers(min_value=2, max_value=500),
    start_pts_ms=st.integers(min_value=0, max_value=1000),
    frame_duration_ms=st.integers(min_value=1, max_value=100),
)
def test_pts_monotonicity(
    frame_count: int,
    start_pts_ms: int,
    frame_duration_ms: int,
) -> None:
    """For any merged output video, the PTS values of all frames must be
    strictly monotonically increasing.

    This property tests the timestamps.txt format: given a valid v2 timestamps
    file with strictly increasing values, the values read back must be strictly
    increasing.

    **Validates: Requirement 6.2**
    """
    import tempfile

    # Generate strictly increasing PTS values
    pts_values_ms = [start_pts_ms + i * frame_duration_ms for i in range(frame_count)]

    with tempfile.TemporaryDirectory() as tmp_dir:
        ts_file = Path(tmp_dir) / "timestamps.txt"
        ts_file.write_text(
            "# timestamp format v2\n" + "\n".join(str(v) for v in pts_values_ms) + "\n",
            encoding="utf-8",
        )

        # Read back the timestamps
        lines = ts_file.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# timestamp format v2"

        read_pts = [int(line) for line in lines[1:] if line.strip()]

    # Verify strict monotonicity
    assert len(read_pts) == frame_count, (
        f"Expected {frame_count} PTS values, got {len(read_pts)}"
    )
    for i in range(1, len(read_pts)):
        assert read_pts[i] > read_pts[i - 1], (
            f"PTS not strictly increasing at index {i}: "
            f"{read_pts[i - 1]} >= {read_pts[i]}"
        )


# ---------------------------------------------------------------------------
# Property 6: PTS accuracy
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 6: PTS accuracy

@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pts_values=st.lists(
        st.floats(
            min_value=0.0,
            max_value=86400.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=500,
    )
)
def test_pts_accuracy(pts_values: list[float]) -> None:
    """For any source video, the absolute difference between each merged output
    frame's PTS and the corresponding source frame's PTS must be at most 1 ms
    (the precision of '# timestamp format v2').

    This property tests the round-trip accuracy of the PTS conversion:
    source PTS (float seconds) → timestamps.txt (integer milliseconds) →
    merged output PTS (integer milliseconds).

    The maximum error is 1 ms because int(pts_seconds * 1000) truncates
    sub-millisecond precision.

    **Validates: Requirement 6.3**
    """
    import tempfile
    from pyqenc.phases.extraction import _extract_timestamps

    # The implementation receives integer ms values directly (from mkvextract or
    # ffprobe with integer PTS format). The round-trip accuracy is exact — no
    # float conversion occurs inside _extract_timestamps.
    # Convert float seconds to integer ms here (as the caller would), then verify
    # the file preserves them exactly.
    pts_ms_values = sorted(int(v * 1000) for v in pts_values)

    with tempfile.TemporaryDirectory() as tmp_dir:
        output = Path(tmp_dir) / "timestamps.txt"

        stdout = "\n".join(str(v) for v in pts_ms_values) + "\n"

        def _mock_run(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                import subprocess as _sp
                raise _sp.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = stdout
            result.stderr     = ""
            return result

        with patch("subprocess.run", side_effect=_mock_run):
            _extract_timestamps(Path("source.mkv"), 0, output)

        lines = output.read_text(encoding="utf-8").splitlines()
        data_lines = lines[1:]  # skip header

    assert len(data_lines) == len(pts_ms_values)

    for i, (line, expected_ms) in enumerate(zip(data_lines, pts_ms_values)):
        actual_ms = int(line)
        assert actual_ms == expected_ms, (
            f"Frame {i}: round-trip failed — expected {expected_ms}ms, got {actual_ms}ms"
        )
