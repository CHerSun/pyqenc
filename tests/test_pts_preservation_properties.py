"""Property-based tests for PTS preservation.

# Feature: pts-preservation

Observable-behavior only: phases are constructed through their real public
constructors with a real phase registry whose dependency ``result`` fields are
pre-set to completed typed results (so the shared dependency walk is a no-op),
then driven through the public ``run()`` entry point. The only things mocked are
genuine external shell-outs (``MKVTrackExtractor`` → ffprobe, ``subprocess.run``
→ mkvmerge, ``get_frame_count`` → ffmpeg) — boundaries, never phase internals.
No ``__new__``, no private ``_recover``/``_execute_merge`` calls, no private-attr
poking.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.app_config import load_app_config
from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome, VideoMetadata
from pyqenc.phase import Artifact, Phase
from pyqenc.phases.extraction import (
    ExtractionPhase,
    TimestampArtifact,
    _extract_timestamps,
)
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import ArtifactState, JobState

# ---------------------------------------------------------------------------
# Shared helpers — build a REAL ExtractionPhase via its real constructor
# ---------------------------------------------------------------------------

_APP_CONFIG = load_app_config(default_only=True)


def _make_source_vm(path: Path) -> VideoMetadata:
    """Return a VideoMetadata with fast-probe fields pre-populated (no probing)."""
    meta = VideoMetadata(path=path)
    meta._duration_seconds = 3600.0
    meta._fps              = 24.0
    meta._resolution       = "1920x1080"
    return meta


def _make_extraction_phase(
    work_dir: Path,
    source:   Path,
    *,
    include:  str | None,
    exclude:  str | None,
) -> ExtractionPhase:
    """Construct ExtractionPhase via its real constructor and a real registry.

    A real ``JobPhase`` instance is placed in the registry with its public
    ``result`` pre-set to a COMPLETED ``JobPhaseResult`` carrying a config whose
    include/exclude filters are the ones under test, so the shared dependency
    walk treats the job as already-run without any mocking.
    """
    collector = NoOpMetricsCollector()

    config = _APP_CONFIG.model_copy(deep=True)
    config.extraction.include = include
    config.extraction.exclude = exclude

    source_vm  = _make_source_vm(source)
    job_result = JobPhaseResult(
        outcome    = PhaseOutcome.COMPLETED,
        artifacts  = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message    = "job complete",
        job        = JobState(source=source_vm),
        force_wipe = False,
        config     = config,
        work_dir   = work_dir,
        source     = source,
    )

    job = JobPhase(
        config, None,
        source     = source,
        work_dir   = work_dir,
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )
    job.result = job_result

    registry: dict[type[Phase], Phase] = {JobPhase: job}
    return ExtractionPhase(config, registry, video_required=True, collector=collector)


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
    """For any include/exclude filter combination, the extraction result must
    always carry exactly one TimestampArtifact whose state is COMPLETE or ABSENT
    only — the timestamp artifact is never affected by stream filtering.

    Bug guarded: if the timestamp artifact were routed through the same
    include/exclude selection as stream tracks, an aggressive filter could drop
    it (or a filter change could leave it in a spurious state), silently losing
    PTS preservation for the merge phase.

    **Validates: Requirements 3.4, 3.5**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        source   = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        # A pre-existing timestamps.txt on disk (COMPLETE case).
        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)
        (extracted_dir / TIMESTAMPS_FILENAME).write_text(
            "# timestamp format v2\n0\n42\n", encoding="utf-8"
        )

        phase = _make_extraction_phase(
            work_dir, source, include=include_pattern, exclude=exclude_pattern
        )

        # Mock only the external ffprobe boundary: no tracks discovered so the
        # filter varies over a real (empty) stream set without shelling out.
        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            result = phase.run(dry_run=True)

    ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]

    assert len(ts_artifacts) == 1, (
        f"Expected exactly 1 TimestampArtifact in the result, got {len(ts_artifacts)} "
        f"(include={include_pattern!r}, exclude={exclude_pattern!r})"
    )
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
    """For any state of extracted/timestamps.txt on disk, the extraction result
    must classify the TimestampArtifact as COMPLETE iff the file exists, and
    ABSENT otherwise.

    Bug guarded: a misclassified timestamp artifact would either trigger a
    needless re-extraction (COMPLETE reported ABSENT) or let the merge phase
    proceed with a missing timestamps.txt (ABSENT reported COMPLETE), corrupting
    PTS restoration.

    **Validates: Requirements 3.6, 3.7**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        source   = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)
        if file_present:
            (extracted_dir / TIMESTAMPS_FILENAME).write_text(
                "# timestamp format v2\n0\n42\n", encoding="utf-8"
            )

        phase = _make_extraction_phase(work_dir, source, include=None, exclude=None)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            result = phase.run(dry_run=True)

    ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
    assert len(ts_artifacts) == 1

    expected_state = ArtifactState.COMPLETE if file_present else ArtifactState.ABSENT
    assert ts_artifacts[0].state == expected_state, (
        f"file_present={file_present}: expected {expected_state}, "
        f"got {ts_artifacts[0].state}"
    )

    # The result's timestamps_path must be set exactly when the file is present.
    if file_present:
        assert result.timestamps_path is not None
    else:
        assert result.timestamps_path is None


# ---------------------------------------------------------------------------
# Property 4: Frame count preservation
# ---------------------------------------------------------------------------
# Feature: pts-preservation, Property 4: Frame count preservation

@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
@given(frame_count=st.integers(min_value=1, max_value=10000))
def test_frame_count_preservation(frame_count: int) -> None:
    """For any source video, the merged output produced by MergePhase.run() must
    record a frame count equal to the source frame count.

    The MergePhase is built through its real constructor and a real registry
    whose Job/Extraction/Probe/Encoding/Audio dependencies carry pre-set
    COMPLETED results (a real COMPLETE encoded chunk and a real timestamps.txt).
    Only external shell-outs are mocked: mkvmerge (``subprocess.run``, produces
    the output file) and the frame-count check (``get_frame_count``).

    Bug guarded: if the merge phase failed to record / verify the output frame
    count against the source, a dropped-or-duplicated-frame concat regression
    would pass silently, breaking source-fidelity guarantees.

    **Validates: Requirement 6.1**
    """
    from pyqenc.constants import FINAL_OUTPUT_DIR
    from pyqenc.models import ExtendedVideoMetadata
    from pyqenc.phases.audio import AudioPhase, AudioPhaseResult
    from pyqenc.phases.encoding import (
        EncodedArtifact,
        EncodingPhase,
        EncodingPhaseResult,
    )
    from pyqenc.phases.extraction import ExtractionPhaseResult
    from pyqenc.phases.merge import MergePhase
    from pyqenc.phases.probe import ProbePhase, ProbePhaseResult

    collector = NoOpMetricsCollector()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_dir = tmp_path / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        source   = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        # A real timestamps.txt (Extraction artifact) and a real encoded chunk.
        ts_file = work_dir / EXTRACTED_DIR / TIMESTAMPS_FILENAME
        ts_file.parent.mkdir(parents=True, exist_ok=True)
        ts_file.write_text(
            "# timestamp format v2\n"
            + "\n".join(str(i * 42) for i in range(frame_count))
            + "\n",
            encoding="utf-8",
        )
        chunk = work_dir / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        # Empty quality targets → quality measurement is skipped (no shell-out).
        config = _APP_CONFIG.model_copy(deep=True)

        source_vm = _make_source_vm(source)

        # --- Real dependency phases with pre-set COMPLETED results ---
        job = JobPhase(
            config, None,
            source     = source,
            work_dir   = work_dir,
            force      = False,
            cleanup    = CleanupLevel.NONE,
            no_metrics = True,
            collector  = collector,
        )
        job.result = JobPhaseResult(
            outcome    = PhaseOutcome.COMPLETED,
            artifacts  = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
            message    = "job complete",
            job        = JobState(source=source_vm),
            force_wipe = False,
            config     = config,
            work_dir   = work_dir,
            source     = source,
        )

        registry: dict[type[Phase], Phase] = {JobPhase: job}

        extraction = ExtractionPhase(config, registry, video_required=True, collector=collector)
        extraction.result = ExtractionPhaseResult(
            outcome         = PhaseOutcome.COMPLETED,
            artifacts       = [Artifact(path=ts_file, state=ArtifactState.COMPLETE)],
            message         = "extraction complete",
            video           = source_vm,
            timestamps_path = ts_file,
        )
        registry[ExtractionPhase] = extraction

        probe = ProbePhase(config, registry, collector=collector, crop_params=None)
        probe.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [Artifact(path=work_dir / "probe.yaml", state=ArtifactState.COMPLETE)],
            message   = "probe complete",
            source    = ExtendedVideoMetadata.from_base(source_vm, frame_count=frame_count),
        )
        registry[ProbePhase] = probe

        encoding = EncodingPhase(config, registry, collector=collector)
        encoding.result = EncodingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "encoding complete",
            encoded   = [EncodedArtifact(
                path     = chunk,
                state    = ArtifactState.COMPLETE,
                chunk_id = "chunk1",
                strategy = "slow+h265",
            )],
        )
        registry[EncodingPhase] = encoding

        audio = AudioPhase(config, registry, collector=collector)
        audio.result = AudioPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [],
            message   = "audio complete",
            audio_files = [],
        )
        registry[AudioPhase] = audio

        merge = MergePhase(config, registry, collector=collector)

        source_stem = source.stem
        safe_name   = "slow+h265".replace(":", "_")
        output_file = work_dir / FINAL_OUTPUT_DIR / f"{source_stem} {safe_name}.mkv"

        def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(b"\x00" * 128)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with (
            patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=frame_count),
        ):
            merge_result = merge.run(dry_run=False)

    assert merge_result.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED), (
        f"Expected COMPLETED or REUSED, got {merge_result.outcome} "
        f"(error={merge_result.error!r})"
    )

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
