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

from __future__ import annotations

import asyncio
import logging

from alive_progress import config_handler

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AudioPhase — Phase object
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from pathlib import Path
from typing import TYPE_CHECKING

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
from pyqenc.audio.layout import ChannelLayout
from pyqenc.audio.select import resolve_selection
from pyqenc.constants import (
    AUDIO_OUTPUT_DIR,
    CHAIN_FILENAME_SUFFIX,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
)
from pyqenc.models import AudioMetadata, PhaseOutcome
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseResult,
    resolve_dependencies,
)
from pyqenc.state import ArtifactState, AudioSidecar
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line
from pyqenc.utils.long_path import LongPath

_AUDIO_YAML = "audio.yaml"

# Default channel layout when a selected track carries no extraction layout.
# Preferred source is always the track's own ``AudioMetadata.layout``; this is a
# graceful fallback so a missing layout never crashes chain execution.
_FALLBACK_LAYOUT_TOKEN = "stereo"


@_dataclass
class AudioArtifact(Artifact):
    """Audio phase artifact for one (source track, chain) output.

    One artifact per expected chain output. ``path`` is the deterministic
    ``<source-stem> chain=<name>.<ext>`` output location; ``state`` reflects
    on-disk presence (COMPLETE when the file exists, ABSENT when it must be
    produced); ``wanted`` marks whether the current config still expects it.

    Attributes:
        source_track: The extracted track this output is produced from.
        chain_name:   The producing chain's configured name.
        out_layout:   The resolved output channel layout (after any downmix).
        codec:        The effective output codec (e.g. ``flac``, ``aac``).
    """

    source_track: AudioMetadata | None = None
    chain_name:   str | None           = None
    out_layout:   ChannelLayout | None = None
    codec:        str | None           = None


@_dataclass
class AudioPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying the audio phase's typed outputs.

    ``artifacts`` (inherited) holds the wanted artifacts, driving the standard
    ``pending`` / ``complete`` / ``is_complete`` machinery so MergePhase — which
    lists AudioPhase purely for ordering — resolves the dependency as
    COMPLETE / REUSED. ``outputs`` is the audio-specific view (one
    :class:`AudioArtifact` per produced chain output). ``audio_files`` is the
    convenience list of the produced delivery-file paths.

    Attributes:
        outputs:     Typed chain-output artifacts (all wanted outputs).
        audio_files: Paths of the produced/present delivery files.
    """

    outputs:     list[AudioArtifact] = _field(default_factory=list)
    audio_files: list[Path]          = _field(default_factory=list)


class AudioPhase:
    """Phase object for audio stream processing.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the audio phase. Drives the configured chains over the selected source
    tracks via the ``pyqenc.audio`` chain executor.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "audio"

    def __init__(
        self,
        config:    AppConfig,
        phases:    dict[type[Phase], Phase] | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        from typing import cast

        from pyqenc.phases.extraction import ExtractionPhase as _ExtractionPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config:     AppConfig               = config
        self._collector:  MetricsCollector        = collector
        self._job:        _JobPhase | None         = cast(_JobPhase,        phases.get(_JobPhase))        if phases else None
        self._extraction: _ExtractionPhase | None = cast(_ExtractionPhase, phases.get(_ExtractionPhase)) if phases else None
        self.result:      AudioPhaseResult | None = None
        self.dependencies: list[Phase]            = [d for d in [self._job, self._extraction] if d is not None]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> AudioPhaseResult:
        """Recover, produce pending (track, chain) outputs, cache result.

        Sequence (mirrors the other phases):

        1. In-run memoization guard.
        2. ``_ensure_dependencies`` — Job, Extraction.
        3. ``emit_phase_banner``.
        4. ``_recover(force_wipe)`` — resolve select + chains, invalidate
           differing/removed chains, write the updated sidecar **before**
           producing, then classify expected (track, chain) outputs vs on-disk.
        5. ``log_recovery_line``.
        6. Dry-run: return REUSED / PENDING without executing.
        7. Execute pending jobs with a count-based ``ProgressBar``.
        8. Emit summary; cache and return.

        Args:
            dry_run: When ``True``, report what would be done without producing
                     files.

        Returns:
            ``AudioPhaseResult`` — COMPLETED / REUSED on success, PENDING in a
            dry-run with pending work, FAILED on a dependency or fatal error.
        """
        # In-run memoization guard: return cached result verbatim.
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(dry_run=dry_run)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        emit_phase_banner("AUDIO", logger)

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        audio_cfg = job_result.config.audio
        logger.info("Chains:  %d configured", len(audio_cfg.chains))
        if audio_cfg.select:
            logger.info("Select:  %d entr(y/ies)", len(audio_cfg.select))

        from pyqenc.metrics import MetricKey

        with self._collector.time(MetricKey.RECOVERY):
            internal_artifacts = self._recover(force_wipe=force_wipe)

        artifacts     = [a for a in internal_artifacts if a.wanted]
        pending       = [a for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)]
        message       = log_recovery_line(logger, internal_artifacts)

        if pending:
            logger.info("Sources: %d (track, chain) output(s) to produce", len(pending))

        # Dry-run path — no production.
        if dry_run:
            outcome = PhaseOutcome.REUSED if not pending else PhaseOutcome.PENDING
            self.result = self._make_result(outcome, artifacts, message)
            return self.result

        # Nothing pending — everything is already on disk.
        if not pending:
            self.result = self._make_result(PhaseOutcome.REUSED, artifacts, message)
            return self.result

        # Produce the pending outputs.
        with self._collector.time(MetricKey.AUDIO):
            result = self._execute_audio(artifacts)
        self.result = result
        return result

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the audio phase.

        Chain outputs are delivery artifacts kept under every cleanup level, and
        ``audio.yaml`` is a recovery sidecar that must survive reruns (Req 10.8).
        ``AudioPhase`` therefore has no deep artifacts to remove — a safe no-op
        regardless of ``ctx.deep_cleanup``.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, dry_run: bool) -> AudioPhaseResult | None:
        """Resolve dependencies via the shared walk; fail fast if incomplete.

        Args:
            dry_run: Propagated unchanged to each dependency's ``run()``.

        Returns:
            A ``FAILED`` result if any dependency failed, a ``PENDING`` result
            if any dependency is legitimately pending (dry-run only), or
            ``None`` when all dependencies are complete and the phase may
            proceed.
        """
        if self._job is None:
            return _failed("AudioPhase requires JobPhase")
        if self._extraction is None:
            return _failed("AudioPhase requires ExtractionPhase")

        status = resolve_dependencies(self, dry_run=dry_run)
        if status.failed:
            names = ", ".join(n.capitalize() for n in status.failed)
            err = f"{self.name.capitalize()} cannot run — failed dependencies: {names}"
            logger.error(err)
            return _failed(err)
        if status.pending:
            names = ", ".join(n.capitalize() for n in status.pending)
            msg = f"{self.name.capitalize()} dry-run is impossible — work still pending at: {names}"
            logger.info(msg)
            return _pending(msg)

        return None

    # ------------------------------------------------------------------
    # Recovery + invalidation
    # ------------------------------------------------------------------

    def _recover(self, force_wipe: bool) -> list[AudioArtifact]:
        """Resolve the plan, invalidate changed chains, classify on-disk outputs.

        Steps (Req 9.x, 10.x):

        1. ``force_wipe`` (Req 9.9) → delete every chain output and the sidecar.
        2. Clean up leftover ``.tmp`` files (Req 8.5).
        3. Resolve the working track set (``resolve_selection`` — select is never
           persisted, Req 9.1) and every configured chain
           (``resolve_chain``).
        4. Compare each resolved chain against the persisted sidecar entry of the
           same name (Req 9.2). For a **differing** chain, delete its on-disk
           outputs for ALL tracks by exact chain-name match (Req 9.4) so they are
           reproduced. For a chain **removed** from config, delete its persisted
           outputs (cleanup — unwanted now).
        5. Write the updated sidecar (current resolved chains) **before producing
           anything** (Req 9.5) when it differs from what is on disk.
        6. Classify each expected (track, chain) output COMPLETE (file present) /
           ABSENT (missing) — completion is read from disk only (Req 9.6).

        Args:
            force_wipe: When ``True``, wipe all audio artifacts + sidecar first.

        Returns:
            The internal artifact list (wanted expected outputs plus any
            present-but-unwanted surplus files).
        """
        job_result   = self._job.result       # type: ignore[union-attr]
        work_dir     = LongPath(job_result.work_dir)
        sidecar_path = work_dir / _AUDIO_YAML
        audio_cfg    = job_result.config.audio

        # Step 3 — resolve the working plan (selection is recomputed every run).
        tracks   = self._selected_tracks()
        resolved = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}

        # Chain outputs go to the phase's DEDICATED audio directory (Phase
        # Contract: each phase owns its own folder). Deletion / .tmp-cleanup /
        # surplus scanning / production all use this dir — never the extraction
        # dir. Created up front so producing can write into it.
        audio_dir = self._output_dir(tracks, work_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)

        # Step 1 — force wipe.
        if force_wipe:
            self._force_wipe(audio_dir, sidecar_path, resolved)

        # Step 2 — clear leftover .tmp files.
        self._clean_tmp(audio_dir)

        # Step 4 + 5 — invalidate differing/removed chains and rewrite the sidecar
        #              BEFORE producing anything.
        self._invalidate_and_commit(audio_dir, sidecar_path, resolved)

        # Step 6 — classify expected outputs (completion from disk only).
        return self._classify(audio_dir, tracks, resolved)

    def _output_dir(self, tracks: list[AudioMetadata], work_dir: LongPath) -> LongPath:
        """Return the phase's DEDICATED audio output directory (``work_dir/audio``).

        The audio phase owns this folder (Phase Contract). Chain outputs are
        written here — never next to the source tracks — so all deletion,
        ``.tmp``-cleanup, surplus-scanning, and production operate on one
        phase-owned directory regardless of where the sources live. The
        ``tracks`` argument is accepted for a uniform signature but no longer
        influences the location.

        Args:
            tracks:   The working track set (unused; kept for signature uniformity).
            work_dir: The job work directory.

        Returns:
            The dedicated audio output directory.
        """
        return work_dir / AUDIO_OUTPUT_DIR

    def _force_wipe(
        self,
        audio_dir:    LongPath,
        sidecar_path: LongPath,
        resolved:     dict[str, ResolvedChain],
    ) -> None:
        """Delete every chain output and the sidecar (Req 9.9).

        The dedicated audio dir holds only chain outputs, but the
        ``chain=<name>`` token guard is kept (extra-safe): only files carrying it
        are removed, so any unrelated file dropped into the dir survives.

        Args:
            audio_dir:    The dedicated audio output directory.
            sidecar_path: The ``audio.yaml`` path.
            resolved:     Current resolved chains (unused for the wipe; kept for a
                          uniform invalidation signature).
        """
        if audio_dir.exists():
            for path in audio_dir.iterdir():
                if path.is_file() and _parse_chain_name(path.name) is not None:
                    path.unlink(missing_ok=True)
                    logger.debug("force_wipe: deleted %s", path.name)
        if sidecar_path.exists():
            sidecar_path.unlink(missing_ok=True)
            logger.debug("force_wipe: deleted %s", sidecar_path.name)

    def _clean_tmp(self, audio_dir: LongPath) -> None:
        """Remove leftover ``.tmp`` files from a previous interrupted run."""
        if not audio_dir.exists():
            return
        for tmp in audio_dir.glob(f"*{TEMP_SUFFIX}"):
            try:
                tmp.unlink()
                logger.warning("Removed leftover temp file: %s", tmp.name)
            except OSError as exc:
                logger.warning("Could not remove temp file %s: %s", tmp, exc)

    def _selected_tracks(self) -> list[AudioMetadata]:
        """Resolve the working track set from extraction + ``audio.select`` (Req 9.1)."""
        extraction_result = self._extraction.result if self._extraction else None  # type: ignore[union-attr]
        audio_meta: list[AudioMetadata] = getattr(extraction_result, "audio", []) or []
        audio_cfg = self._job.result.config.audio  # type: ignore[union-attr]
        return resolve_selection(audio_meta, audio_cfg.select)

    def _invalidate_and_commit(
        self,
        audio_dir:    LongPath,
        sidecar_path: LongPath,
        resolved:     dict[str, ResolvedChain],
    ) -> None:
        """Delete outputs of differing/removed chains, then commit the sidecar.

        Compares each resolved chain to the persisted sidecar (Req 9.2). A
        differing chain's outputs are deleted for all tracks (reproduced,
        Req 9.3/9.4); a removed chain's persisted outputs are deleted (cleanup).
        The updated sidecar is written **before any output is produced**
        (Req 9.5); when nothing differs the rewrite is skipped (the sidecar is
        already correct).

        Args:
            audio_dir:    The dedicated audio output directory.
            sidecar_path: The ``audio.yaml`` path.
            resolved:     Current resolved chains, keyed by name.
        """
        persisted   = AudioSidecar.load(sidecar_path)
        prior_sigs  = persisted.signatures if persisted is not None else {}

        # Current chain signatures (the same canonical string the sidecar stores).
        current      = AudioSidecar.from_resolved(resolved)
        current_sigs = current.signatures

        # Chains whose signature changed → invalidate (reproduce).
        changed = {
            name for name, sig in current_sigs.items()
            if name in prior_sigs and prior_sigs[name] != sig
        }
        # Chains removed from config → invalidate (cleanup, now unwanted).
        removed = set(prior_sigs) - set(current_sigs)

        for name in sorted(changed):
            logger.info("Chain %r changed — invalidating its outputs for reprocessing", name)
            self._delete_chain_outputs(audio_dir, name)
        for name in sorted(removed):
            logger.info("Chain %r removed from config — cleaning up its outputs", name)
            self._delete_chain_outputs(audio_dir, name)

        # Commit the current signatures before producing (Req 9.5). Skip the
        # rewrite when the sidecar already matches exactly (Req 9.5 last sentence).
        if prior_sigs != current_sigs:
            current.save(sidecar_path)
            logger.debug("Committed audio sidecar (%d chain(s)) before producing", len(resolved))

    def _delete_chain_outputs(self, audio_dir: LongPath, chain_name: str) -> None:
        """Delete on-disk outputs of ``chain_name`` by EXACT chain-name (Req 9.4).

        Output files are ``<stem> chain=<name>.<ext>``. The trailing
        ``chain=<name>`` token is parsed from each candidate and compared for
        equality — never a substring/prefix match — so ``chain=nightlong`` is not
        deleted when invalidating ``night``.

        Args:
            audio_dir:  The directory holding chain outputs.
            chain_name: The exact chain name whose outputs must be removed.
        """
        if not audio_dir.exists():
            return
        for path in audio_dir.iterdir():
            if not path.is_file():
                continue
            if _parse_chain_name(path.name) == chain_name:
                try:
                    path.unlink()
                    logger.debug("Deleted invalidated output: %s", path.name)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path, exc)

    def _classify(
        self,
        audio_dir: LongPath,
        tracks:    list[AudioMetadata],
        resolved:  dict[str, ResolvedChain],
    ) -> list[AudioArtifact]:
        """Build one artifact per expected (track, chain), classified from disk.

        Completion is read solely from output-file presence (Req 9.6): present →
        COMPLETE, missing → ABSENT. Any present file that is not an expected
        output of a configured chain is surfaced as present-but-unwanted
        (COMPLETE, ``wanted=False``) per the Phase Contract (Req 9.8).

        Args:
            audio_dir: The dedicated audio output directory.
            tracks:    The working track set.
            resolved:  Current resolved chains, keyed by name.

        Returns:
            The internal artifact list (expected outputs + surplus files).
        """
        artifacts: list[AudioArtifact] = []
        expected_names: set[str]       = set()

        for track in tracks:
            source = LongPath(track.path)
            layout = self._track_layout(track)
            for name, chain in resolved.items():
                out = chain_output_path(source, name, chain.encode.extension, audio_dir)
                expected_names.add(out.name)
                state = ArtifactState.COMPLETE if out.exists() else ArtifactState.ABSENT
                artifacts.append(AudioArtifact(
                    path         = out,
                    state        = state,
                    source_track = track,
                    chain_name   = name,
                    out_layout   = layout,
                    codec        = chain.encode.codec,
                ))

        # Surface present-but-unwanted surplus files (a stale output whose chain
        # was removed and whose deletion failed, or an unrelated file).
        if audio_dir.exists():
            for path in audio_dir.iterdir():
                if (
                    path.is_file()
                    and not path.name.endswith(TEMP_SUFFIX)
                    and _parse_chain_name(path.name) is not None
                    and path.name not in expected_names
                ):
                    artifacts.append(AudioArtifact(
                        path   = LongPath(path),
                        state  = ArtifactState.COMPLETE,
                        wanted = False,
                    ))

        return artifacts

    def _track_layout(self, track: AudioMetadata) -> ChannelLayout:
        """Return the track's channel layout, falling back gracefully when absent.

        The extraction-provided ``AudioMetadata.layout`` is preferred (Task 4);
        when it is ``None`` a stereo fallback keeps chain execution viable rather
        than crashing on a missing layout.

        Args:
            track: The selected extracted audio track.

        Returns:
            A concrete :class:`ChannelLayout`.
        """
        if track.layout is not None:
            return track.layout
        logger.warning(
            "Track %s has no extraction layout — falling back to %s",
            track.path.name, _FALLBACK_LAYOUT_TOKEN,
        )
        return ChannelLayout.parse(_FALLBACK_LAYOUT_TOKEN)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_audio(self, artifacts: list[AudioArtifact]) -> AudioPhaseResult:
        """Produce every pending (track, chain) output via the chain executor.

        Pending artifacts (ABSENT / PARTIAL) are produced one at a time through
        :func:`~pyqenc.audio.chain.execute_chain`, advancing a count-based
        :class:`ProgressBar` (total = pending job count, Req 10.6). A
        ``passthrough`` chain raises ``NotImplementedError`` and a failing chain
        raises ``ChainExecutionError``; both are caught per-job and surfaced as a
        FAILED artifact for that output — the phase never crashes on one bad
        chain (Req 11.1, 11.4).

        Args:
            artifacts: The wanted artifact list from ``_recover()``.

        Returns:
            ``AudioPhaseResult`` — COMPLETED when work ran (even with some
            failures), FAILED only when nothing could be produced and failures
            occurred.
        """
        pending = [a for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)]
        job_result = self._job.result  # type: ignore[union-attr]
        audio_cfg  = job_result.config.audio
        resolved   = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir  = LongPath(job_result.work_dir) / AUDIO_OUTPUT_DIR

        produced = 0
        failed   = 0
        with ProgressBar(total=len(pending), title="AUDIO", total_count=len(pending)) as advance:
            for art in pending:
                label = f"[{art.chain_name}] {art.source_track.path.stem if art.source_track else '?'}"
                logger.debug("Producing %s", label)
                try:
                    self._produce_one(art, resolved[art.chain_name], audio_dir)  # type: ignore[index]
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
            return _failed(f"all {failed} audio chain output(s) failed")

        outcome = PhaseOutcome.COMPLETED
        return self._make_result(
            outcome,
            artifacts,
            f"produced {produced}, reused {max(reused, 0)}, failed {failed}",
        )

    def _produce_one(self, artifact: AudioArtifact, chain: ResolvedChain, output_dir: LongPath) -> None:
        """Execute one (track, chain) job, writing the artifact's output file.

        Runs the async chain executor to completion. The executor enforces the
        ``.tmp``-then-rename protocol and the correct output container muxer, and
        writes into the phase's dedicated ``output_dir``.

        Args:
            artifact:   The pending artifact (carries source track + layout).
            chain:      The resolved chain to apply.
            output_dir: The dedicated audio output directory.

        Raises:
            NotImplementedError: For a ``passthrough`` chain (Req 11).
            ChainExecutionError: When a measurement or application pass fails.
        """
        assert artifact.source_track is not None
        source = LongPath(artifact.source_track.path)
        layout = artifact.out_layout or self._track_layout(artifact.source_track)
        asyncio.run(execute_chain(chain, source, layout, output_dir))

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[AudioArtifact],
        message:   str,
    ) -> AudioPhaseResult:
        """Assemble an ``AudioPhaseResult`` from the wanted artifacts.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.

        Returns:
            The populated result (``artifacts`` drive dependency resolution;
            ``outputs`` / ``audio_files`` are the audio-specific views).
        """
        complete = [a for a in artifacts if a.state == ArtifactState.COMPLETE]
        return AudioPhaseResult(
            outcome     = outcome,
            artifacts   = artifacts,
            message     = message,
            outputs     = artifacts,
            audio_files = [a.path for a in complete],
        )


# ---------------------------------------------------------------------------
# AudioPhase module-level helpers
# ---------------------------------------------------------------------------

def _parse_chain_name(filename: str) -> str | None:
    """Return the exact chain name from a ``<stem> chain=<name>.<ext>`` filename.

    Splits on the ``chain=`` suffix delimiter and strips the extension, returning
    the chain name verbatim for exact-match invalidation (Req 9.4). Returns
    ``None`` when the filename carries no ``chain=`` token (not a chain output).

    Args:
        filename: A bare filename (no directory component).

    Returns:
        The chain name, or ``None`` when the file is not a chain output.
    """
    idx = filename.rfind(CHAIN_FILENAME_SUFFIX)
    if idx == -1:
        return None
    tail = filename[idx + len(CHAIN_FILENAME_SUFFIX):]
    # Strip the extension (single trailing suffix) — chain names are filesystem
    # safe and contain no dot in practice, but rsplit is robust to a dotted stem.
    dot = tail.rfind(".")
    return tail[:dot] if dot != -1 else tail


def _failed(error: str) -> AudioPhaseResult:
    """Return a ``FAILED`` ``AudioPhaseResult`` with the given error message."""
    return AudioPhaseResult(
        outcome     = PhaseOutcome.FAILED,
        artifacts   = [],
        message     = error,
        error       = error,
        audio_files = [],
    )


def _pending(reason: str) -> AudioPhaseResult:
    """Return a ``PENDING`` ``AudioPhaseResult`` with the given reason.

    Used when a dependency is legitimately pending during a dry-run preview:
    the phase cannot preview its own work, so it chains ``PENDING`` without an
    error.
    """
    return AudioPhaseResult(
        outcome     = PhaseOutcome.PENDING,
        artifacts   = [],
        message     = reason,
        error       = None,
        audio_files = [],
    )
