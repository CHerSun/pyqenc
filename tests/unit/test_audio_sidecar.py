"""Unit tests for :class:`AudioSidecar` load/save round-trip and invalidation.

Each test names the concrete bug it guards against and checks observable
behaviour — the compact per-chain **signature map** surviving save/load, a
different signature when a filter param changes, a dropped entry when a chain is
removed, and the ``.tmp``-then-rename guarantee — never internal serialisation
details.

Invalidation (Req 9.2, 9.3, 9.4) is driven by comparing each chain's canonical
signature string against the persisted one. These tests pin that behaviour
because the audio phase relies on it: a changed filter param inside a chain
MUST yield a different signature so the phase reprocesses; a removed chain MUST
drop from the map so the phase cleans it up; an unchanged chain MUST keep an
identical signature so its output is reused.
"""
# CHerSun 2026

from pathlib import Path

from pyqenc.app_config import ChainSpec, FilterInstance
from pyqenc.audio.chain import chain_signature, resolve_chain
from pyqenc.state import AudioSidecar


def _fi(type_id: str, **params) -> FilterInstance:
    """Build a validated config-side ``FilterInstance`` from raw params."""
    return FilterInstance(type=type_id, **params)  # type: ignore[arg-type]


def _palette() -> dict[str, FilterInstance]:
    """A small realistic palette covering a filter, a downmix, and an encode."""
    return {
        "dyn":   _fi("dynaudnorm", framelen=150, gausssize=15, peak=0.9, maxgain=9.0, targetrms=0.0),
        "peak":  _fi("peaknorm", target_dbfs=-1.0),
        "down":  _fi("downmix", to="2.0", matrix="std"),
        "aac":   _fi("encode", codec="aac", bitrate_per_channel="64k", extension="m4a"),
    }


def _sidecar(*chains: tuple[str, list[str]]) -> AudioSidecar:
    """Build an :class:`AudioSidecar` by resolving each ``(name, filter_order)``."""
    palette = _palette()
    resolved = {
        name: resolve_chain(ChainSpec(name=name, filters=order), palette)
        for name, order in chains
    }
    return AudioSidecar.from_resolved(resolved)


class TestRoundTrip:
    """Save → load restores every chain, filter param, and encode target."""

    def test_round_trip_preserves_signatures(self, tmp_path: Path) -> None:
        """A saved sidecar reloads with an identical signature map.

        Bug: if the compact signature string did not survive save/load intact
        (e.g. YAML mangling), every chain would falsely look "changed" on the
        next run, reprocessing everything.
        """
        original = _sidecar(
            ("night",  ["peak", "down", "aac"]),
            ("normal", ["dyn"]),
        )
        path = tmp_path / "audio.yaml"
        original.save(path)

        restored = AudioSidecar.load(path)
        assert restored is not None
        assert restored == original
        assert restored.signatures == original.signatures
        assert set(restored.signatures) == {"night", "normal"}

    def test_round_trip_flac_default_chain(self, tmp_path: Path) -> None:
        """A norm-only chain's signature is stable across save/load.

        Bug: the implicit FLAC terminal target (not a real filter) could shift the
        signature across a round-trip, so a norm-only chain would reload with a
        different signature and be treated as changed.
        """
        original = _sidecar(("normal", ["dyn"]))
        path = tmp_path / "audio.yaml"
        original.save(path)

        restored = AudioSidecar.load(path)
        assert restored is not None
        # The signature is a stable string; FLAC (no encode) is baked into it.
        assert "flac" in restored.signatures["normal"]
        assert restored == original


class TestInvalidationSignatures:
    """Signatures detect a changed filter param and a removed chain (Req 9.2–9.4)."""

    def test_changed_filter_param_yields_different_signature(self, tmp_path: Path) -> None:
        """Changing a filter param inside a chain yields a different signature string.

        Bug: if the signature did not incorporate inlined filter params (or keyed
        only on name), a user tuning ``peaknorm.target_dbfs`` would not trigger
        reprocessing — the phase would reuse a stale output.
        """
        saved = _sidecar(("night", ["peak", "aac"]))
        path = tmp_path / "audio.yaml"
        saved.save(path)
        persisted = AudioSidecar.load(path)
        assert persisted is not None

        # Same chain name, one filter param changed.
        changed_palette = _palette()
        changed_palette["peak"] = _fi("peaknorm", target_dbfs=-3.0)
        current_sig = chain_signature(resolve_chain(
            ChainSpec(name="night", filters=["peak", "aac"]),
            changed_palette,
        ))

        assert current_sig != persisted.signatures["night"]

    def test_removed_chain_drops_from_signature_map(self, tmp_path: Path) -> None:
        """Removing a chain drops its entry from the persisted signature map.

        Bug: if a removed chain lingered in the map, a chain deleted from config
        would not be recognised as removed and its stale output would never be
        cleaned up.
        """
        saved = _sidecar(
            ("night",  ["peak", "aac"]),
            ("normal", ["dyn"]),
        )
        path = tmp_path / "audio.yaml"
        saved.save(path)
        persisted = AudioSidecar.load(path)
        assert persisted is not None

        current = _sidecar(("normal", ["dyn"]))

        assert current.signatures != persisted.signatures
        assert "night" in persisted.signatures
        assert "night" not in current.signatures

    def test_unchanged_chain_keeps_identical_signature(self, tmp_path: Path) -> None:
        """An identically-resolved chain keeps the same signature so it is reused.

        Bug: spurious signature drift (e.g. from non-deterministic serialisation)
        would reprocess an unchanged chain on every run.
        """
        saved = _sidecar(("night", ["peak", "down", "aac"]))
        path = tmp_path / "audio.yaml"
        saved.save(path)
        persisted = AudioSidecar.load(path)
        assert persisted is not None

        current_sig = chain_signature(resolve_chain(
            ChainSpec(name="night", filters=["peak", "down", "aac"]),
            _palette(),
        ))
        assert current_sig == persisted.signatures["night"]


class TestAtomicWriteAndRecovery:
    """The sidecar is written atomically and load handles missing/corrupt files."""

    def test_load_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        """load() returns None when the file does not exist.

        Bug: a missing sidecar could raise instead of returning None, breaking
        first-run recovery which uses None to mean "nothing committed yet".
        """
        assert AudioSidecar.load(tmp_path / "audio.yaml") is None

    def test_save_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        """After save() no ``.tmp`` file remains (Req 9.7 atomic write).

        Bug: a leftover ``.tmp`` from a non-atomic write would corrupt recovery
        on the next run.
        """
        _sidecar(("night", ["peak", "aac"])).save(tmp_path / "audio.yaml")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        """save() creates missing parent directories.

        Bug: saving into a not-yet-created work-dir subfolder would raise
        FileNotFoundError.
        """
        nested = tmp_path / "work" / "audio.yaml"
        _sidecar(("normal", ["dyn"])).save(nested)
        assert nested.exists()

    def test_load_returns_none_for_invalid_yaml(self, tmp_path: Path) -> None:
        """load() returns None (not raise) for a corrupt file.

        Bug: a corrupt audio.yaml could abort the whole run instead of triggering
        a clean re-derivation of the sidecar.
        """
        path = tmp_path / "audio.yaml"
        path.write_text("not: valid: yaml: [unclosed", encoding="utf-8")
        assert AudioSidecar.load(path) is None
