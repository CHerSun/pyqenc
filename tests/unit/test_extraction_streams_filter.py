"""Tests for ``streams_filter_plain_regex`` (extraction phase stream filtering).

Covers include-only, exclude-only, and combined include/exclude patterns, plus
the precedence and case-insensitivity rules.

Migrated from the old ``test_audio.py`` (audio-chains Task 10) — these exercise
the still-existing extraction-phase behaviour, not the removed audio strategies.
"""
# CHerSun 2026

from __future__ import annotations

from typing import ClassVar


class _FakeStream:
    """Minimal stand-in for StreamBase — only display_name() is needed."""

    def __init__(self, name: str) -> None:
        self._name = name

    def display_name(self, *_: int) -> str:
        return self._name


def _fake_tracks(names: list[str]) -> list[_FakeStream]:  # type: ignore[return]
    return [_FakeStream(n) for n in names]


class TestStreamsFilterPlainRegex:
    _TRACKS: ClassVar[list[str]] = [
        "#01 ID=1 (video-h264) lang=eng res=1920x1080 start=0.0.mkv",
        "#02 ID=2 (audio-ac3) lang=eng ch=5.1(side) start=0.028.mka",
        "#03 ID=3 (audio-ac3) lang=rus ch=5.1(side) start=0.028.mka",
        "#04 ID=4 (subtitle-subrip) lang=eng start=0.0.srt",
        "#05 ID=5 (subtitle-subrip) lang=rus start=0.0.srt",
    ]

    def _filter(
        self,
        include: str | None = None,
        exclude: str | None = None,
    ) -> list[str]:
        from pyqenc.phases.extraction import streams_filter_plain_regex as _f
        tracks = _fake_tracks(self._TRACKS)
        result = _f(tracks, include_pattern=include, exclude_pattern=exclude)  # type: ignore[arg-type]
        return [t.display_name() for t in result]

    def test_no_filters_returns_all(self) -> None:
        assert self._filter() == self._TRACKS

    def test_include_only_audio(self) -> None:
        result = self._filter(include=r"audio")
        assert all("audio" in r for r in result)
        assert len(result) == 2

    def test_exclude_rus(self) -> None:
        result = self._filter(exclude=r"lang=rus")
        assert all("lang=rus" not in r for r in result)
        assert len(result) == 3

    def test_include_audio_exclude_rus(self) -> None:
        result = self._filter(include=r"audio", exclude=r"lang=rus")
        assert result == [self._TRACKS[1]]  # only eng audio

    def test_exclude_takes_precedence_over_include(self) -> None:
        # include everything, but exclude video
        result = self._filter(include=r".*", exclude=r"video")
        assert all("video" not in r for r in result)

    def test_include_pattern_case_insensitive(self) -> None:
        result = self._filter(include=r"AUDIO")
        assert len(result) == 2

    def test_empty_include_pattern_matches_all(self) -> None:
        # An empty string pattern matches everything
        result = self._filter(include=r"")
        assert result == self._TRACKS

    def test_include_video_only(self) -> None:
        result = self._filter(include=r"video")
        assert len(result) == 1
        assert "video" in result[0]

    def test_exclude_all_subtitles(self) -> None:
        result = self._filter(exclude=r"subtitle")
        assert all("subtitle" not in r for r in result)
        assert len(result) == 3

    def test_no_match_include_returns_empty(self) -> None:
        result = self._filter(include=r"nonexistent_codec")
        assert result == []
