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

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
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
    SUCCESS_SYMBOL_MINOR,
    THICK_LINE,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import Fingerprint, PhaseOutcome, identity_changed
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.state import ArtifactState
from pyqenc.stream_model import AudioOutput, AudioStream, File
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.fs import remove_stale_tmp_files, safe_stat_size
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


# ---------------------------------------------------------------------------
# audio.yaml — the committed chain records (re-homed sidecar, Req 21)
# ---------------------------------------------------------------------------

class AudioChainRecord(BaseModel):
    """One committed chain: its identity fingerprint + output extension.

    The fingerprint is the invalidation key (Req 10b); the extension is the
    name-composition fact composed-name deletions need (the fingerprint
    alone cannot rebuild an output's name).
    """

    model_config = ConfigDict(frozen=True)

    fingerprint: Fingerprint
    extension:   str


class AudioSidecar(BaseModel):
    """Sidecar model for ``audio.yaml``.

    Records the committed **intent** per chain (identity + naming fact),
    keyed by chain name, plus the source identity key. ``select`` is
    deliberately NOT persisted: selection is a pure function of the current
    extracted tracks plus the current ``select`` config, recomputed for free
    every run. The sidecar never reconstructs a
    :class:`~pyqenc.audio.chain.ResolvedChain` from a record — the token is
    opaque by design; invalidation compares fingerprints, deletions compose
    names from the live chain or the record's extension.

    On-disk shape (``audio.yaml``)::

        chains:
          normal: {fingerprint: {token: "9f2c…"}, extension: flac}
        source: {size: …, token: "…"}

    Attributes:
        chains: Map of chain name → its committed record.
        source: The source identity key (mismatch = catastrophic, Req 33;
                ``None`` on legacy files is unknown, never a mismatch).
    """

    chains: dict[str, AudioChainRecord] = Field(default_factory=dict)
    source: Fingerprint | None          = None

    @classmethod
    def from_resolved(
        cls,
        resolved: dict[str, ResolvedChain],
        source:   Fingerprint | None = None,
    ) -> Self:
        """Build an ``AudioSidecar`` from resolved chains.

        Each record carries the chain's own fingerprint (the SAME derivation
        the phase compares with — DRY) and its effective output extension.

        Args:
            resolved: Map of chain name → :class:`ResolvedChain`.
            source:   The source identity key at the writing site.

        Returns:
            The sidecar holding one record per chain.
        """
        return cls(
            chains = {
                name: AudioChainRecord(
                    fingerprint = chain.fingerprint,
                    extension   = chain.encode.extension,
                )
                for name, chain in resolved.items()
            },
            source = source,
        )

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``audio.yaml``; ``None`` when absent or unparseable."""
        return load_model(path, cls)

    def save(self, path: Path) -> None:
        """Write this ``AudioSidecar`` to *path* atomically."""
        save_model(path, self)


def _composed_output_names(
    tracks:      list[AudioStream],
    chain_name:  str,
    extensions:  set[str],
) -> set[str]:
    """The exact output names for (track set x chain x extensions).

    Deletions' name-composition helper: composed through the entity composer
    (:func:`chain_output_path`) — the single naming site (no manual joins).

    Args:
        tracks:     The working track set.
        chain_name: The chain whose outputs are addressed.
        extensions: The output extensions to cover (a changed chain deletes
                    under both its old and new extension).

    Returns:
        The composed bare file names.
    """
    return {
        chain_output_path(stream, chain_name, ext, Path()).name
        for stream in tracks
        for ext in extensions
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

    name:        str       = "audio"
    SIDECAR_NAME = "audio.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    _METRIC_KEY: MetricKey = MetricKey.AUDIO

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the configured chain / select counts (key parameters)."""
        audio_cfg = self._config.audio
        logger.info("Chains:  %d configured", len(audio_cfg.chains))
        if audio_cfg.select:
            logger.info("Select:  %d entr(y/ies)", len(audio_cfg.select))

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
        job_result  = self._deps[JobPhase]
        work_dir     = job_result.work_dir
        sidecar_path = work_dir / AudioPhase.SIDECAR_NAME
        audio_cfg    = job_result.config.audio

        tracks   = self._selected_tracks()
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir = self._output_dir(tracks, work_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        persisted_audio = AudioSidecar.load(sidecar_path)
        if (
            persisted_audio is not None
            and identity_changed(persisted_audio.source, job_result.source_fingerprint)
        ):
            if not job_result.force:
                raise RecoveryError(
                    "Source content identity mismatch (audio.yaml) — the chain "
                    "outputs belong to a different source.  Re-run with --force "
                    "to grant permission to wipe them and reprocess the new source."
                )
            logger.warning(
                "Source identity mismatch (--force granted — wiping the audio "
                "dir and audio.yaml)"
            )
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
        work_dir    = job_result.work_dir
        audio_cfg   = job_result.config.audio

        tracks   = self._selected_tracks()
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir = self._output_dir(tracks, work_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        remove_stale_tmp_files(audio_dir)

        listing = {f.name for f in audio_dir.iterdir() if f.is_file()}
        return Recovery.from_artifacts(self._classify(audio_dir, tracks, resolved, listing))

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
        audio_streams: list[AudioStream] = [
            a.payload for a in extraction_result.audio_streams
        ]
        audio_cfg = self._deps[JobPhase].config.audio
        return resolve_selection(audio_streams, audio_cfg.select)

    def _invalidate_and_commit(
        self,
        audio_dir:    Path,
        sidecar_path: Path,
        resolved:     dict[str, ResolvedChain],
        tracks:       list[AudioStream],
    ) -> None:
        """Delete outputs of differing/removed chains BY COMPOSED NAME, then commit.

        Comparisons are per-chain fingerprint; deletions compose the exact
        expected output names from the current track set through the entity
        composer (``chain_output_path``) — never a directory listing, never
        name parsing (spec nuance 4). A changed chain deletes under BOTH its
        persisted and current extension (the outputs on disk were named by
        the old chain); a removed chain under its persisted one. The updated
        sidecar is written **before any output is produced**; when nothing
        differs the rewrite is skipped.

        Args:
            audio_dir:    The dedicated audio output directory.
            sidecar_path: The ``audio.yaml`` path.
            resolved:     Current resolved chains, keyed by name.
            tracks:       The working track set (deletion scope).
        """
        persisted = AudioSidecar.load(sidecar_path)
        prior     = persisted.chains if persisted is not None else {}

        current = AudioSidecar.from_resolved(
            resolved, source=self._deps[JobPhase].source_fingerprint,
        )
        current_records = current.chains

        def _delete(names: set[str], reason: str) -> None:
            for name in sorted(names):
                path = audio_dir / name
                try:
                    path.unlink()
                    logger.debug("Deleted invalidated output: %s (%s)", name, reason)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path, exc)

        # Chains whose fingerprint changed → invalidate (reproduce) — delete
        # under both the persisted and the current extension.
        for chain_name, record in current_records.items():
            if chain_name not in prior:
                continue
            if prior[chain_name].fingerprint == record.fingerprint:
                continue
            logger.info(
                "Chain %r changed — invalidating its outputs for reprocessing",
                chain_name,
            )
            _delete(
                _composed_output_names(tracks, chain_name, {
                    prior[chain_name].extension, record.extension,
                }),
                reason="chain changed",
            )

        # Chains removed from config → invalidate (cleanup, now unwanted).
        for chain_name, record in prior.items():
            if chain_name in current_records:
                continue
            logger.info(
                "Chain %r removed from config — cleaning up its outputs", chain_name,
            )
            _delete(
                _composed_output_names(tracks, chain_name, {record.extension}),
                reason="chain removed",
            )

        # Commit the current chain records before producing. Skip the rewrite
        # when the sidecar already matches exactly.
        if prior != current_records:
            current.save(sidecar_path)
            logger.debug("Committed audio sidecar (%d chain(s)) before producing", len(resolved))

    def _classify(
        self,
        audio_dir: Path,
        tracks:    list[AudioStream],
        resolved:  dict[str, ResolvedChain],
        listing:   set[str],
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

        for stream in tracks:
            assert stream.info.layout is not None, "layout guaranteed by ExtractionPhase"
            for name, chain in resolved.items():
                out = chain_output_path(stream, name, chain.encode.extension, audio_dir)
                expected_names.add(out.name)
                rows.append(Artifact(
                    payload = AudioOutput(stream=stream, chain_name=name, output_path=out),
                    state   = (
                        ArtifactState.COMPLETE
                        if out.name in listing else ArtifactState.ABSENT
                    ),
                ))

        # Surface present-but-unwanted surplus files (the same listing minus
        # every consumed name).
        for name in sorted(listing - expected_names):
            path = audio_dir / name
            rows.append(Artifact(
                payload = File(path=path, file_size_bytes=safe_stat_size(path)),
                state   = ArtifactState.COMPLETE,
                wanted  = False,
            ))

        return rows

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> AudioPhaseResult:
        """Produce every pending (track, chain) output via the chain executor.

        Pending artifacts (ABSENT / PARTIAL) are produced one at a time through
        :func:`~pyqenc.audio.chain.execute_chain`, advancing a count-based
        :class:`ProgressBar` (total = pending job count). A
        ``passthrough`` chain raises ``NotImplementedError`` and a failing chain
        raises ``ChainExecutionError``; both are caught per-job and surfaced as a
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
        artifacts  = wanted
        pending    = [a for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)]
        job_result = self._deps[JobPhase]
        audio_cfg  = job_result.config.audio
        resolved   = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir  = job_result.work_dir / AUDIO_OUTPUT_DIR

        if pending:
            logger.info("Sources: %d (track, chain) output(s) to produce", len(pending))

        produced = 0
        failed   = 0
        with ProgressBar(total=len(pending), title="AUDIO", total_count=len(pending)) as advance:
            for art in pending:
                output = art.payload
                label = f"[{output.chain_name}] {output.stream.file.path.stem}"
                logger.debug("Producing %s", label)
                try:
                    self._produce_one(output, resolved[output.chain_name], audio_dir)
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

        reused = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE) - produced
        logger.info(
            "%s Audio complete: %d produced, %d reused, %d failed",
            SUCCESS_SYMBOL_MINOR, produced, max(reused, 0), failed,
        )
        logger.info(THICK_LINE)

        if produced == 0 and failed > 0:
            err = f"all {failed} audio chain output(s) failed"
            return self._make_result(PhaseOutcome.FAILED, artifacts, err)

        outcome = PhaseOutcome.COMPLETED
        return self._make_result(
            outcome,
            artifacts,
            f"produced {produced}, reused {max(reused, 0)}, failed {failed}",
        )

    def _produce_one(self, output: AudioOutput, chain: ResolvedChain, output_dir: Path) -> None:
        """Execute one (track, chain) job, writing the output's delivery file.

        Runs the async chain executor to completion. The executor enforces the
        ``.tmp``-then-rename protocol and the correct output container muxer,
        and writes into the phase's dedicated ``output_dir``.

        Args:
            output:     The pending row's payload (the source track + chain).
            chain:      The resolved chain to apply.
            output_dir: The dedicated audio output directory.

        Raises:
            NotImplementedError: For a ``passthrough`` chain.
            ChainExecutionError: When a measurement or application pass fails.
        """
        asyncio.run(execute_chain(chain, output.stream, output_dir))

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
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
            outcome   = outcome,
            message   = message,
            outputs   = outputs,
        )


# ---------------------------------------------------------------------------
# AudioPhase module-level helpers
# ---------------------------------------------------------------------------
