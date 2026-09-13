"""Unit tests for the ``audio.select`` resolver (:func:`resolve_selection`).

Each test names the concrete bug it guards against and checks observable
behaviour — the tracks the resolver returns — never internal representation.

Tracks are built with distinct ``path`` values because the resolver dedups on
``path``; a shared path would collapse distinct tracks and hide bugs.
"""
# CHerSun 2026

from pathlib import Path

from pyqenc.app_config import SelectEntry
from pyqenc.audio.layout import ChannelLayout
from pyqenc.audio.select import resolve_selection
from pyqenc.models import AudioMetadata


def _track(
    name:     str,
    language: str,
    layout:   str,
    title:    str | None = None,
) -> AudioMetadata:
    """Build an ``AudioMetadata`` with a unique path (the dedup key)."""
    return AudioMetadata(
        path     = Path(f"/fake/{name}.mka"),
        language = language,
        layout   = ChannelLayout.parse(layout),
        title    = title,
    )


def _names(tracks: list[AudioMetadata]) -> list[str]:
    """Return each track's file stem for readable assertions."""
    return [t.path.stem for t in tracks]


class TestEmptySelect:
    """Empty/absent select selects all tracks (Req 5.3)."""

    def test_empty_select_returns_all_tracks_in_order(self) -> None:
        # Bug: an empty select silently dropping tracks (or reordering them)
        # would skip audio the user expected processed by default.
        tracks = [
            _track("a", "eng", "5.1"),
            _track("b", "rus", "2.0"),
            _track("c", "jpn", "7.1"),
        ]
        result = resolve_selection(tracks, [])
        assert _names(result) == ["a", "b", "c"]


class TestForAndExclude:
    """Candidates are ``for`` matches minus ``exclude`` matches (Req 5.5)."""

    def test_for_language_selects_all_matching_dubs(self) -> None:
        # Bug: a `for` gate picking only one match instead of all would drop
        # some of the requested-language dubs (rus-dubs case).
        tracks = [
            _track("rus_51",  "rus", "5.1"),
            _track("rus_20",  "rus", "2.0"),
            _track("eng_51",  "eng", "5.1"),
        ]
        entry = SelectEntry(**{"for": "lang=rus"})
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["rus_51", "rus_20"]

    def test_exclude_drops_comment_tracks(self) -> None:
        # Bug: not honouring `exclude` would process commentary tracks the user
        # explicitly filtered out.
        tracks = [
            _track("main",    "eng", "5.1", title="Surround"),
            _track("comment", "eng", "2.0", title="Director Commentary"),
        ]
        entry = SelectEntry(**{"for": "lang=eng", "exclude": "comment"})
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["main"]

    def test_matching_is_case_insensitive(self) -> None:
        # Bug: case-sensitive matching would fail a user regex written in a
        # different case than the source tag (extraction filtering is
        # case-insensitive, so selection must be too).
        tracks = [_track("eng_51", "eng", "5.1")]
        entry = SelectEntry(**{"for": "LANG=ENG"})
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["eng_51"]


class TestPreferTiers:
    """The first prefer tier with a match wins; else all candidates (Req 5.6, 5.7)."""

    def test_71_wins_over_51_when_present(self) -> None:
        # Bug: merging tiers (or picking the wrong tier) would select both 7.1
        # and 5.1 instead of just the top available tier.
        tracks = [
            _track("eng_51", "eng", "5.1"),
            _track("eng_71", "eng", "7.1"),
            _track("eng_20", "eng", "2.0"),
        ]
        entry = SelectEntry(**{
            "for":    "lang=eng",
            "prefer": [r"ch=7\.1", r"ch=5\.1", "ch="],
        })
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["eng_71"]

    def test_51_wins_when_no_71(self) -> None:
        # Bug: falling through to the wildcard tier instead of the 5.1 tier
        # would grab every track when the intent was "5.1 if no 7.1".
        tracks = [
            _track("eng_51", "eng", "5.1"),
            _track("eng_20", "eng", "2.0"),
        ]
        entry = SelectEntry(**{
            "for":    "lang=eng",
            "prefer": [r"ch=7\.1", r"ch=5\.1", "ch="],
        })
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["eng_51"]

    def test_wildcard_fallback_tier_selects_all_when_no_named_tier(self) -> None:
        # Bug: an explicit catch-all tier not matching would leave the user with
        # nothing when they asked for "any layout as a last resort".
        tracks = [
            _track("eng_20", "eng", "2.0"),
            _track("eng_10", "eng", "1.0"),
        ]
        entry = SelectEntry(**{
            "for":    "lang=eng",
            "prefer": [r"ch=7\.1", r"ch=5\.1", "ch="],
        })
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["eng_20", "eng_10"]

    def test_implicit_fallback_all_candidates_when_no_tier_matches(self) -> None:
        # Bug: no implicit fallback would drop every candidate when the user's
        # prefer tiers happen to match none of them (Req 5.7).
        tracks = [
            _track("eng_20", "eng", "2.0"),
            _track("eng_10", "eng", "1.0"),
        ]
        entry = SelectEntry(**{
            "for":    "lang=eng",
            "prefer": [r"ch=7\.1", r"ch=5\.1"],  # neither matches
        })
        result = resolve_selection(tracks, [entry])
        assert _names(result) == ["eng_20", "eng_10"]


class TestAdditiveEntries:
    """Entries are additive with dedup on overlap (Req 5.9)."""

    def test_two_entries_additive_with_dedup_on_overlap(self) -> None:
        # Bug: treating entries as fallbacks (or duplicating an overlapping
        # track) would either drop the second entry's picks or emit the same
        # track twice, producing duplicate chain outputs.
        rus = _track("rus_51", "rus", "5.1")
        eng = _track("eng_71", "eng", "7.1")
        tracks = [rus, eng]
        entries = [
            SelectEntry(**{"for": "lang=rus"}),
            SelectEntry(**{"for": "lang=eng"}),
            SelectEntry(**{"for": "ch="}),  # overlaps both — must not duplicate
        ]
        result = resolve_selection(tracks, entries)
        assert _names(result) == ["rus_51", "eng_71"]

    def test_overlap_track_appears_once_in_original_order(self) -> None:
        # Bug: a track picked by multiple entries appearing more than once, or
        # the union losing original order determinism.
        a = _track("a", "eng", "7.1")
        b = _track("b", "rus", "5.1")
        c = _track("c", "eng", "2.0")
        tracks = [a, b, c]
        entries = [
            SelectEntry(**{"for": "lang=eng"}),  # picks a, c
            SelectEntry(**{"for": r"ch=7\.1"}),  # picks a again
        ]
        result = resolve_selection(tracks, entries)
        assert _names(result) == ["a", "c"]
