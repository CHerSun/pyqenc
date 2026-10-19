"""
Audio processing phase for the quality-based encoding pipeline.

Drives the configured audio *chains* over the selected source tracks: each
(track, chain) pair produces one deterministically-named output. Chain
resolution, selection, and the generic filter executor live in the
``pyqenc.audio`` package; this module owns the :class:`AudioPhase` object that
recovers, invalidates, produces, and reports those outputs following the Phase
pattern.
"""
# CHerSun 2026

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import BaseModel, Field

from pyqenc.utils.log_format import fmt_key_value_table

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig, SelectEntry
    from pyqenc.metrics import MetricsCollector

from pyqenc.audio.chain import (
    ChainExecutionError,
    ResolvedChain,
    chain_output_path,
    execute_chain,
    resolve_chain,
)
from pyqenc.audio.select import resolve_selection
from pyqenc.constants import (
    AUDIO_OUTPUT_DIR,
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import FingerprintYaml, PhaseOutcome, SizedFingerprint, SizedFingerprintYaml
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
    claim_expected_name,
)
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.state import ArtifactState
from pyqenc.stream_model import AudioOutput, AudioStream, File
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.concurrency import run_bounded
from pyqenc.utils.fs import remove_stale_tmp_files
from pyqenc.utils.yaml_utils import load_model, save_model

logger = logging.getLogger(__name__)


@dataclass
class AudioPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying the audio phase's typed outputs.

    ``outputs`` is the single storage — one ``Artifact[AudioOutput]`` per
    expected (track, chain) row — driving the standard ``pending`` /
    ``complete`` / ``is_complete`` machinery so MergePhase (which lists
    AudioPhase purely for ordering) resolves the dependency as COMPLETE /
    REUSED.

    Attributes:
        outputs: Typed chain-output rows (all wanted outputs).
    """

    outputs: list[Artifact[AudioOutput]] = field(default_factory=list)


class AudioSidecar(BaseModel):
    """Sidecar model for ``audio.yaml``.

    Records the committed **intent** per chain (its identity token), keyed
    by chain name, plus the source identity key. ``select`` is
    deliberately NOT persisted: selection is a pure function of the current
    extracted tracks plus the current ``select`` config, recomputed for free
    every run. The sidecar never reconstructs a
    :class:`~pyqenc.audio.chain.ResolvedChain` from a record — the token is
    opaque by design; invalidation compares tokens. No output-extension
    fact is persisted (dropped 2026-10-10): stale outputs are swept by the
    listing protocol (one listing, expected names of the live chains,
    leftovers deleted — Req 12-13), so nothing needs to remember how old
    files were named.

    On-disk shape (``audio.yaml``) — fingerprints persist as their
    one-line storage strings (:func:`str` on :class:`Fingerprint`)::

        source: <sized storage string>
        chains:
          normal: <token-only storage string>

    Attributes:
        source: The source identity key (mismatch = catastrophic, Req 33) —
                required: every write path carries it, and a sidecar missing
                it fails validation → absent → the phase redoes its work.
        chains: Map of chain name → its committed chain fingerprint.
    """

    source:  SizedFingerprintYaml
    chains: dict[str, FingerprintYaml] = Field(default_factory=dict)

    @classmethod
    def from_resolved(
        cls,
        resolved: dict[str, ResolvedChain],
        source: SizedFingerprint,
    ) -> Self:
        """Build an ``AudioSidecar`` from resolved chains.

        Each entry carries the chain's own fingerprint (the SAME derivation
        the phase compares with — DRY).

        Args:
            resolved: Map of chain name → :class:`ResolvedChain`.
            source:   The source identity key at the writing site.

        Returns:
            The sidecar holding one fingerprint per chain.
        """
        return cls(
            chains={name: chain.fingerprint for name, chain in resolved.items()},
            source=source,
        )


def _expected_output_names(
    tracks: list[AudioStream],
    resolved: dict[str, ResolvedChain],
) -> set[str]:
    """The exact expected output names for (track set x live chains).

    The single composition site for both the orphan sweep and the row
    classification: composed through the entity composer
    (:func:`chain_output_path`) — the naming site, no manual joins.

    Args:
        tracks:   The working track set.
        resolved: Current resolved chains, keyed by name.

    Returns:
        The composed bare file names.
    """
    return {
        chain_output_path(stream, name, chain.encode.extension, Path()).name
        for stream in tracks
        for name, chain in resolved.items()
    }


class AudioPhase(Phase[AudioPhaseResult]):
    """Phase object for audio stream processing.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the audio phase. Drives the configured chains over the selected source
    tracks via the ``pyqenc.audio`` chain executor. The uniform run footprint
    (memoization, dependencies, banner, timed recovery, dry-run / reused
    branches, timed execution) is inherited from :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "audio"
    SIDECAR_NAME = "audio.yaml"
    DEPENDS_ON: ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    _METRIC_KEY: MetricKey = MetricKey.AUDIO

    def __init__(
        self,
        config: AppConfig,
        phases: PhaseRegistry,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the audio phase key input params."""
        audio_cfg = self._config.audio

        kv: dict[str, object] = {
            "Processing chains:" : len(audio_cfg.chains),
        }
        if audio_cfg.concurrency > 1:
            kv["Concurrency:"] = audio_cfg.concurrency
        fmt_key_value_table(kv)

    # ------------------------------------------------------------------
    # Recovery + invalidation
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """Key-triggered effects over the audio outputs (disk effects only).

        Identity key (Req 33): a persisted identity contradicting the live
        source is catastrophic — fatal without permission; with it, wipe the
        audio dir and the sidecar. Unknown currency (Req 47, A-1): a MISSING
        sidecar while the audio dir holds files means nothing proves what
        produced them — the conservative wipe is automatic (vacuous when
        nothing exists). Chain invalidation (Req 15/46, nuance 4): differing
        and removed chains' outputs are deleted by their exact COMPOSED
        names (current track set x the entity composer — no directory
        parsing, no listing), and the sidecar is committed BEFORE producing
        when it differs.

        Raises:
            RecoveryError: On an identity mismatch without ``--force``.
        """
        job_result = self._deps[JobPhase]
        work_dir = job_result.work_dir
        sidecar_path = work_dir / AudioPhase.SIDECAR_NAME
        audio_cfg = job_result.config.audio

        tracks = self._selected_tracks()
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir = self._output_dir(tracks, work_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        persisted_audio = load_model(sidecar_path, AudioSidecar)
        if persisted_audio is not None and persisted_audio.source != job_result.file.payload.fingerprint:
            if not job_result.force:
                raise RecoveryError(
                    "Source content identity mismatch (audio.yaml) — the chain "
                    "outputs belong to a different source.  Re-run with --force "
                    "to grant permission to wipe them and reprocess the new source."
                )
            logger.warning("Source identity mismatch (--force granted — wiping the audio dir and audio.yaml)")
            self._wipe_audio_dir(audio_dir, sidecar_path)
        elif persisted_audio is None:
            # Unknown currency (Req 47, A-1): no record of what produced the
            # files — the conservative wipe fires on the missing sidecar
            # ALONE (vacuous when nothing exists; no existence probe, no
            # listing — spec nuance 2).
            logger.info(
                "audio.yaml missing — chain-output currency unknown; wiping "
                "the audio dir (outputs reproduce from the source)"
            )
            self._wipe_audio_dir(audio_dir, sidecar_path)

        self._invalidate_and_commit(audio_dir, sidecar_path, resolved, tracks)

    def _recover(self) -> Recovery:
        """Classification from ONE listing of the audio dir (Req 15, §103).

        Runs after :meth:`_invalidate` settled the outputs on disk: the
        single listing feeds BOTH the per-(track, chain) membership
        classification and the surplus scan (present names no row consumed —
        retained deliverables). Selection (``select``) is recomputed live
        every run and never persists.
        """
        job_result = self._deps[JobPhase]
        work_dir = job_result.work_dir
        audio_cfg = job_result.config.audio

        tracks = self._selected_tracks()
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir = self._output_dir(tracks, work_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        remove_stale_tmp_files(audio_dir)

        listing = {f.name for f in audio_dir.iterdir() if f.is_file()}
        rows = self._classify(audio_dir, tracks, resolved, listing)
        all_streams = [a.payload for a in self._deps[ExtractionPhase].audio_streams]
        if all_streams:
            _log_selector(audio_cfg.select)
        _log_track_table(all_streams, tracks, rows)
        return Recovery.from_artifacts(rows)

    def _output_dir(self, tracks: list[AudioStream], work_dir: Path) -> Path:
        """Return the phase's dedicated audio output directory (``work_dir/audio``).

        All chain outputs, deletion, ``.tmp``-cleanup, surplus-scanning, and
        production operate on this one phase-owned directory regardless of
        where the source tracks live.

        Args:
            tracks:   Unused (the location depends only on *work_dir*).
            work_dir: The job work directory.

        Returns:
            The dedicated audio output directory.
        """
        return work_dir / AUDIO_OUTPUT_DIR

    @staticmethod
    def _wipe_audio_dir(audio_dir: Path, sidecar_path: Path) -> None:
        """Wipe the phase's own output dir and the sidecar.

        Used by the catastrophic identity branch and the conservative
        unknown-currency branch — both end in a full reproduce from the
        source, so nothing in the dir is worth keeping. The wipe is vacuous
        when the dir is empty.
        """
        shutil.rmtree(audio_dir, ignore_errors=True)
        sidecar_path.unlink(missing_ok=True)

    def _selected_tracks(self) -> list[AudioStream]:
        """Resolve the working track set from extraction + ``audio.select``."""
        extraction_result = self._deps[ExtractionPhase]
        audio_streams: list[AudioStream] = [a.payload for a in extraction_result.audio_streams]
        audio_cfg = self._deps[JobPhase].config.audio
        return resolve_selection(audio_streams, audio_cfg.select)

    def _invalidate_and_commit(
        self,
        audio_dir: Path,
        sidecar_path: Path,
        resolved: dict[str, ResolvedChain],
        tracks: list[AudioStream],
    ) -> None:
        """Commit the current chain fingerprints, sweeping orphaned outputs.

        The listing protocol (Req 12-13): ONE directory listing, expected
        names composed from the live (tracks × chains), every leftover
        deleted — outputs of changed chains (any extension they carried),
        removed chains, and deselected tracks alike; nothing needs to
        remember how stale files were named. Comparisons stay per-chain
        fingerprint (changed chains log their reprocessing; a same-name
        change is reproduced in place via the atomic .tmp→rename
        promotion). The updated sidecar is written **before any output is
        produced**; when nothing differs the rewrite is skipped.

        Args:
            audio_dir:    The dedicated audio output directory.
            sidecar_path: The ``audio.yaml`` path.
            resolved:     Current resolved chains, keyed by name.
            tracks:       The working track set (expected-name scope).
        """
        persisted = load_model(sidecar_path, AudioSidecar)
        prior = persisted.chains if persisted is not None else {}

        current = AudioSidecar.from_resolved(
            resolved,
            source=self._deps[JobPhase].file.payload.fingerprint,
        )
        current_records = current.chains

        changed = [
            name for name, record in current_records.items() if name in prior and prior[name].token != record.token
        ]
        removed = [name for name in prior if name not in current_records]
        for chain_name in changed:
            logger.info(
                "Chain %r changed — invalidating its outputs for reprocessing",
                chain_name,
            )
        for chain_name in removed:
            logger.info(
                "Chain %r removed from config — cleaning up its outputs",
                chain_name,
            )

        def _unlink(name: str) -> None:
            path = audio_dir / name
            try:
                path.unlink()
                logger.debug("Deleted invalidated audio output: %s", name)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", path, exc)

        # Disk effect 1 — a changed chain's stale bytes under its CURRENT
        # (live-composed) name: deletion is what presence-based classification
        # reads as "reproduce" (Req 12 keeps classification sidecar-blind).
        # An extension change renames the outputs — the old names fall to the
        # sweep below; no persisted fact names them.
        for chain_name in changed:
            for name in _expected_output_names(tracks, {chain_name: resolved[chain_name]}):
                _unlink(name)

        # Disk effect 2 — the orphan sweep (Req 12-13): one listing minus the
        # live expected names; every leftover deleted (old extensions,
        # removed chains, deselected tracks).
        if audio_dir.is_dir():
            expected = _expected_output_names(tracks, resolved)
            listing = {p.name for p in audio_dir.iterdir()}
            for name in sorted(listing - expected):
                if name.endswith(TEMP_SUFFIX):
                    continue  # stale .tmp pre-clean owns those
                _unlink(name)

        # Commit the current chain records before producing. Skip the rewrite
        # when the sidecar already matches exactly.
        if prior != current_records:
            save_model(sidecar_path, current)
            logger.debug("Committed audio sidecar (%d chain(s)) before producing", len(resolved))

    def _classify(
        self,
        audio_dir: Path,
        tracks: list[AudioStream],
        resolved: dict[str, ResolvedChain],
        listing: set[str],
    ) -> list[Artifact]:
        """Build one row per expected (track, chain) from the single listing.

        Completion is membership of the composed name in the listing (Req 15,
        §103 — one listing before the rows, no per-output ``.exists()``).
        Expected rows carry an :class:`~pyqenc.stream_model.AudioOutput`
        payload composed at the chain-output materialization site. Any listed
        name that is not an expected output surfaces as present-but-unwanted
        (``COMPLETE``, ``wanted=False``, retained in place) — its chain is
        gone from the config or its track no longer selected.

        Args:
            audio_dir: The dedicated audio output directory.
            tracks:    The working track set.
            resolved:  Current resolved chains, keyed by name.
            listing:   The ONE directory listing (final names, no ``.tmp``).

        Returns:
            The internal ledger (expected outputs + surplus files).
        """

        rows: list[Artifact] = []
        expected_names: set[str] = set()
        claimed: dict[str, str] = {}
        """The listing's claim registry — the Req 5 collision guard."""

        for stream in tracks:
            assert stream.info.layout is not None, "layout guaranteed by ExtractionPhase"
            for name, chain in resolved.items():
                out = chain_output_path(stream, name, chain.encode.extension, audio_dir)
                claim_expected_name(claimed, out.name, f"{stream.display_name()} × chain {name!r}")
                expected_names.add(out.name)
                rows.append(
                    Artifact(
                        payload=AudioOutput(stream=stream, chain_name=name, output_path=out),
                        state=(ArtifactState.COMPLETE if out.name in listing else ArtifactState.ABSENT),
                    )
                )

        # Surface present-but-unwanted surplus files (the same listing minus
        # every consumed name).
        for name in sorted(listing - expected_names):
            path = audio_dir / name
            rows.append(
                Artifact(
                    payload=File(path=path),
                    state=ArtifactState.COMPLETE,
                    wanted=False,
                )
            )

        return rows

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> AudioPhaseResult:
        """Produce every pending (track, chain) output via the bounded runner.

        Pending artifacts (ABSENT / PARTIAL) run as jobs through
        :func:`~pyqenc.utils.concurrency.run_bounded` at
        ``audio.concurrency`` — one :func:`~pyqenc.audio.chain.execute_chain`
        await per job, timed under the ``audio.<chain>`` dotted key, plus its
        bookkeeping on the count-based bar. A ``passthrough`` chain raises
        ``NotImplementedError`` and a failing chain raises
        ``ChainExecutionError``; both are caught per-job and surfaced as a
        FAILED artifact for that output — the phase never crashes on one bad
        chain. ``dry_run`` is never ``True`` here (audio is not
        a readonly-execute phase; the template previews instead).

        Args:
            wanted:  The wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``AudioPhaseResult`` — COMPLETED when work ran (even with some
            failures), FAILED only when nothing could be produced and failures
            occurred.
        """
        artifacts = wanted
        pending = [a for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)]
        job_result = self._deps[JobPhase]
        audio_cfg = job_result.config.audio
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir = job_result.work_dir / AUDIO_OUTPUT_DIR

        if pending:
            logger.info("Sources: %d (track, chain) output(s) to produce", len(pending))

        produced = 0
        failed = 0
        with ProgressBar(len(pending), title="AUDIO", total_count=len(pending)) as advance:

            async def process(art: Artifact) -> None:
                """One (track, chain) job: the chain executor, then bookkeeping."""
                nonlocal produced, failed
                output = art.payload
                chain = resolved[output.chain_name]
                label = f"[{output.chain_name}] {output.stream.file.path.stem}"
                logger.debug("Producing %s", label)
                try:
                    with self._collector.time(MetricKey.AUDIO, output.chain_name):
                        await execute_chain(chain, output.stream, audio_dir)
                    art.state = ArtifactState.COMPLETE
                    produced += 1
                    advance(1, AdvanceState.SUCCESS)
                except NotImplementedError as exc:
                    art.state = ArtifactState.ABSENT
                    failed += 1
                    logger.error("%s — passthrough not implemented: %s", label, exc)
                    advance(1, AdvanceState.FAILED)
                except ChainExecutionError as exc:
                    art.state = ArtifactState.ABSENT
                    failed += 1
                    logger.error("%s — chain failed: %s", label, exc)
                    advance(1, AdvanceState.FAILED)

            asyncio.run(run_bounded(pending, audio_cfg.concurrency, process))

        reused = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE) - produced

        if failed > 0:
            return self._make_result(
                PhaseOutcome.FAILED, artifacts,
                f"{failed} audio chain output(s) failed "
                f"(produced {produced}, reused {max(reused, 0)})",
            )

        return self._make_result(
            PhaseOutcome.COMPLETED, artifacts,
            f"produced {produced}, reused {max(reused, 0)}",
        )

    def _make_result(
        self,
        outcome: PhaseOutcome,
        artifacts: list[Artifact],
        message: str,
    ) -> AudioPhaseResult:
        """Assemble an ``AudioPhaseResult`` from the wanted rows.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted row list (the AudioOutput rows only —
                       surplus rows are wanted=False and stay internal).
            message:   Human-readable summary — on ``FAILED``, the error
                       description.

        Returns:
            The populated result (``outputs`` is the single storage driving
            dependency resolution).
        """
        outputs = [r for r in artifacts if isinstance(r.payload, AudioOutput)]
        return AudioPhaseResult(
            outcome=outcome,
            message=message,
            outputs=outputs,
        )


# ---------------------------------------------------------------------------
# Run-log presentation
# ---------------------------------------------------------------------------

def _log_track_table(
    streams:  list[AudioStream],
    selected: list[AudioStream],
    rows:     list[Artifact],
) -> None:
    """Log a 3-column table over the extraction inventory: want, present, name.

    Columns (orthogonal — neither influences the other):
    - Want:    ``✔`` when the track is in the working track set (selected
               by ``audio.select``) else ``✘``.
    - Present: ``✔`` when every expected chain output of the track is on
               disk (COMPLETE ledger rows) else ``✘`` — a deselected track
               expects no outputs and shows ``✘`` by construction.
    - Name:    The stream's ``display_name()``.

    Args:
        streams:  Every extracted audio stream, in extraction order.
        selected: The working track set (the ``Want`` column's source).
        rows:     The classified ledger (only the ``AudioOutput`` rows are
                  read).
    """
    logger.info("Tracks:")
    logger.info("Want  Present      Name")
    if not streams:
        logger.warning("NO audio tracks in the source.")
        return

    selected_ids = {(s.file.path, s.info.track_id) for s in selected}
    states_by_id: dict[tuple[Path, int], list[ArtifactState]] = {}
    for row in rows:
        payload = row.payload
        if isinstance(payload, AudioOutput):
            key = (payload.stream.file.path, payload.stream.info.track_id)
            states_by_id.setdefault(key, []).append(row.state)
    present_ids = {
        key for key, states in states_by_id.items()
        if all(s == ArtifactState.COMPLETE for s in states)
    }

    for stream in streams:
        key = (stream.file.path, stream.info.track_id)
        w_sym = SUCCESS_SYMBOL_MINOR if key in selected_ids else FAILURE_SYMBOL_MINOR
        p_sym = SUCCESS_SYMBOL_MINOR if key in present_ids else FAILURE_SYMBOL_MINOR
        logger.info("   %s  %s  \"%s\"", w_sym, p_sym, stream.display_name())


def _log_selector(select: list[SelectEntry]) -> None:
    """Log the ``audio.select`` entries — the reason behind the table's picks.

    One line per entry renders the configured regexes verbatim (``for``
    always; ``exclude`` and ``prefer`` when set); an empty select logs the
    pick-everything fallback instead.

    Args:
        select: The ordered ``audio.select`` entries.
    """
    if not select:
        logger.info("Selector: none — all extracted tracks selected")
        return
    logger.info("Selector:")
    for entry in select:
        parts = [f"for={entry.for_}"]
        if entry.exclude:
            parts.append(f"exclude={entry.exclude}")
        if entry.prefer:
            parts.append("prefer: " + ", ".join(entry.prefer))
        logger.info("  %s", "  ".join(parts))
