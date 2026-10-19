"""Unit tests for the yaml write/read chokepoint's UTF-8 BOM contract."""
# CHerSun 2026

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from pyqenc.utils.yaml_utils import load_model, save_model, write_yaml_atomic


class _Note(BaseModel):
    """Minimal sidecar model — one required string field."""

    note: str


class TestBomWriteContract:
    """Every yaml the pipeline writes starts with the UTF-8 BOM."""

    def test_written_yaml_starts_with_bom(self, tmp_path: Path):
        """write_yaml_atomic must emit EF BB BF as the first three bytes.

        Windows tools that guess the ANSI codepage misrender the non-ASCII
        glyphs the formats rely on (the fingerprint storage separator, the
        narrow no-break space in size formatting) when the BOM is absent.
        """
        path = tmp_path / "sidecar.yaml"
        write_yaml_atomic(path, {"note": "αβγ"})
        assert path.read_bytes()[:3] == b"\xef\xbb\xbf"

    def test_saved_model_starts_with_bom(self, tmp_path: Path):
        """save_model must go through the same BOM-writing chokepoint."""
        path = tmp_path / "model.yaml"
        save_model(path, _Note(note="x"))
        assert path.read_bytes()[:3] == b"\xef\xbb\xbf"


class TestBomReadContract:
    """Loads accept BOM'd files — including ones not written by us."""

    def test_bom_prefixed_file_loads(self, tmp_path: Path):
        """load_model must load a hand-saved BOM'd sidecar (utf-8-sig read).

        Users edit sidecars and configs with Windows editors that prepend a
        BOM on save; the load must not treat that as corruption.
        """
        path = tmp_path / "hand-edited.yaml"
        path.write_bytes(b"\xef\xbb\xbf" + b"note: hi\n")
        assert load_model(path, _Note) == _Note(note="hi")

    def test_bomless_file_still_loads(self, tmp_path: Path):
        """utf-8-sig reads must be identical on BOM-less files.

        Files written before the BOM change (and the bundled, git-tracked
        default config) carry no BOM and must keep loading.
        """
        path = tmp_path / "legacy.yaml"
        path.write_text("note: hi\n", encoding="utf-8")
        assert load_model(path, _Note) == _Note(note="hi")

    def test_corrupt_file_loads_as_none(self, tmp_path: Path):
        """load_model must swallow parse failures as None (rebuild contract).

        A BOM is never a parse failure, but a genuinely corrupt file must
        still mean "rebuild", never raise.
        """
        path = tmp_path / "broken.yaml"
        path.write_bytes(b"\xef\xbb\xbf" + b"note: [unclosed\n")
        assert load_model(path, _Note) is None


@given(st.text(alphabet=st.characters(exclude_characters="\u0085\u2028\u2029")))
def test_properties_round_trip_preserves_arbitrary_text(text: str) -> None:
    """save_model → load_model preserves any string, BOM on disk or not.

    The persistence contract is value preservation; arbitrary text —
    including the separators and spaces the yaml formats rely on — must
    survive the write/read cycle unchanged. YAML 1.1's non-ASCII line
    breaks (NEL, LS, PS) are outside the persistence domain: printable
    per the spec, so the dumper emits them raw, yet the loader
    normalizes them as line breaks — no pipeline value carries them.
    """
    with TemporaryDirectory() as td:
        path = Path(td) / "roundtrip.yaml"
        model = _Note(note=text)
        save_model(path, model)
        loaded = load_model(path, _Note)
        assert loaded == model
