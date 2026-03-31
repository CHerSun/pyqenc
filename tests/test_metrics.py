"""Unit tests for pyqenc/metrics.py.

Covers enum membership, protocol conformance, and lifecycle behaviour.
"""

from pyqenc.metrics import NoOpMetricsCollector, SpaceKey, TimeKey


# ---------------------------------------------------------------------------
# TimeKey enum membership
# ---------------------------------------------------------------------------

_EXPECTED_TIME_KEYS: list[tuple[str, str]] = [
    ("JOB_PROBE",             "job.probe"),
    ("JOB_CROP_DETECT",       "job.crop_detect"),
    ("EXTRACTION",            "extraction.mkvextract"),
    ("CHUNKING_SCENE_DETECT", "chunking.scene_detect"),
    ("CHUNKING_SPLIT",        "chunking.split"),
    ("AUDIO",                 "audio.processing"),
    ("ENCODING_OPTIMIZATION", "encoding.optimization"),
    ("ENCODING_MAIN",         "encoding.main"),
    ("MERGE_CONCAT",          "merge.concat"),
    ("MERGE_QUALITY_MEASURE", "merge.quality_measure"),
    ("RECOVERY",              "recovery"),
]


def test_time_key_member_count() -> None:
    """TimeKey must have exactly 11 members (Req 2.4)."""
    assert len(TimeKey) == 11


def test_time_key_member_names_and_values() -> None:
    """Every TimeKey member must have the correct dotted string value (Req 2.4)."""
    for name, expected_value in _EXPECTED_TIME_KEYS:
        member = TimeKey[name]
        assert member.value == expected_value, (
            f"TimeKey.{name}: expected {expected_value!r}, got {member.value!r}"
        )


def test_time_key_is_str() -> None:
    """TimeKey values must be plain strings (StrEnum contract)."""
    for member in TimeKey:
        assert isinstance(member, str)


# ---------------------------------------------------------------------------
# SpaceKey enum membership
# ---------------------------------------------------------------------------

_EXPECTED_SPACE_KEYS: list[tuple[str, str]] = [
    ("SOURCE",             "source"),
    ("EXTRACTED_VIDEO",    "extracted.video"),
    ("EXTRACTED_AUDIO",    "extracted.audio"),
    ("EXTRACTED_OTHER",    "extracted.other"),
    ("CHUNKS",             "chunks"),
    ("AUDIO_INTERMEDIATE", "audio.intermediate"),
    ("AUDIO_FINAL",        "audio.final"),
    ("ENCODING_WORKSPACE", "encoding.workspace"),
    ("ENCODING_OUTPUTS",   "encoding.outputs"),
    ("FINAL",              "final"),
]


def test_space_key_member_count() -> None:
    """SpaceKey must have exactly 10 members (Req 3.2)."""
    assert len(SpaceKey) == 10


def test_space_key_member_names_and_values() -> None:
    """Every SpaceKey member must have the correct dotted string value (Req 3.2)."""
    for name, expected_value in _EXPECTED_SPACE_KEYS:
        member = SpaceKey[name]
        assert member.value == expected_value, (
            f"SpaceKey.{name}: expected {expected_value!r}, got {member.value!r}"
        )


def test_space_key_is_str() -> None:
    """SpaceKey values must be plain strings (StrEnum contract)."""
    for member in SpaceKey:
        assert isinstance(member, str)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_noop_collector_time_returns_context_manager() -> None:
    """NoOpMetricsCollector.time() must return a usable context manager."""
    collector = NoOpMetricsCollector()
    with collector.time(TimeKey.ENCODING_MAIN):
        pass  # must not raise


def test_noop_collector_record_step_is_noop() -> None:
    """NoOpMetricsCollector.record_step() must accept all args without error."""
    from pyqenc.metrics import ConvergenceUpdate
    collector = NoOpMetricsCollector()
    collector.record_step(TimeKey.ENCODING_MAIN, 1.5)
    collector.record_step(
        TimeKey.ENCODING_MAIN,
        2.0,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )


def test_noop_collector_flush_is_noop() -> None:
    """NoOpMetricsCollector.flush() must accept partial flag without error."""
    collector = NoOpMetricsCollector()
    collector.flush(partial=True)
    collector.flush(partial=False)
