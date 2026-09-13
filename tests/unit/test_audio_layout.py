"""Unit tests for ``ChannelLayout`` and ``AudioMetadata.selector_string()``.

Each test names the concrete bug it guards against, per project standards, and
checks observable behaviour (parsed fields, the conventional string), never
internal representation.
"""
# CHerSun 2026

from pathlib import Path

from pyqenc.audio.layout import ChannelLayout
from pyqenc.models import AudioMetadata


class TestChannelLayoutNormalization:
    """Parsing maps aliases and strips qualifiers to a canonical normalized form."""

    def test_stereo_and_20_normalize_equal(self) -> None:
        # Bug: treating `stereo` and `2.0` as different layouts would make a
        # stereo source look like it needs a downmix (or mis-scale bitrate),
        # since sources tag the same 2-channel layout both ways.
        stereo = ChannelLayout.parse("stereo")
        two_oh = ChannelLayout.parse("2.0")
        assert stereo.normalized == two_oh.normalized == "2.0"
        assert stereo.channels == two_oh.channels == 2

    def test_51_side_qualifier_stripped_in_normalized(self) -> None:
        # Bug: leaving the `(side)` qualifier in the normalized value breaks
        # matrix lookup and the downmix-only comparison, which key on the base
        # `5.1` layout.
        layout = ChannelLayout.parse("5.1(side)")
        assert layout.normalized == "5.1"
        assert layout.channels == 6

    def test_51_side_original_preserved(self) -> None:
        # Bug: normalizing away the qualifier in `original` would make the user's
        # `ch=5.1(side)` select regex fail to match the faithful source layout.
        layout = ChannelLayout.parse("5.1(side)")
        assert layout.original == "5.1(side)"

    def test_71_channel_count(self) -> None:
        # Bug: a wrong 7.1 channel count mis-scales per-channel bitrate.
        layout = ChannelLayout.parse("7.1")
        assert layout.normalized == "7.1"
        assert layout.channels == 8

    def test_mono_alias_and_count(self) -> None:
        # Bug: an unknown `mono` token defaulting to stereo would over-count a
        # single-channel source.
        layout = ChannelLayout.parse("mono")
        assert layout.normalized == "1.0"
        assert layout.channels == 1

    def test_str_renders_original_token(self) -> None:
        # Bug: rendering the normalized form in filenames/tags would drop the
        # faithful source qualifier that select regexes rely on.
        assert str(ChannelLayout.parse("5.1(side)")) == "5.1(side)"


class TestSelectorString:
    """``selector_string()`` builds the conventional, regex-friendly track string."""

    @staticmethod
    def _meta(language: str, raw_layout: str, title: str | None = None) -> AudioMetadata:
        return AudioMetadata(
            path     = Path("/fake/track.mka"),
            language = language,
            layout   = ChannelLayout.parse(raw_layout),
            title    = title,
        )

    def test_ch_token_uses_original_layout_71(self) -> None:
        # Bug: a `ch=` token built from the normalized layout would not match a
        # user regex written against the faithful source token.
        s = self._meta("eng", "7.1").selector_string()
        assert "lang=eng" in s
        assert "ch=7.1" in s

    def test_ch_token_preserves_51_side_qualifier(self) -> None:
        # Bug: dropping `(side)` from `ch=` breaks faithful-layout matching.
        s = self._meta("rus", "5.1(side)", title="Surround").selector_string()
        assert "ch=5.1(side)" in s
        assert "title=Surround" in s

    def test_ch_token_for_20_and_stereo_both_show_original(self) -> None:
        # Bug: the conventional string must show the faithful source token so a
        # regex can distinguish (or deliberately match) how the source tagged it.
        assert "ch=2.0" in self._meta("eng", "2.0").selector_string()
        assert "ch=stereo" in self._meta("eng", "stereo").selector_string()

    def test_title_omitted_when_absent(self) -> None:
        # Bug: emitting an empty `title=` token invites accidental regex matches
        # on tracks that have no title.
        s = self._meta("eng", "5.1").selector_string()
        assert "title=" not in s

    def test_tokens_are_space_separated(self) -> None:
        # Bug: tokens run together would let a `ch=` regex bleed into the title.
        s = self._meta("eng", "5.1", title="Main").selector_string()
        assert s == "lang=eng ch=5.1 title=Main"
