"""
Encoding phase for the quality-based encoding pipeline.

This module handles chunk encoding with iterative CRF adjustment to meet
quality targets, including parallel execution and artifact-based resumption.
"""

import asyncio
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from alive_progress import config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import (
    ENCODED_ATTEMPT_GLOB_PATTERN,
    ENCODED_ATTEMPT_NAME_PATTERN,
    FAILURE_SYMBOL_MINOR,
    PADDING_CRF,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THRESHOLD_ATTEMPTS_WARNING,
)
from pyqenc.models import (
    AttemptMetadata,
    ChunkMetadata,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    StrategyConfig,
    VideoMetadata,
)
from pyqenc.quality import CRFHistory, adjust_crf
from pyqenc.state import EncodingResultSidecar, MetricsSidecar
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.ffmpeg_runner import run_ffmpeg
from pyqenc.utils.log_format import (
    fmt_chunk,
    fmt_chunk_attempt_result,
    fmt_chunk_attempt_start,
    fmt_chunk_final,
    fmt_chunk_start,
)
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.phases.recovery import PhaseRecovery
    from pyqenc.state import JobStateManager

config_handler.set_global(enrich_print=False) # type: ignore
_logger = logging.getLogger(__name__)


def _probe_resolution(path: Path) -> str | None:
    """Return the video resolution of *path* as ``'WxH'``, or ``None`` on failure."""
    import json as _json
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            w, h = streams[0].get("width"), streams[0].get("height")
            if w and h:
                return f"{w}x{h}"
    except Exception:
        pass
    return None


def _read_metrics_sidecar(attempt_path: Path) -> dict | None:
    """Read a per-attempt metrics sidecar for an encoded attempt.

    Tries the new YAML format (``.yaml``) first; falls back to the legacy
    JSON format (``.metrics.json``) for backward compatibility (Req 8.2).

    Args:
        attempt_path: Path to the encoded attempt ``.mkv`` file.

    Returns:
        Parsed sidecar dict (keys: ``targets_met``, ``crf``, ``metrics``),
        or ``None`` if no sidecar exists or it cannot be parsed.
    """
    import yaml as _yaml

    yaml_sidecar = attempt_path.with_suffix(".yaml")
    if yaml_sidecar.exists():
        try:
            with yaml_sidecar.open("r", encoding="utf-8") as fh:
                return _yaml.safe_load(fh)
        except Exception:
            pass

    # Legacy fallback
    json_sidecar = attempt_path.with_suffix(".metrics.json")
    if json_sidecar.exists():
        import json as _json
        try:
            with json_sidecar.open("r", encoding="utf-8") as fh:
                return _json.load(fh)
        except Exception:
            pass

    return None


def _write_metrics_sidecar(
    attempt_path: Path,
    targets_met:  bool,
    crf:          float,
    metrics:      dict[str, float],
) -> None:
    """Atomically write a per-attempt metrics sidecar alongside an encoded attempt.

    Uses ``write_yaml_atomic`` so a crash during writing never leaves a partial
    sidecar.  Stores ALL measured metric values (not filtered to current targets)
    so the CRF history is reusable when quality targets change (Req 6a.1).

    Args:
        attempt_path: Path to the encoded attempt ``.mkv`` file.
        targets_met:  Whether quality targets were met (for human inspection only).
        crf:          CRF value used for this attempt.
        metrics:      ALL measured quality metrics dict (not filtered to targets).
    """
    sidecar = attempt_path.with_suffix(".yaml")
    data    = MetricsSidecar(crf=crf, targets_met=targets_met, metrics=metrics)
    try:
        write_yaml_atomic(sidecar, data.to_yaml_dict())
    except Exception as e:
        _logger.warning("Failed to write metrics sidecar for %s: %s", attempt_path.name, e)


def _write_encoding_result_sidecar(
    output_dir:      Path,
    chunk_id:        str,
    resolution:      str,
    winning_attempt: Path,
    crf:             float,
    quality_targets: list[QualityTarget],
    metrics:         dict[str, float],
) -> None:
    """Atomically write an encoding result sidecar when CRF search converges.

    Written as ``<chunk_id>.<res>.yaml`` in the strategy output directory.
    Its presence marks the ``(chunk_id, strategy)`` pair as ``COMPLETE``
    (Req 6b.1, 6b.2).

    Args:
        output_dir:      Strategy output directory.
        chunk_id:        Chunk identifier.
        resolution:      Output resolution string (e.g. ``'1920x800'``).
        winning_attempt: Path to the winning encoded attempt ``.mkv``.
        crf:             Winning CRF value.
        quality_targets: Quality targets active at convergence time.
        metrics:         Targeted metric values for the winning attempt.
    """
    sidecar_path = output_dir / f"{chunk_id}.{resolution}.yaml"
    target_keys  = {f"{t.metric}_{t.statistic}" for t in quality_targets}
    targeted_metrics = {k: v for k, v in metrics.items() if k in target_keys}
    data = EncodingResultSidecar(
        winning_attempt=winning_attempt.name,
        crf=crf,
        quality_targets=[f"{t.metric}-{t.statistic}:{t.value}" for t in quality_targets],
        metrics=targeted_metrics,
    )
    try:
        write_yaml_atomic(sidecar_path, data.to_yaml_dict())
        _logger.debug(
            "Wrote encoding result sidecar: %s (crf=%.1f)", sidecar_path.name, crf
        )
    except Exception as e:
        _logger.warning(
            "Failed to write encoding result sidecar for %s/%s: %s",
            chunk_id, resolution, e,
        )



@dataclass
class ChunkEncodingResult:
    """Result of encoding a single chunk.

    Attributes:
        chunk_id:     Chunk identifier.
        strategy:     Strategy used.
        success:      Whether encoding succeeded.
        final_crf:    Final CRF value used.
        attempts:     Number of encoding attempts.
        encoded_file: Metadata for the final encoded attempt artifact.
        reused:       Whether existing encoding was reused.
        error:        Error message if failed.
    """

    chunk_id:     str
    strategy:     str
    success:      bool
    final_crf:    float          | None = None
    attempts:     int                   = 0
    encoded_file: AttemptMetadata | None = None
    reused:       bool                  = False
    error:        str            | None = None


@dataclass
class EncodingResult:
    """Result of encoding all chunks.

    Attributes:
        encoded_chunks: Mapping of chunk_id -> strategy -> encoded file path.
        reused_count:   Number of chunks reused from previous runs.
        encoded_count:  Number of chunks newly encoded.
        outcome:        Phase outcome.
        failed_chunks:  List of chunk IDs that failed.
        error:          Error message if pipeline failed.
    """

    encoded_chunks: dict[str, dict[str, Path]] = field(default_factory=dict)
    reused_count:   int                         = 0
    encoded_count:  int                         = 0
    outcome:        PhaseOutcome                = PhaseOutcome.COMPLETED
    failed_chunks:  list[str]                   = field(default_factory=list)
    error:          str | None                  = None


class ChunkEncoder:
    """Handles encoding of individual chunks with CRF adjustment.

    This class manages the iterative encoding process for a single chunk,
    adjusting CRF values until quality targets are met.
    """

    def __init__(
        self,
        config_manager:    ConfigManager,
        quality_evaluator: QualityEvaluator,
        work_dir:          Path,
        crop_params:       CropParams | None = None,
    ):
        """Initialize chunk encoder.

        Args:
            config_manager:    Configuration manager for strategy parsing
            quality_evaluator: Quality evaluator for metric calculation
            work_dir:          Working directory for artifacts
            crop_params:       Optional crop parameters to apply to every chunk attempt.
        """
        self.config_manager    = config_manager
        self.quality_evaluator = quality_evaluator
        self.work_dir          = work_dir
        self._crop_params      = crop_params

    def _get_output_dir(self, strategy: str) -> Path:
        """Get output directory for strategy.

        Args:
            strategy: Strategy name

        Returns:
            Path to strategy output directory
        """
        # Make strategy name filesystem-safe
        safe_strategy = strategy.replace("+", "_").replace(":", "_")
        return self.work_dir / "encoded" / safe_strategy

    def _get_attempt_path(
        self,
        chunk_id:   str,
        strategy:   str,
        resolution: str | None = None,
        crf:        float | None = None,
    ) -> Path:
        """Get the final path for a CRF-only encoded attempt.

        Naming pattern: ``<chunk_id>.<width>x<height>.crf<CRF>.mkv``

        Falls back to a simpler name when resolution or CRF are not yet known.

        Args:
            chunk_id:   Chunk identifier (e.g. ``'00꞉00꞉00․000-00꞉05꞉20․000'``).
            strategy:   Strategy name.
            resolution: Output resolution string (e.g. ``'1920x800'``).
            crf:        CRF value used for this attempt.

        Returns:
            Path to encoded file for this attempt.
        """
        output_dir = self._get_output_dir(strategy)
        if resolution and crf is not None:
            filename = f"{chunk_id}.{resolution}.crf{crf:{PADDING_CRF}}.mkv"
        else:
            filename = f"{chunk_id}.mkv"
        return output_dir / filename

    def _check_existing_encoding(
        self,
        chunk_id:   str,
        strategy:   str,
        resolution: str | None,
        crf:        float,
    ) -> AttemptMetadata | None:
        """Check if a complete encoded attempt already exists on disk.

        Scans the strategy output directory for a file matching
        ``ENCODED_ATTEMPT_NAME_PATTERN`` with the correct ``chunk_id``,
        ``resolution``, and ``crf``.  No progress-tracker lookup is performed.

        Args:
            chunk_id:   Chunk identifier.
            strategy:   Strategy name.
            resolution: Expected resolution string (e.g. ``'1920x800'``).
                        When ``None`` any resolution is accepted.
            crf:        CRF value to look for.

        Returns:
            ``AttemptMetadata`` if a matching file exists, ``None`` otherwise.
        """
        output_dir = self._get_output_dir(strategy)
        if not output_dir.exists():
            return None

        for candidate in output_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
            if m is None:
                continue
            if m.group("chunk_id") != chunk_id:
                continue
            try:
                file_crf = float(m.group("crf"))
            except ValueError:
                continue
            if abs(file_crf - crf) > 0.05:
                continue
            file_resolution = m.group("resolution")
            if resolution is not None and file_resolution != resolution:
                continue
            # Found a matching file
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            return AttemptMetadata(
                path=candidate,
                chunk_id=chunk_id,
                strategy=strategy,
                crf=file_crf,
                resolution=file_resolution,
                file_size_bytes=size,
            )
        return None

    def _load_history_from_sidecars(
        self,
        chunk_id:        str,
        strategy:        str,
        quality_targets: list[QualityTarget],
    ) -> tuple[CRFHistory, float | None]:
        """Pre-populate a ``CRFHistory`` from all existing per-attempt sidecar files.

        Scans every ``*.yaml`` in the strategy output directory that belongs to
        ``chunk_id`` and adds each attempt to the history.  Also returns the
        highest-passing CRF as the recommended starting point for the next
        search iteration.

        Per-attempt sidecars store ALL measured metrics; pass/fail is always
        re-evaluated from ``metrics`` against the current quality targets
        (Req 6a.2, 6a.3).

        Falls back to legacy ``.metrics.json`` sidecars when no YAML sidecar
        exists for an attempt (Req 8.2).

        Args:
            chunk_id:        Chunk identifier to filter sidecars.
            strategy:        Strategy name.
            quality_targets: Current quality targets for pass/fail re-evaluation.

        Returns:
            Tuple of ``(history, seed_crf)`` where ``seed_crf`` is the highest
            CRF that met targets (best efficiency found so far), or ``None`` if
            no passing attempt exists yet.
        """
        import yaml as _yaml

        output_dir = self._get_output_dir(strategy)
        history    = CRFHistory()
        seed_crf:  float | None = None

        if not output_dir.exists():
            return history, seed_crf

        required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}

        for candidate in output_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN):
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(candidate.name)
            if m is None or m.group("chunk_id") != chunk_id:
                continue

            # Try YAML sidecar first, then legacy JSON
            sidecar_data: dict | None = None
            yaml_sidecar = candidate.with_suffix(".yaml")
            if yaml_sidecar.exists():
                try:
                    with yaml_sidecar.open("r", encoding="utf-8") as fh:
                        sidecar_data = _yaml.safe_load(fh)
                except Exception:
                    pass

            if sidecar_data is None:
                import json as _json
                json_sidecar = candidate.with_suffix(".metrics.json")
                if json_sidecar.exists():
                    try:
                        with json_sidecar.open("r", encoding="utf-8") as fh:
                            sidecar_data = _json.load(fh)
                    except Exception:
                        pass

            if sidecar_data is None:
                # ARTIFACT_ONLY — attempt file exists but no sidecar; skip for history
                continue

            try:
                crf     = float(sidecar_data["crf"])
                metrics = {k: float(v) for k, v in sidecar_data.get("metrics", {}).items()}
                history.add_attempt(crf, metrics)

                # Re-evaluate pass/fail from metrics against current targets (Req 6a.2)
                if required_keys and required_keys.issubset(metrics.keys()):
                    targets_met = all(
                        metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                        for t in quality_targets
                    )
                    if targets_met:
                        if seed_crf is None or crf > seed_crf:
                            seed_crf = crf
            except Exception:
                continue

        return history, seed_crf

    def _encode_with_ffmpeg(
        self,
        chunk:           ChunkMetadata,
        strategy_config: StrategyConfig,
        crf:             float,
        output_file:     Path,
    ) -> bool:
        """Encode chunk with FFmpeg using the runner's ``.tmp``-then-rename protocol.

        The runner substitutes ``output_file`` with a ``<stem>.tmp`` sibling,
        runs ffmpeg, and renames to the final name on success.

        Args:
            chunk:           Chunk information.
            strategy_config: Strategy configuration.
            crf:             CRF value to use.
            output_file:     Intended final output path.

        Returns:
            ``True`` if encoding succeeded, ``False`` otherwise.
        """
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Build FFmpeg command
        ffmpeg_args = strategy_config.to_ffmpeg_args(crf)

        # Inject crop filter when crop params are set and non-empty
        if self._crop_params and not self._crop_params.is_empty():
            ffmpeg_args = ["-vf", self._crop_params.to_ffmpeg_filter(), *ffmpeg_args]

        cmd: list[str | Path] = [
            "ffmpeg",
            "-y",
            "-i", chunk.path,
            *ffmpeg_args,
            "-f", "matroska",
            output_file,
        ]

        _logger.debug("Encoding command: %s", " ".join(str(a) for a in cmd))

        try:
            result = run_ffmpeg(cmd, output_file=output_file)

            if not result.success:
                _logger.error(
                    "FFmpeg encoding failed with code %d for chunk %s",
                    result.returncode, chunk.chunk_id,
                )
                return False

            return True

        except Exception as e:
            _logger.error("Exception during encoding: %s", e)
            return False

    def encode_chunk(
        self,
        chunk:            ChunkMetadata,
        reference:        VideoMetadata,
        strategy:         str,
        quality_targets:  list[QualityTarget],
        initial_crf:      float = 20.0,
        force:            bool  = False,
        max_attempts:     int   = 10,
        initial_history:  "CRFHistory | None" = None,
    ) -> ChunkEncodingResult:
        """Encode single chunk, adjusting CRF until quality targets met.

        Args:
            chunk:            Chunk information.
            reference:        Reference chunk for quality comparison.
            strategy:         Strategy string (e.g., ``'slow+h265-aq'``).
            quality_targets:  Quality targets to meet.
            initial_crf:      Initial CRF value (if no history available).
            force:            If ``False``, reuse existing encoding that meets targets.
            max_attempts:     Unused; kept for API compatibility.
            initial_history:  Pre-populated ``CRFHistory`` from recovery; when provided
                              the sidecar scan is skipped and this history is used directly.

        Returns:
            ChunkEncodingResult with encoding outcome.
        """
        _logger.info(fmt_chunk_start(strategy, chunk.chunk_id))

        # Parse strategy up-front so we know the codec CRF range
        try:
            strategy_configs = self.config_manager.parse_strategy(strategy)
            if not strategy_configs:
                raise ValueError(f"Strategy '{strategy}' resolved to no configurations")
            strategy_config = strategy_configs[0]
        except ValueError as e:
            _logger.error("Invalid strategy '%s': %s", strategy, e)
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                error=str(e),
            )

        crf_min, crf_max = strategy_config.codec.crf_range

        # Pre-populate CRF history — use injected history from recovery when available,
        # otherwise scan sidecar files on disk.  This gives adjust_crf complete bounds
        # from the very first call so the search resumes correctly.
        if initial_history is not None:
            history  = initial_history
            # Derive seed_crf from the injected history (highest passing CRF)
            required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            seed_crf: float | None = None
            for attempt_crf, attempt_metrics in history.attempts:
                if required_keys and required_keys.issubset(attempt_metrics.keys()):
                    if all(attempt_metrics.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                           for t in quality_targets):
                        if seed_crf is None or attempt_crf > seed_crf:
                            seed_crf = attempt_crf
        else:
            history, seed_crf = self._load_history_from_sidecars(chunk.chunk_id, strategy, quality_targets)

        if seed_crf is not None:
            current_crf = seed_crf
            _logger.info(
                fmt_chunk(strategy, chunk.chunk_id,
                f"Restored {len(history.attempts)} attempt(s) from sidecars; resuming from best-passing CRF {current_crf:{PADDING_CRF}}")
            )
        else:
            current_crf = initial_crf
            _logger.debug(f"No prior sidecars found; starting from CRF {current_crf:{PADDING_CRF}}")

        attempt_number    = 0
        final_attempt:    AttemptMetadata | None = None
        best_crf:         float | None           = None

        while True:
            attempt_number += 1

            if attempt_number == THRESHOLD_ATTEMPTS_WARNING:
                _logger.warning(
                    fmt_chunk(strategy, chunk.chunk_id,
                              f"reached {THRESHOLD_ATTEMPTS_WARNING} attempts without meeting targets — "
                              "continuing search")
                )

            _logger.info(fmt_chunk_attempt_start(strategy, chunk.chunk_id, attempt_number, current_crf))

            # Determine the final output path for this CRF (resolution unknown yet)
            # We'll encode to a temp file, probe resolution, then rename to final path.
            output_dir = self._get_output_dir(strategy)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Probe resolution from the source chunk to build the final path before encoding
            # (so we can check for an existing file first)
            if chunk._resolution is not None:
                resolution = chunk._resolution
            else:
                resolution = chunk.resolution  # triggers lazy probe

            # When crop is active the encoded output resolution differs from the source chunk
            # resolution, so we cannot use the source resolution to match existing files.
            # Pass None to accept any resolution for the given chunk_id + crf.
            check_resolution = None if (self._crop_params and not self._crop_params.is_empty()) else resolution

            # Check for existing encoding at this CRF (filesystem scan, no tracker)
            if not force:
                existing = self._check_existing_encoding(
                    chunk.chunk_id, strategy, check_resolution, current_crf
                )
                if existing is not None:
                    sidecar = _read_metrics_sidecar(existing.path)
                    # Validate sidecar contains all required metric keys (Req 10.4)
                    required_keys = {f"{t.metric}_{t.statistic}" for t in quality_targets}
                    sidecar_valid = (
                        sidecar is not None
                        and required_keys.issubset(sidecar.get("metrics", {}).keys())
                    )
                    if sidecar_valid and sidecar is not None:
                        # Re-evaluate pass/fail from metrics against current targets (Req 6a.2)
                        metrics_dict: dict[str, float] = {
                            k: float(v) for k, v in sidecar.get("metrics", {}).items()
                        }
                        targets_met: bool = all(
                            metrics_dict.get(f"{t.metric}_{t.statistic}", 0.0) >= t.value
                            for t in quality_targets
                        )
                        metric_summary = ", ".join(f"{k}={v:.1f}" for k, v in metrics_dict.items())
                        pass_fail = (
                            f"{SUCCESS_SYMBOL_MINOR} pass"
                            if targets_met
                            else f"{FAILURE_SYMBOL_MINOR} miss"
                        )
                        best_string = ""
                        if targets_met:
                            if final_attempt is None or existing.crf > (best_crf or 0):
                                best_crf      = existing.crf
                                final_attempt = existing
                                best_string   = " NEW BEST"
                        _logger.info(
                            fmt_chunk_attempt_result(
                                strategy, chunk.chunk_id, attempt_number,
                                f"{pass_fail} with CRF {existing.crf:{PADDING_CRF}} ({metric_summary}){best_string} [reused]",
                            )
                        )
                        history.add_attempt(existing.crf, metrics_dict)
                        next_crf = adjust_crf(
                            existing.crf, metrics_dict, quality_targets, history,
                            crf_min=crf_min, crf_max=crf_max,
                        )
                        if next_crf is None:
                            if final_attempt is not None:
                                _logger.info(fmt_chunk_final(strategy, chunk.chunk_id, best_crf, attempt_number))
                                # Write encoding result sidecar — marks this pair as COMPLETE (Req 6b.1)
                                _write_encoding_result_sidecar(
                                    output_dir=self._get_output_dir(strategy),
                                    chunk_id=chunk.chunk_id,
                                    resolution=existing.resolution,
                                    winning_attempt=final_attempt.path,
                                    crf=best_crf or existing.crf,
                                    quality_targets=quality_targets,
                                    metrics=metrics_dict,
                                )
                            else:
                                _logger.warning(
                                    "CRF search space exhausted for chunk %s strategy %s after %d attempts",
                                    chunk.chunk_id, strategy, attempt_number,
                                )
                            break
                        current_crf = next_crf
                        continue
                    else:
                        # File exists but sidecar is missing or incomplete — re-evaluate metrics only
                        if sidecar is not None:
                            missing = required_keys - set(sidecar.get("metrics", {}).keys())
                            _logger.info(
                                "Found existing attempt %s (crf=%.2f) with incomplete sidecar "
                                "(missing keys: %s) — re-evaluating metrics",
                                existing.path.name, existing.crf, missing,
                            )
                        else:
                            _logger.info(
                                "Found existing attempt %s (crf=%.2f) without sidecar — re-evaluating metrics",
                                existing.path.name, existing.crf,
                            )
                        output_file = existing.path
                        # Skip encoding, jump straight to quality evaluation below
                        encode_success = True
                        # Build a placeholder so the code below can use output_file
                        goto_eval = True
                else:
                    goto_eval = False
            else:
                goto_eval = False

            if not goto_eval:
                # Build final output path (resolution known from chunk metadata)
                output_file = self._get_attempt_path(
                    chunk.chunk_id, strategy, resolution=resolution, crf=current_crf
                )
                encode_success = self._encode_with_ffmpeg(
                    chunk, strategy_config, current_crf, output_file
                )

                if not encode_success:
                    error_msg = f"Encoding failed for chunk {chunk.chunk_id}"
                    _logger.error(error_msg)
                    return ChunkEncodingResult(
                        chunk_id=chunk.chunk_id,
                        strategy=strategy,
                        success=False,
                        attempts=attempt_number,
                        error=error_msg,
                    )

                # Update resolution from actual output (crop may change dimensions)
                actual_resolution = _probe_resolution(output_file)
                if actual_resolution and actual_resolution != resolution:
                    # Rename to correct resolution-based path
                    correct_path = self._get_attempt_path(
                        chunk.chunk_id, strategy, resolution=actual_resolution, crf=current_crf
                    )
                    try:
                        output_file.replace(correct_path)
                    except OSError:
                        output_file.rename(correct_path)
                    output_file = correct_path
                    resolution  = actual_resolution

            # Evaluate quality — raw metric logs/stats go into a per-attempt subfolder;
            # the plot and YAML sidecar stay next to the .mkv (Req 8.1)
            attempt_metrics_dir = output_file.parent / output_file.stem
            attempt_plot_path   = output_file.parent / f"{output_file.stem}.png"
            evaluation  = self.quality_evaluator.evaluate_chunk(
                encoded=output_file,
                reference=reference.path,
                ref_crop=self._crop_params or CropParams(),
                targets=quality_targets,
                output_dir=attempt_metrics_dir,
                plot_path=attempt_plot_path,
            )

            # Collect ALL measured metrics (not filtered to current targets) for the sidecar
            # so the CRF history is reusable when quality targets change (Req 6a.1)
            all_metrics: dict[str, float] = {
                f"{metric.value}_{stat}": float(value)
                for metric, stats in evaluation.metrics.items()
                for stat, value in stats.items()
            }

            # Targeted metrics subset (for history and convergence decisions)
            targets_set  = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            metrics_dict = {k: v for k, v in all_metrics.items() if k in targets_set}

            # Write per-attempt metrics sidecar atomically (Req 6a.1)
            _write_metrics_sidecar(output_file, evaluation.targets_met, current_crf, all_metrics)

            # Build AttemptMetadata for this attempt
            attempt_meta = AttemptMetadata(
                path=output_file,
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                crf=current_crf,
                resolution=resolution or "",
                file_size_bytes=output_file.stat().st_size,
            )

            history.add_attempt(current_crf, metrics_dict)

            best_string = ""
            if evaluation.targets_met:
                if final_attempt is None or current_crf > (best_crf or 0):
                    best_crf      = current_crf
                    final_attempt = attempt_meta
                    best_string   = " NEW BEST"

            metric_summary = ", ".join(f"{k}={v:.1f}" for k, v in metrics_dict.items())
            pass_fail = (
                f"{SUCCESS_SYMBOL_MINOR} pass"
                if evaluation.targets_met
                else f"{FAILURE_SYMBOL_MINOR} miss"
            )
            _logger.info(
                fmt_chunk_attempt_result(
                    strategy, chunk.chunk_id, attempt_number,
                    f"{pass_fail} with CRF {current_crf:{PADDING_CRF}} ({metric_summary}){best_string}",
                )
            )

            next_crf = adjust_crf(
                current_crf, metrics_dict, quality_targets, history,
                crf_min=crf_min, crf_max=crf_max,
            )

            if next_crf is None:
                if final_attempt is not None:
                    _logger.info(fmt_chunk_final(strategy, chunk.chunk_id, best_crf, attempt_number))
                    # Write encoding result sidecar — marks this pair as COMPLETE (Req 6b.1)
                    _write_encoding_result_sidecar(
                        output_dir=self._get_output_dir(strategy),
                        chunk_id=chunk.chunk_id,
                        resolution=resolution or "",
                        winning_attempt=final_attempt.path,
                        crf=best_crf or current_crf,
                        quality_targets=quality_targets,
                        metrics=all_metrics,
                    )
                else:
                    _logger.warning(
                        "CRF search space exhausted for chunk %s strategy %s after %d attempts",
                        chunk.chunk_id, strategy, attempt_number,
                    )
                break

            _logger.debug("Adjusting CRF from %.2f to %.2f", current_crf, next_crf)
            current_crf = next_crf

        if final_attempt is not None:
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=True,
                final_crf=best_crf,
                attempts=attempt_number,
                encoded_file=final_attempt,
                reused=False,
            )
        else:
            error_msg = f"Failed to meet quality targets after {attempt_number} attempts"
            _logger.error("Chunk %s: %s", chunk.chunk_id, error_msg)
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                attempts=attempt_number,
                error=error_msg,
            )



class ChunkQueue:
    """Manages queue of chunks for parallel encoding.

    Prioritizes completing started chunks before starting new ones.
    """

    def __init__(self, chunks: list[ChunkMetadata], strategies: list[str]):
        """Initialize chunk queue.

        Args:
            chunks: List of chunks to encode
            strategies: List of strategies to apply
        """
        self.chunks = chunks
        self.strategies = strategies
        self._pending: list[tuple[ChunkMetadata, str]] = []
        self._in_progress: set[tuple[str, str]] = set()
        self._completed: set[tuple[str, str]] = set()

        # Build initial queue (all chunk+strategy combinations)
        for chunk in chunks:
            for strategy in strategies:
                self._pending.append((chunk, strategy))

    def get_next(self) -> tuple[ChunkMetadata, str] | None:
        """Get next chunk+strategy to encode.

        Prioritizes completing started chunks before starting new ones.

        Returns:
            Tuple of (chunk, strategy) or None if queue empty
        """
        if not self._pending:
            return None

        # Check if any in-progress chunks have other strategies pending
        for chunk, strategy in self._pending:
            if any((chunk.chunk_id, s) in self._in_progress for s in self.strategies):
                # This chunk has work in progress, prioritize it
                self._pending.remove((chunk, strategy))
                self._in_progress.add((chunk.chunk_id, strategy))
                return (chunk, strategy)

        # No in-progress chunks, take first pending
        chunk, strategy = self._pending.pop(0)
        self._in_progress.add((chunk.chunk_id, strategy))
        return (chunk, strategy)

    def mark_complete(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as complete.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))
        self._completed.add((chunk_id, strategy))

    def mark_failed(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as failed.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))

    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if no more work to do
        """
        return len(self._pending) == 0 and len(self._in_progress) == 0

    def get_progress(self) -> tuple[int, int]:
        """Get current progress.

        Returns:
            Tuple of (completed, total)
        """
        total = len(self.chunks) * len(self.strategies)
        completed = len(self._completed)
        return (completed, total)


async def _encode_chunk_async(
    encoder:          ChunkEncoder,
    chunk:            ChunkMetadata,
    reference:        VideoMetadata,
    strategy:         str,
    quality_targets:  list[QualityTarget],
    initial_crf:      float,
    force:            bool,
    initial_history:  "CRFHistory | None" = None,
) -> ChunkEncodingResult:
    """Async wrapper for chunk encoding.

    Args:
        encoder:          ChunkEncoder instance.
        chunk:            Chunk to encode.
        reference:        Reference chunk.
        strategy:         Strategy to use.
        quality_targets:  Quality targets.
        initial_crf:      Initial CRF value.
        force:            Whether to force re-encoding.
        initial_history:  Pre-populated ``CRFHistory`` from recovery; when provided
                          the sidecar scan is skipped and this history is used directly.

    Returns:
        ChunkEncodingResult
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        encoder.encode_chunk,
        chunk,
        reference,
        strategy,
        quality_targets,
        initial_crf,
        force,
        10,             # max_attempts (unused, kept for API compat)
        initial_history,
    )


async def _encode_chunks_parallel(
    encoder:        ChunkEncoder,
    chunks:         list[ChunkMetadata],
    reference_dir:  Path,
    strategies:     list[str],
    quality_targets: list[QualityTarget],
    max_parallel:   int,
    force:          bool,
    initial_crf:    float = 20.0,
    phase_recovery: "PhaseRecovery | None" = None,
    bar:            Callable[[int | float, AdvanceState], None] | None = None,
) -> EncodingResult:
    """Encode chunks in parallel with semaphore control.

    Args:
        encoder:        ChunkEncoder instance
        chunks:         List of chunks to encode
        reference_dir:  Directory containing reference chunks
        strategies:     List of strategies to use
        quality_targets: Quality targets to meet
        max_parallel:   Maximum concurrent encodings
        force:          Whether to force re-encoding
        phase_recovery: Optional recovery state from ``recover_attempts``; when
                        provided, ``COMPLETE`` pairs are skipped and ``ARTIFACT_ONLY``
                        pairs resume from their recovered ``CRFHistory``.
        bar:            Optional advance callable from ``ProgressBar``; called with
                        chunk duration in seconds and an ``AdvanceState`` on each
                        chunk completion.

    Returns:
        EncodingResult with all encoding outcomes
    """
    from pyqenc.phases.recovery import ArtifactState as _AS, PhaseRecovery as _PR  # local to avoid circular

    result    = EncodingResult()
    semaphore = asyncio.Semaphore(max_parallel)
    counter_failed = 0

    # Pre-populate result with COMPLETE pairs from recovery (skip them in the queue)
    complete_pairs: set[tuple[str, str]] = set()
    if phase_recovery is not None:
        for chunk in chunks:
            for strategy in strategies:
                pair_recovery = phase_recovery.pairs.get((chunk.chunk_id, strategy))
                if pair_recovery is not None and pair_recovery.state == _AS.COMPLETE:
                    _logger.info(
                        "Skipping COMPLETE pair %s/%s (encoding result sidecar valid)",
                        chunk.chunk_id, strategy,
                    )
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    # Find the winning attempt path from the recovery
                    winning_file: Path | None = None
                    for ar in reversed(pair_recovery.attempts):
                        if ar.state == _AS.COMPLETE and ar.path.exists():
                            winning_file = ar.path
                            break
                    result.encoded_chunks[chunk.chunk_id][strategy] = winning_file
                    result.reused_count += 1
                    complete_pairs.add((chunk.chunk_id, strategy))

    queue = ChunkQueue(chunks, strategies)
    # Remove already-complete pairs from the queue
    queue._pending = [
        (c, s) for (c, s) in queue._pending
        if (c.chunk_id, s) not in complete_pairs
    ]
    queue._completed = complete_pairs.copy()

    async def encode_worker() -> None:
        """Worker coroutine for encoding chunks."""
        nonlocal counter_failed
        while not queue.is_empty():
            next_item = queue.get_next()
            if next_item is None:
                break

            chunk, strategy = next_item

            async with semaphore:
                # Find reference chunk
                reference = reference_dir / chunk.path.name
                if not reference.exists():
                    _logger.error("Reference chunk not found: %s", reference)
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    if bar is not None:
                        bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
                    continue

                # Inject recovered CRFHistory for ARTIFACT_ONLY pairs (Req 6.3)
                recovered_history: CRFHistory | None = None
                if phase_recovery is not None:
                    pair_rec = phase_recovery.pairs.get((chunk.chunk_id, strategy))
                    if pair_rec is not None and pair_rec.state == _AS.ARTIFACT_ONLY:
                        recovered_history = pair_rec.history
                        _logger.info(
                            "Resuming ARTIFACT_ONLY pair %s/%s from %d recovered attempt(s)",
                            chunk.chunk_id, strategy, len(pair_rec.history.attempts),
                        )

                # Encode chunk
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    VideoMetadata(path=reference),
                    strategy,
                    quality_targets,
                    initial_crf,
                    force,
                    initial_history=recovered_history,
                )

                # Update result
                if chunk_result.success:
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    encoded_path = chunk_result.encoded_file.path if chunk_result.encoded_file else None
                    result.encoded_chunks[chunk.chunk_id][strategy] = encoded_path

                    if chunk_result.reused:
                        result.reused_count += 1
                        if bar is not None:
                            bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                    else:
                        result.encoded_count += 1
                        if bar is not None:
                            bar(chunk.end_timestamp - chunk.start_timestamp)

                    queue.mark_complete(chunk.chunk_id, strategy)
                else:
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    counter_failed += 1
                    if bar is not None:
                        bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)


    # Start worker tasks
    workers = [asyncio.create_task(encode_worker()) for _ in range(max_parallel)]

    # Wait for all workers to complete
    await asyncio.gather(*workers)

    return result


def encode_all_chunks(
    chunks:           list[ChunkMetadata],
    reference_dir:    Path,
    strategies:       list[str],
    quality_targets:  list[QualityTarget],
    work_dir:         Path,
    config_manager:   ConfigManager,
    max_parallel:     int = 2,
    force:            bool = False,
    dry_run:          bool = False,
    crop_params:      CropParams | None = None,
    state_manager:    "JobStateManager | None" = None,
    standalone:       bool = False,
) -> EncodingResult:
    """Encode all chunks with quality-targeted CRF adjustment.

    This is the main entry point for the encoding phase. It handles:
    - Pre-validating crop params against ``encoding.yaml`` (Req 3.5)
    - Writing ``encoding.yaml`` with current crop params (Req 2.4)
    - Calling ``recover_attempts`` to classify all ``(chunk, strategy)`` pairs
    - Skipping ``COMPLETE`` pairs and resuming ``ARTIFACT_ONLY`` pairs
    - Parallel encoding of chunks that need work

    When *standalone* is ``True`` (direct CLI invocation, not via the
    auto-pipeline), ``discover_inputs`` is called first to verify that the
    chunking phase has produced its outputs (Req 11.1, 11.2).

    Args:
        chunks:           List of chunks to encode
        reference_dir:    Directory containing reference chunks (already cropped)
        strategies:       List of encoding strategies to use
        quality_targets:  Quality targets to meet
        work_dir:         Working directory for artifacts
        config_manager:   Configuration manager
        max_parallel:     Maximum concurrent encoding processes
        force:            If False, reuse existing encodings that meet current targets
        dry_run:          If True, only report what would be done without encoding
        crop_params:      Crop parameters to apply uniformly to every chunk attempt.
                          When ``None``, no cropping is applied.
        state_manager:    Optional ``JobStateManager`` for reading/writing phase
                          parameter YAML files.  When provided, crop pre-validation
                          and ``encoding.yaml`` persistence are enabled.
        standalone:       If True, run inputs discovery before proceeding (Req 11.1).

    Returns:
        EncodingResult with paths to encoded chunks and statistics
    """
    from pyqenc.phases.recovery import discover_inputs, recover_attempts
    from pyqenc.state import EncodingParams, JobStateManager as _JSM

    _logger.info(
        "Encoding phase: %d chunks, %d strategies, %d quality targets",
        len(chunks), len(strategies), len(quality_targets),
    )

    # Inputs discovery — only when invoked standalone (not via auto-pipeline)
    if standalone:
        discovery = discover_inputs("encoding", work_dir)
        if not discovery.ok:
            return EncodingResult(outcome=PhaseOutcome.FAILED, error=discovery.error)

    # --- Step 1: Crop pre-validation against encoding.yaml (Req 3.5) ---
    if state_manager is not None:
        persisted_enc = state_manager.load_encoding()
        if persisted_enc is not None:
            if persisted_enc.crop != crop_params:
                if force:
                    _logger.warning(
                        "Crop params changed since last encoding run "
                        "(persisted=%s, current=%s) — --force: deleting all encoded attempt artifacts",
                        persisted_enc.crop, crop_params,
                    )
                    encoded_base = work_dir / "encoded"
                    if encoded_base.exists():
                        import shutil as _shutil
                        _shutil.rmtree(encoded_base)
                        _logger.debug("Deleted encoded directory: %s", encoded_base)
                else:
                    _logger.critical(
                        "Crop params changed since last encoding run "
                        "(persisted=%s, current=%s). "
                        "Re-run with --force to delete stale encoded artifacts and continue.",
                        persisted_enc.crop, crop_params,
                    )
                    return EncodingResult(
                        outcome=PhaseOutcome.FAILED,
                        error="Crop params mismatch — aborting. Use --force to override.",
                    )

    # --- Stale .tmp cleanup (Req 7.7) ---
    encoded_base = work_dir / "encoded"
    if encoded_base.exists():
        for tmp_file in encoded_base.rglob(f"*{TEMP_SUFFIX}"):
            _logger.warning("Removing stale temp file from previous run: %s", tmp_file.name)
            try:
                tmp_file.unlink()
            except OSError as e:
                _logger.warning("Could not remove stale temp file %s: %s", tmp_file, e)

    # --- Step 2: Write encoding.yaml with current crop params (Req 2.4) ---
    if state_manager is not None and not dry_run:
        state_manager.save_encoding(EncodingParams(crop=crop_params))
        _logger.debug("Wrote encoding.yaml (crop=%s)", crop_params)

    # --- Step 3: Artifact recovery via recover_attempts (Req 3.6) ---
    chunk_ids = [c.chunk_id for c in chunks]
    phase_recovery = recover_attempts(work_dir, chunk_ids, strategies, quality_targets)

    if dry_run:
        pending_count = len(phase_recovery.pending)
        complete_count = len(chunk_ids) * len(strategies) - pending_count
        _logger.info("[DRY-RUN] Encoding recovery: %d COMPLETE, %d pending", complete_count, pending_count)
        if pending_count == 0:
            _logger.info("[DRY-RUN] Status: Complete (all chunks already encoded)")
        else:
            _logger.info("[DRY-RUN] Status: Needs work (%d pair(s) pending)", pending_count)
        result = EncodingResult()
        result.reused_count = complete_count
        return result

    # Create encoder
    encoder = ChunkEncoder(
        config_manager=config_manager,
        quality_evaluator=QualityEvaluator(work_dir),
        work_dir=work_dir,
        crop_params=crop_params,
    )

    # Run parallel encoding — COMPLETE pairs are skipped inside _encode_chunks_parallel
    _logger.info("Starting parallel encoding with %d workers", max_parallel)
    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(strategies)
    with ProgressBar(total_seconds, title="Encoding") as advance:
        result = asyncio.run(
            _encode_chunks_parallel(
                encoder=encoder,
                chunks=chunks,
                reference_dir=reference_dir,
                strategies=strategies,
                quality_targets=quality_targets,
                max_parallel=max_parallel,
                force=force,
                initial_crf=20.0,
                phase_recovery=phase_recovery,
                bar=advance,
            )
        )

    # Log summary
    _logger.info(
        "Encoding complete: %d newly encoded, %d reused, %d failed",
        result.encoded_count, result.reused_count, len(result.failed_chunks),
    )

    if result.failed_chunks:
        _logger.error("Failed chunks: %s", ", ".join(result.failed_chunks))

    return result
