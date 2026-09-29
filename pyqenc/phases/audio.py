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
from typing import TYPE_CHECKING, ClassVar, cast

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
    CHAIN_FILENAME_SUFFIX,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import PhaseOutcome
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
)
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import ArtifactState, AudioSidecar
from pyqenc.stream_model import AudioOutput, AudioStream
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.long_path import LongPath

_AUDIO_YAML = "audio.yaml"

# Default channel layout when a selected track carries no extraction layout.
# Preferred source is always the track's own ``AudioMetadata.layout``; this is a
# graceful fallback so a missing layout never crashes chain execution.
_FALLBACK_LAYOUT_TOKEN = "stereo"


@_dataclass
class AudioPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying the audio phase's typed outputs.

    ``outputs`` is the single storage — one ``Artifact[AudioOutput]`` per
    expected (track, chain) row — driving the standard ``pending`` /
    ``complete`` / ``is_complete`` machinery so MergePhase (which lists
    AudioPhase purely for ordering) resolves the dependency as COMPLETE /
    REUSED. ``audio_files`` is the derived list of produced delivery-file
    paths.

    Attributes:
        outputs: Typed chain-output rows (all wanted outputs).
    """

    outputs: list[Artifact[AudioOutput]] = _field(default_factory=list)

    # Transitional population (deleted in task 9 when the base field becomes
    # the derived concatenation).

    @property
    def audio_files(self) -> list[Path]:
        """Paths of the complete outputs' delivery files."""
        return [
            row.payload.output_path
            for row in self.outputs
            if row.state == ArtifactState.COMPLETE
        ]


class AudioPhase(Phase):
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
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    _METRIC_KEY: MetricKey = MetricKey.AUDIO

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry | None = None,
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

    def _recover(self) -> Recovery:
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

        Returns:
            The :class:`Recovery` single source of truth (internal artifact
            list: wanted expected outputs plus any present-but-unwanted
            surplus files).
        """
        job_result: JobPhaseResult = cast(JobPhaseResult, self._dep(JobPhase).result)
        work_dir    = LongPath(job_result.work_dir)
        sidecar_path = work_dir / _AUDIO_YAML
        audio_cfg   = job_result.config.audio
        force_wipe  = job_result.force_wipe

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
        return Recovery.from_artifacts(self._classify(audio_dir, tracks, resolved))

    def _output_dir(self, tracks: list[AudioStream], work_dir: LongPath) -> LongPath:
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

    def _selected_tracks(self) -> list[AudioStream]:
        """Resolve the working track set from extraction + ``audio.select`` (Req 9.1)."""
        extraction_result = cast(ExtractionPhaseResult, self._dep(ExtractionPhase).result)
        audio_streams: list[AudioStream] = [
            a.payload for a in extraction_result.audio_streams
        ]
        audio_cfg = cast(JobPhaseResult, self._dep(JobPhase).result).config.audio
        return resolve_selection(audio_streams, audio_cfg.select)

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
        prior_sigs  = persisted.chains if persisted is not None else {}

        # Current chain signatures (the same canonical string the sidecar stores).
        current      = AudioSidecar.from_resolved(resolved)
        current_sigs = current.chains

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

        Output files are ``<stream safe name> chain=<name>.<ext>``. The trailing
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
        tracks:    list[AudioStream],
        resolved:  dict[str, ResolvedChain],
    ) -> list[Artifact]:
        """Build one row per expected (track, chain), classified from disk.

        Completion is read solely from output-file presence (Req 9.6): present →
        COMPLETE, missing → ABSENT. Expected rows carry an
        :class:`~pyqenc.stream_model.AudioOutput` payload composed at the
        chain-output materialization site. Any present file that is not an
        expected output of a configured chain is surfaced as present-but-
        unwanted (``COMPLETE``, ``wanted=False``) per the Phase Contract
        (Req 9.8) — the producing entity no longer exists, so the on-disk
        product itself (a :class:`~pyqenc.stream_model.File`) is the payload.

        Args:
            audio_dir: The dedicated audio output directory.
            tracks:    The working track set.
            resolved:  Current resolved chains, keyed by name.

        Returns:
            The internal ledger (expected outputs + surplus files).
        """
        from pyqenc.stream_model import File

        rows: list[Artifact] = []
        expected_names: set[str] = set()

        for stream in tracks:
            assert stream.info.layout is not None, "layout guaranteed by ExtractionPhase"
            for name, chain in resolved.items():
                out = chain_output_path(stream, name, chain.encode.extension, audio_dir)
                expected_names.add(out.name)
                rows.append(Artifact(
                    payload = AudioOutput(stream=stream, chain_name=name, output_path=out),
                    state   = ArtifactState.COMPLETE if out.exists() else ArtifactState.ABSENT,
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
                    rows.append(Artifact(
                        payload = File(path=LongPath(path), file_size_bytes=_safe_size(path)),
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
        :class:`ProgressBar` (total = pending job count, Req 10.6). A
        ``passthrough`` chain raises ``NotImplementedError`` and a failing chain
        raises ``ChainExecutionError``; both are caught per-job and surfaced as a
        FAILED artifact for that output — the phase never crashes on one bad
        chain (Req 11.1, 11.4). ``dry_run`` is never ``True`` here (audio is not
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
        job_result = cast(JobPhaseResult, self._dep(JobPhase).result)
        audio_cfg  = job_result.config.audio
        resolved   = {spec.name: resolve_chain(spec, audio_cfg.filters) for spec in audio_cfg.chains}
        audio_dir  = LongPath(job_result.work_dir) / AUDIO_OUTPUT_DIR

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

    def _produce_one(self, output: AudioOutput, chain: ResolvedChain, output_dir: LongPath) -> None:
        """Execute one (track, chain) job, writing the output's delivery file.

        Runs the async chain executor to completion. The executor enforces the
        ``.tmp``-then-rename protocol and the correct output container muxer,
        and writes into the phase's dedicated ``output_dir``.

        Args:
            output:     The pending row's payload (the source track + chain).
            chain:      The resolved chain to apply.
            output_dir: The dedicated audio output directory.

        Raises:
            NotImplementedError: For a ``passthrough`` chain (Req 11).
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
            dependency resolution and the derived ``audio_files``).
        """
        outputs = [r for r in artifacts if isinstance(r.payload, AudioOutput)]
        return AudioPhaseResult(
            outcome   = outcome,
            artifacts = artifacts,  # transitional (task 9 derives it)
            message   = message,
            outputs   = outputs,
        )


# ---------------------------------------------------------------------------
# AudioPhase module-level helpers
# ---------------------------------------------------------------------------

def _safe_size(path: Path) -> int | None:
    """The file's size in bytes, or ``None`` when the stat fails."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _parse_chain_name(filename: str) -> str | None:
    """Return the exact chain name from a ``<stream safe name> chain=<name>.<ext>`` filename.

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
