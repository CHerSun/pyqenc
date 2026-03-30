"""
Merging phase for the quality-based encoding pipeline.

This module handles concatenation of encoded video chunks to produce final
MKV output files.  It also measures final quality metrics and generates
visual plots for verification.

Audio muxing is intentionally omitted — the final output is video-only.
Audio delivery files are kept alongside the output for the user to mux
manually or in a downstream step.
"""
# CHerSun 2026

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from pyqenc.constants import (
    ENCODED_ATTEMPT_NAME_PATTERN,
    FAILURE_SYMBOL_MINOR,
    FINAL_OUTPUT_DIR,
    RANGE_SEPARATOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, VideoMetadata
from pyqenc.phase import Artifact, ArtifactState, Phase, PhaseResult
from pyqenc.state import MergeParams
from pyqenc.utils.ffmpeg_runner import get_frame_count, run_ffmpeg
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line
from pyqenc.utils.visualization import QualityEvaluator, create_crf_plot
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.audio import AudioPhase, AudioPhaseResult
    from pyqenc.phases.encoding import (
        EncodedArtifact,
        EncodingPhase,
        EncodingPhaseResult,
    )
    from pyqenc.phases.job import JobPhase, JobPhaseResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MERGE_YAML = "merge.yaml"


def _targets_as_strings(targets: list[QualityTarget]) -> list[str]:
    """Serialise quality targets to ``"metric-statistic:value"`` strings."""
    return [f"{t.metric}-{t.statistic}:{t.value}" for t in targets]


# ---------------------------------------------------------------------------
# MergeArtifact
# ---------------------------------------------------------------------------

@dataclass
class MergeArtifact(Artifact):
    """Artifact for a single merged output file.

    Attributes:
        strategy_name: Display name of the encoding strategy (e.g. ``slow+h265-aq``).
        frame_count:   Frame count of the merged output; ``None`` until measured.
        metrics:       Final quality metrics dict; empty until measured.
        targets_met:   Whether quality targets were met; ``False`` until measured.
        plot_path:     Path to the quality plot PNG; ``None`` if not produced.
    """

    strategy_name: str        = ""
    frame_count:   int | None = None
    metrics:       dict[str, float] = field(default_factory=dict)
    targets_met:   bool             = False
    plot_path:     Path | None      = None


# ---------------------------------------------------------------------------
# Sidecar model
# ---------------------------------------------------------------------------

_MERGE_SIDECAR_VERSION = 1


def _sidecar_path(output_file: Path) -> Path:
    """Return the sidecar YAML path for a merged output file."""
    return output_file.with_suffix(".yaml")


def _load_merge_sidecar(output_file: Path) -> dict | None:
    """Load the merge sidecar for *output_file*, or ``None`` if absent/invalid."""
    path = _sidecar_path(output_file)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception as exc:
        logger.debug("Could not load merge sidecar %s: %s", path.name, exc)
        return None


def _write_merge_sidecar(
    output_file:     Path,
    frame_count:     int | None,
    final_metrics:   dict[str, float],
    targets_met:     bool,
    plot_path:       Path | None,
) -> None:
    """Atomically write a merge sidecar alongside *output_file*."""
    data: dict = {
        "version":     _MERGE_SIDECAR_VERSION,
        "frame_count": frame_count,
        "targets_met": targets_met,
        "metrics":     final_metrics,
    }
    if plot_path is not None:
        data["plot"] = str(plot_path)
    try:
        write_yaml_atomic(_sidecar_path(output_file), data)
    except Exception as exc:
        logger.warning("Could not write merge sidecar for %s: %s", output_file.name, exc)




# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strategy_display_name(strategy: str) -> str:
    """Return a human-readable strategy name (preserves ``+`` and ``:`` separators)."""
    return strategy.replace("_", "+", 1)  # only first underscore — profile may contain underscores


def _strategy_safe_name(strategy: str) -> str:
    """Return a filesystem-safe strategy name."""
    return strategy.replace("+", "_").replace(":", "_")


def _cleanup_tmp_files(directory: Path) -> None:
    """Remove leftover ``.tmp`` files from a previous interrupted run."""
    if not directory.exists():
        return
    for tmp_file in directory.glob(f"*{TEMP_SUFFIX}"):
        try:
            tmp_file.unlink()
            logger.warning("Removed leftover temp file: %s", tmp_file.name)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", tmp_file.name, exc)


def _measure_quality(
    final_result:     Path,
    source_video:     VideoMetadata,
    ref_crop:         CropParams | None,
    quality_targets:  list[QualityTarget],
    output_dir:       Path,
    metrics_sampling: int,
) -> tuple[dict[str, float], bool, Path | None]:
    """Measure final quality metrics for *final_result* against *source_video*.

    Intermediate metric artifacts (raw logs, ``.stats`` files) are placed in
    ``output_dir / final_result.stem``.  The quality plot PNG is written to
    ``output_dir / f"{final_result.stem}.png"``.

    Returns:
        Tuple of ``(metrics_dict, targets_met, plot_path)``.
    """
    evaluator   = QualityEvaluator(output_dir)
    metrics_dir = output_dir / final_result.stem
    plot_path   = output_dir / f"{final_result.stem}.png"

    evaluation = evaluator.evaluate_chunk(
        encoded=final_result,
        reference=source_video.path,
        ref_crop=ref_crop,
        targets=quality_targets,
        output_dir=metrics_dir,
        subsample_factor=metrics_sampling,
        show_progress=True,
        plot_path=plot_path,
    )

    metrics_dict: dict[str, float] = {}
    for metric_name, metric_stats in evaluation.metrics.items():
        for stat_name, stat_value in metric_stats.items():
            metrics_dict[f"{metric_name}_{stat_name}"] = stat_value

    plot_path = evaluation.artifacts.plot if evaluation.artifacts.plot else None
    return metrics_dict, evaluation.targets_met, plot_path


def _log_metrics_summary(
    strategy:        str,
    metrics_dict:    dict[str, float],
    quality_targets: list[QualityTarget],
    targets_met:     bool,
) -> None:
    """Log a compact quality metrics summary for *strategy*."""
    for target in quality_targets:
        key    = f"{target.metric}_{target.statistic}"
        value  = metrics_dict.get(key)
        if value is None:
            continue
        symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
        logger.info(
            "  %s %s-%s: %.2f (target: %.2f)",
            symbol, target.metric, target.statistic, value, target.value,
        )
    overall = SUCCESS_SYMBOL_MINOR if targets_met else FAILURE_SYMBOL_MINOR
    logger.info("  %s %s: quality targets %s", overall, strategy, "met" if targets_met else "NOT met")


def _collect_crf_data(
    encoded:     "list[EncodedArtifact]",
    strategy:    str,
) -> list[tuple[float, float, float]]:
    """Extract ``(start_seconds, end_seconds, crf)`` tuples for winning chunks of *strategy*.

    Timestamps are parsed from the ``chunk_id`` stem, which encodes the range
    as ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm`` using filesystem-safe separators.

    Args:
        encoded:  All ``EncodedArtifact`` objects from the encoding phase.
        strategy: Strategy name to filter by.

    Returns:
        List of ``(start_s, end_s, crf)`` sorted by start time.
        Chunks with missing CRF or unparseable IDs are silently skipped.
    """
    def _parse_ts(ts_str: str) -> float:
        """Parse ``HH꞉MM꞉SS․mmm`` into seconds."""
        parts = ts_str.split(TIME_SEPARATOR_SAFE)
        if len(parts) != 3:
            raise ValueError(f"Unexpected timestamp format: {ts_str!r}")
        h, m, s_ms = parts
        s, ms = s_ms.split(TIME_SEPARATOR_MS)
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    result: list[tuple[float, float, float]] = []
    for artifact in encoded:
        if artifact.strategy != strategy:
            continue

        crf = artifact.crf
        if crf is None:
            # Fallback: parse CRF from the artifact filename (e.g. "…crf26.5.mkv")
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(artifact.path.name)
            if m:
                try:
                    crf = float(m.group("crf"))
                except (ValueError, IndexError):
                    pass

        if crf is None:
            logger.debug("No CRF available for chunk %r — skipping", artifact.chunk_id)
            continue

        try:
            start_str, end_str = artifact.chunk_id.split(RANGE_SEPARATOR, 1)
            start_s = _parse_ts(start_str)
            end_s   = _parse_ts(end_str)
            result.append((start_s, end_s, crf))
        except Exception as exc:
            logger.debug("Could not parse chunk_id %r for CRF plot: %s", artifact.chunk_id, exc)

    result.sort(key=lambda t: t[0])
    return result





# ---------------------------------------------------------------------------
# MergePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class MergePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying merge-specific payload.

    Attributes:
        merged: All ``MergeArtifact`` objects produced by this phase.
    """

    merged: list[MergeArtifact] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MergePhase
# ---------------------------------------------------------------------------

class MergePhase:
    """Phase object for final video merging.

    Owns artifact enumeration, recovery, execution, and logging for the merge
    phase.  Wraps the existing ``merge_final_video`` helper.

    In pipeline mode encoded chunks are read directly from
    ``EncodingPhase.result`` without rescanning the filesystem.  In standalone
    mode the phase scans ``encoded/<strategy.safe_name>/`` for each strategy.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "merge"

    def __init__(
        self,
        config: "PipelineConfig",
        phases: "dict[type[Phase], Phase] | None" = None,
    ) -> None:
        from pyqenc.phases.audio import AudioPhase as _AudioPhase
        from pyqenc.phases.encoding import EncodingPhase as _EncodingPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config:    "PipelineConfig"           = config
        self._job:       "_JobPhase | None"          = cast("_JobPhase",      phases[_JobPhase])      if phases else None
        self._encoding:  "_EncodingPhase | None"     = cast("_EncodingPhase", phases[_EncodingPhase]) if phases else None
        self._audio:     "_AudioPhase | None"        = cast("_AudioPhase",    phases[_AudioPhase])    if phases else None
        self.params      = MergeParams(
            quality_targets  = _targets_as_strings(config.quality_targets),
            metrics_sampling = config.metrics_sampling,
        )
        self.result:     "MergePhaseResult | None"   = None
        self.dependencies: "list[Phase]"             = [d for d in [self._job, self._encoding, self._audio] if d is not None]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> "MergePhaseResult":
        """Classify existing merge artifacts without executing any work.

        Returns:
            ``MergePhaseResult`` with all artifacts classified.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        artifacts = self._recover(force_wipe=force_wipe, execute=False)
        outcome   = _outcome_from_artifacts(artifacts, did_work=False)

        self.result = MergePhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = _recovery_message(artifacts),
            merged    = artifacts,
        )
        return self.result

    def run(self, dry_run: bool = False) -> "MergePhaseResult":
        """Recover, merge pending strategies, cache and return result.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results.
        3. Run ``_recover()`` — handles ``force_wipe``.
        4. Log recovery result line.
        5. In dry-run mode: return ``DRY_RUN`` if any artifacts are pending.
        6. Merge pending strategies.
        7. Log completion summary.

        Args:
            dry_run: When ``True``, report what would be done without merging.

        Returns:
            ``MergePhaseResult`` with all artifacts ``COMPLETE`` on success.
        """
        emit_phase_banner("MERGE", logger)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        # Key parameters
        logger.info("Source stem:  %s", self._config.source_video.stem)
        if self._config.quality_targets:
            logger.info("Targets:      %s", ", ".join(
                f"{t.metric}-{t.statistic}≥{t.value}" for t in self._config.quality_targets
            ))

        artifacts = self._recover(force_wipe=force_wipe, execute=True)

        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending_count  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
        log_recovery_line(logger, complete_count, pending_count)

        # Dry-run path
        if dry_run:
            outcome = PhaseOutcome.REUSED if pending_count == 0 else PhaseOutcome.DRY_RUN
            self.result = MergePhaseResult(
                outcome   = outcome,
                artifacts = artifacts,
                message   = "dry-run",
                merged    = artifacts,
            )
            return self.result

        # Nothing to do
        if pending_count == 0:
            self.result = MergePhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = artifacts,
                message   = "all merge artifacts reused",
                merged    = artifacts,
            )
            return self.result

        # Execute merging
        result = self._execute_merge(artifacts)
        self.result = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> "MergePhaseResult | None":
        """Scan/run dependencies if they have no cached result; fail fast if incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` for deps without a cached result.

        Returns:
            A ``FAILED`` result if any dependency is not complete; ``None`` otherwise.
        """
        if self._job is None:
            return _failed("MergePhase requires JobPhase")

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        if self._encoding is None:
            return _failed("MergePhase requires EncodingPhase")

        if self._encoding.result is None:
            if execute:
                self._encoding.run()
            else:
                self._encoding.scan()

        if not self._encoding.result.is_complete:  # type: ignore[union-attr]
            err = "EncodingPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        incomplete = [
            a for a in self._encoding.result.encoded  # type: ignore[union-attr]
            if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY)
        ]
        if incomplete:
            err = f"EncodingPhase has {len(incomplete)} incomplete artifact(s) — cannot merge"
            logger.critical(err)
            return _failed(err)

        if self._audio is None:
            return _failed("MergePhase requires AudioPhase")

        if self._audio.result is None:
            if execute:
                self._audio.run()
            else:
                self._audio.scan()

        if not self._audio.result.is_complete:  # type: ignore[union-attr]
            err = "AudioPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        return None

    def _recover(self, force_wipe: bool, execute: bool) -> list[MergeArtifact]:
        """Classify merge artifacts and handle force-wipe.

        Steps:
        1. If ``force_wipe`` and execute: delete ``final/`` and ``merge.yaml``.
        2. Detect quality-target / metrics_sampling change — delete per-output
           sidecars so stale COMPLETE artifacts are reclassified as ARTIFACT_ONLY
           and the merge re-runs with fresh metrics.
        3. Clean up leftover ``.tmp`` files (execute mode only).
        4. Determine expected strategies from ``EncodingPhase.result``.
        5. Scan ``final/`` for output + sidecar pairs; classify each.

        Args:
            force_wipe: When ``True``, wipe all merge artifacts first.
            execute:    When ``True``, wipe and ``.tmp`` cleanup are performed.

        Returns:
            List of ``MergeArtifact`` objects.
        """
        work_dir  = self._config.work_dir
        final_dir = work_dir / FINAL_OUTPUT_DIR
        merge_yaml = work_dir / _MERGE_YAML

        # Step 1: force-wipe
        if force_wipe and execute:
            if final_dir.exists():
                shutil.rmtree(final_dir)
                logger.debug("force_wipe: deleted %s", final_dir)
            merge_yaml.unlink(missing_ok=True)
            logger.debug("force_wipe: deleted %s", merge_yaml)

        # Step 2: quality-target / metrics_sampling change detection.
        # When params change, delete all per-output sidecars so every artifact
        # is reclassified as ARTIFACT_ONLY and the merge re-runs with fresh metrics.
        if execute and not force_wipe and final_dir.exists():
            persisted = MergeParams.load(merge_yaml)
            if persisted is not None and persisted != self.params:
                targets_changed  = bool(persisted.quality_targets) and persisted.quality_targets != self.params.quality_targets
                sampling_changed = persisted.metrics_sampling is not None and persisted.metrics_sampling != self.params.metrics_sampling
                if targets_changed or sampling_changed:
                    logger.info(
                        "Merge params changed (%s) — deleting merge sidecars to re-measure quality",
                        "quality targets" if targets_changed else "metrics_sampling",
                    )
                    for sidecar in final_dir.glob("*.yaml"):
                        try:
                            sidecar.unlink()
                            logger.debug("Deleted stale merge sidecar: %s", sidecar.name)
                        except OSError as exc:
                            logger.warning("Could not delete merge sidecar %s: %s", sidecar.name, exc)
                    merge_yaml.unlink(missing_ok=True)

        # Step 3: clean up .tmp files (execute mode only)
        if execute and final_dir.exists():
            for tmp in final_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 4: determine expected strategies
        strategies = self._get_expected_strategies()
        if not strategies:
            return []

        source_stem = self._config.source_video.stem

        # Step 5: classify each expected output
        artifacts: list[MergeArtifact] = []
        for strategy_name, safe_name in strategies:
            output_file = final_dir / f"{source_stem} {safe_name}.mkv"
            sidecar     = _load_merge_sidecar(output_file)

            if output_file.exists() and sidecar is not None:
                # COMPLETE — file and sidecar both present
                frame_count = sidecar.get("frame_count")
                metrics     = {k: float(v) for k, v in sidecar.get("metrics", {}).items()}
                targets_met = bool(sidecar.get("targets_met", False))
                plot_path: Path | None = None
                if sidecar.get("plot"):
                    p = Path(sidecar["plot"])
                    if p.exists():
                        plot_path = p

                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.COMPLETE,
                    strategy_name = strategy_name,
                    frame_count   = int(frame_count) if frame_count is not None else None,
                    metrics       = metrics,
                    targets_met   = targets_met,
                    plot_path     = plot_path,
                ))
            elif output_file.exists():
                # ARTIFACT_ONLY — file present but sidecar missing
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.ARTIFACT_ONLY,
                    strategy_name = strategy_name,
                ))
            else:
                # ABSENT — not yet produced
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.ABSENT,
                    strategy_name = strategy_name,
                ))

        return artifacts

    def _get_expected_strategies(self) -> list[tuple[str, str]]:
        """Return ``(display_name, safe_name)`` pairs for all expected strategies.

        In pipeline mode reads from ``EncodingPhase.result.encoded`` directly.
        In standalone mode calls ``EncodingPhase.scan()`` first to populate the
        result, then reads from it — this ensures quality-target re-evaluation
        and crop mismatch detection are applied (Req 3.1, 3.2, 6.5).

        Returns:
            List of ``(strategy_name, safe_name)`` tuples.
        """
        self._ensure_encoding_result()

        if self._encoding is None or self._encoding.result is None:
            return []

        encoded = getattr(self._encoding.result, "encoded", [])
        seen: dict[str, str] = {}
        for artifact in encoded:
            if artifact.state == ArtifactState.COMPLETE:
                strategy_name = artifact.strategy
                safe_name     = strategy_name.replace("+", "_").replace(":", "_")
                seen[strategy_name] = safe_name
        return list(seen.items())

    def _execute_merge(self, artifacts: list[MergeArtifact]) -> "MergePhaseResult":
        """Merge pending strategies by concatenating encoded chunks.

        Args:
            artifacts: Artifact list from ``_recover()``.

        Returns:
            ``MergePhaseResult`` after merging.
        """
        work_dir  = self._config.work_dir
        final_dir = work_dir / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        job_result = self._job.result  # type: ignore[union-attr]
        crop       = getattr(job_result, "crop", None)
        job        = getattr(job_result, "job", None)
        source_video: VideoMetadata | None = getattr(job, "source", None) if job else None
        source_frame_count: int | None = source_video.frame_count if source_video else None
        source_stem = self._config.source_video.stem

        # Build encoded_chunks dict from EncodingPhase result
        encoded_chunks = self._collect_encoded_chunks()

        final_artifacts: list[MergeArtifact] = []
        failed_strategies: list[str] = []

        for artifact in artifacts:
            strategy_name = artifact.strategy_name
            safe_name     = strategy_name.replace("+", "_").replace(":", "_")

            if artifact.state == ArtifactState.COMPLETE:
                final_artifacts.append(artifact)
                continue

            output_file = final_dir / f"{source_stem} {safe_name}.mkv"
            logger.info("Merging: %s", strategy_name)

            try:
                # Collect and sort chunks for this strategy
                strategy_chunks: list[Path] = sorted(
                    (
                        encoded_chunks[chunk_id][strategy_name]
                        for chunk_id in sorted(encoded_chunks.keys())
                        if strategy_name in encoded_chunks[chunk_id]
                    ),
                    key=lambda p: p.name,
                )

                if not strategy_chunks:
                    logger.error("No encoded chunks found for strategy %s — skipping", strategy_name)
                    failed_strategies.append(strategy_name)
                    continue

                logger.info("  Starting concatenation of %d chunks...", len(strategy_chunks))

                # Write concat list to a temp file
                concat_file = final_dir / f"concat_{safe_name}{TEMP_SUFFIX}.txt"
                with concat_file.open("w", encoding="utf-8") as fh:
                    for chunk_path in strategy_chunks:
                        abs_path = chunk_path.resolve()
                        escaped  = str(abs_path).replace("'", "'\\''")
                        fh.write(f"file '{escaped}'\n")

                concat_cmd: list[str | os.PathLike] = [
                    "ffmpeg",
                    "-f",    "concat",
                    "-safe", "0",
                    "-i",    concat_file,
                    "-c",    "copy",
                    "-y",
                    output_file,
                ]

                logger.debug("Concat command: %s", " ".join(str(a) for a in concat_cmd))
                concat_result = run_ffmpeg(concat_cmd, output_file=output_file)
                concat_file.unlink(missing_ok=True)

                if not concat_result.success:
                    logger.error("Concatenation failed for strategy %s", strategy_name)
                    failed_strategies.append(strategy_name)
                    continue

                logger.info("  Concatenation complete: %s", output_file.name)

                # Verify frame count
                frame_count: int | None = None
                try:
                    frame_count = get_frame_count(output_file)
                    if source_frame_count is not None:
                        if frame_count != source_frame_count:
                            diff = frame_count - source_frame_count
                            logger.warning(
                                "  Frame count mismatch: expected %d, got %d (%+d)",
                                source_frame_count, frame_count, diff,
                            )
                        else:
                            logger.info("  Frame count verified: %d %s", frame_count, SUCCESS_SYMBOL_MINOR)
                except Exception as exc:
                    logger.warning("  Could not verify frame count: %s", exc)

                # Measure quality
                metrics_dict: dict[str, float] = {}
                targets_met:  bool             = False
                plot_path:    Path | None       = None

                if source_video and self._config.quality_targets:
                    logger.info("  Measuring final quality metrics...")
                    try:
                        metrics_dict, targets_met, plot_path = _measure_quality(
                            final_result     = output_file,
                            source_video     = source_video,
                            ref_crop         = crop,
                            quality_targets  = self._config.quality_targets,
                            output_dir       = final_dir,
                            metrics_sampling = self._config.metrics_sampling,
                        )
                    except Exception as exc:
                        logger.warning("  Could not measure quality: %s", exc)

                    if metrics_dict:
                        _log_metrics_summary(
                            strategy_name, metrics_dict,
                            self._config.quality_targets, targets_met,
                        )

                # CRF distribution plot
                encoded_artifacts = getattr(
                    getattr(self._encoding, "result", None), "encoded", []
                )
                crf_data = _collect_crf_data(encoded_artifacts, strategy_name)
                if crf_data:
                    crf_plot_path = final_dir / f"{output_file.stem}.crf.png"
                    try:
                        create_crf_plot(
                            chunks      = crf_data,
                            output_path = crf_plot_path,
                            title       = f"CRF\n{output_file.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")}",
                        )
                        logger.info("  CRF plot saved: %s", crf_plot_path.name)
                    except Exception as exc:
                        logger.warning("  Could not generate CRF plot: %s", exc)
                else:
                    logger.warning("No CRF data available for strategy %s — skipping CRF plot", strategy_name)

                # Write sidecar (marks this output as COMPLETE)
                _write_merge_sidecar(
                    output_file   = output_file,
                    frame_count   = frame_count,
                    final_metrics = metrics_dict,
                    targets_met   = targets_met,
                    plot_path     = plot_path,
                )

                symbol = SUCCESS_SYMBOL_MINOR if targets_met else FAILURE_SYMBOL_MINOR
                logger.info(
                    "%s %s: merged successfully (frames=%s)",
                    symbol, strategy_name,
                    str(frame_count) if frame_count is not None else "unknown",
                )

                final_artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.COMPLETE,
                    strategy_name = strategy_name,
                    frame_count   = frame_count,
                    metrics       = metrics_dict,
                    targets_met   = targets_met,
                    plot_path     = plot_path,
                ))

            except Exception as exc:
                logger.error("Merging strategy %s error: %s", strategy_name, exc, exc_info=True)
                failed_strategies.append(strategy_name)

        # Phase completion summary
        complete_count = sum(1 for a in final_artifacts if a.state == ArtifactState.COMPLETE)
        logger.info(THICK_LINE)
        logger.info("MERGE SUMMARY")
        logger.info(THICK_LINE)
        logger.info("  Complete: %d output file(s)", complete_count)
        if failed_strategies:
            logger.error("  Failed strategies: %s", ", ".join(failed_strategies))
        logger.info(THICK_LINE)

        if failed_strategies and not final_artifacts:
            return _failed("All strategy merges failed")

        # Persist merge params so quality-target / sampling changes are detected next run
        if complete_count > 0:
            self.params.save(self._config.work_dir / _MERGE_YAML)

        if failed_strategies:
            return MergePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = final_artifacts,
                message   = f"{len(failed_strategies)} strategy(ies) failed",
                error     = f"Failed: {', '.join(failed_strategies[:5])}",
                merged    = final_artifacts,
            )

        did_work = any(a.state == ArtifactState.COMPLETE for a in final_artifacts)
        return MergePhaseResult(
            outcome   = PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED,
            artifacts = final_artifacts,
            message   = f"{complete_count} output file(s) complete",
            merged    = final_artifacts,
        )

    def _ensure_encoding_result(self) -> None:
        """Ensure ``EncodingPhase.result`` is populated.

        In pipeline mode the result is already cached from a prior ``run()`` call.
        In standalone mode (no cached result) calls ``self._encoding.scan()`` so
        that quality-target re-evaluation and crop mismatch detection are applied
        before any strategy or chunk lookup (Req 3.1, 3.2, 6.5).
        """
        if self._encoding is not None and self._encoding.result is None:
            self._encoding.scan()

    def _collect_encoded_chunks(self) -> dict[str, dict[str, Path]]:
        """Build ``{chunk_id: {strategy_name: path}}`` from ``EncodingPhase.result``.

        In pipeline mode reads directly from ``EncodingPhase.result.encoded``.
        In standalone mode calls ``EncodingPhase.scan()`` first to populate the
        result — this applies quality-target re-evaluation and crop mismatch
        detection that a raw filesystem glob would miss (Req 3.1, 3.2, 6.5).

        Returns:
            Nested dict mapping chunk IDs to strategy-to-path mappings.
        """
        self._ensure_encoding_result()

        if self._encoding is None or self._encoding.result is None:
            return {}

        encoded = getattr(self._encoding.result, "encoded", [])
        chunks: dict[str, dict[str, Path]] = {}
        for artifact in encoded:
            if artifact.state == ArtifactState.COMPLETE and artifact.path.exists():
                chunk_id      = artifact.chunk_id
                strategy_name = artifact.strategy
                if chunk_id not in chunks:
                    chunks[chunk_id] = {}
                chunks[chunk_id][strategy_name] = artifact.path
        return chunks


# ---------------------------------------------------------------------------
# MergePhase module-level helpers
# ---------------------------------------------------------------------------

def _outcome_from_artifacts(
    artifacts: list[MergeArtifact],
    did_work:  bool,
) -> PhaseOutcome:
    """Derive ``PhaseOutcome`` from artifact states."""
    if not artifacts:
        return PhaseOutcome.REUSED
    if any(a.state == ArtifactState.ABSENT for a in artifacts):
        return PhaseOutcome.DRY_RUN
    if all(a.state == ArtifactState.COMPLETE for a in artifacts):
        return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
    return PhaseOutcome.DRY_RUN


def _recovery_message(artifacts: list[MergeArtifact]) -> str:
    """Build a human-readable recovery summary string."""
    complete = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
    pending  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
    if pending == 0:
        return f"{complete} output(s) complete — reusing"
    if complete == 0:
        return f"{pending} output(s) pending — full run needed"
    return f"{complete} output(s) complete, {pending} pending — resuming"


def _failed(error: str) -> MergePhaseResult:
    """Return a ``FAILED`` ``MergePhaseResult`` with the given error message."""
    return MergePhaseResult(
        outcome   = PhaseOutcome.FAILED,
        artifacts = [],
        message   = error,
        error     = error,
        merged    = [],
    )
