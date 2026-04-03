# Design Document: Standalone Measure Command

<!-- markdownlint-disable MD024 -->

- Created: 2025-07-17
- Completed:

## Overview

This feature adds a `measure` subcommand to the pyqenc CLI. Given a source (reference) video and zero or more target (encoded) videos, it computes all supported quality metrics (currently VMAF/SSIM/PSNR, extensible), writes a metrics sidecar YAML per target, generates a quality graph per target, and captures N screenshots from the source (once) and each target at native resolution. The command reuses `QualityEvaluator.evaluate_chunk` from `pyqenc/utils/visualization.py` for all metric work, and the unified ffmpeg runner for screenshot extraction.

All outputs land under `<work_dir>/measure/` to stay isolated from pipeline phase artifacts.

---

## Module Layout

No new top-level modules. Changes are confined to:

| File | Change |
|---|---|
| `pyqenc/cli.py` | Add `_create_measure_subcommand` + `_cmd_measure`; split `_add_common_arguments` into `_add_base_arguments` + `_add_pipeline_arguments`; extract `_resolve_crop_params` helper; update `_cmd_auto` to use it |
| `pyqenc/api.py` | Add `measure_quality` public function |
| `pyqenc/phases/measure.py` | New module — all measure logic |
| `pyqenc/constants.py` | Add `MEASURE_DIR`, `SCREENSHOTS_SUBDIR_SUFFIX`, `METRICS_SUBDIR_SUFFIX`, `SCREENSHOT_TIMESTAMP_FORMAT` |
| `pyqenc/utils/log_format.py` | Update `fmt_key_value_table` to support list values with vertical alignment |

`pyqenc/phases/measure.py` is the single implementation home. `api.py` is a thin delegation layer. `cli.py` handles argument parsing and calls `api.py`.

---

## Constants

Added to `pyqenc/constants.py`:

```python
MEASURE_DIR                = "measure"
"""Output subdirectory name for standalone measure artifacts."""

METRICS_SUBDIR_SUFFIX      = ".metrics"
"""Suffix appended to target stem to form the raw metric logs subdirectory."""

SCREENSHOTS_SUBDIR_SUFFIX  = ".screenshots"
"""Suffix appended to target stem to form the screenshots subdirectory."""

SCREENSHOT_TIMESTAMP_FMT   = "{h:02d}{sep}{m:02d}{sep}{s:02d}{ms_sep}{ms:03d}"
"""Zero-padded timestamp format for screenshot filenames.

Uses TIME_SEPARATOR_SAFE and TIME_SEPARATOR_MS from constants — the same
separators used in chunk filenames — producing e.g. ``01꞉02꞉03․456``.
"""

DEFAULT_SCREENSHOT_COUNT   = 20
"""Default number of screenshots captured from each video."""

DEFAULT_METRICS_SAMPLING   = 10
"""Default frame subsampling factor for metric computation."""
```

---

## `fmt_key_value_table` Enhancement

`pyqenc/utils/log_format.py` — update the existing function signature and body:

```python
def fmt_key_value_table(kv_to_show: dict) -> None:
    """Log a key-value table at INFO level with aligned columns.

    Value dispatch (checked in this order):
    1. ``isinstance(value, str)`` → single line, formatted as-is.
    2. ``isinstance(value, list)`` → multi-line: first item on the key line,
       subsequent items on continuation lines aligned to the value column.
       Each item is formatted via f-string (``f"{item}"``), so any type works.
    3. Anything else → single line, formatted via f-string.

    Type-check order matters: str must be checked FIRST because str is iterable
    and would incorrectly satisfy a bare list check.

    Example output::

        source    /path/to/source.mkv
        targets   target_a.mkv
                  target_b.mkv
                  target_c.mkv
        crop      top=138 bottom=138
        sampling  10
    """
    max_key_len = max(len(k) for k in kv_to_show) + 1
    for key, value in kv_to_show.items():
        if isinstance(value, str):
            logger.info(f"{key:<{max_key_len}} {value}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                prefix = key if i == 0 else ""
                logger.info(f"{prefix:<{max_key_len}} {item}")
        else:
            logger.info(f"{key:<{max_key_len}} {value}")
```

All existing callers passing `str` values are unaffected. Callers can pass `list[Any]` for multi-value keys — each item is formatted by the f-string, so `Path`, `int`, custom objects with `__str__` all work without extra conversion.

---

## CLI Wiring

### Argument helper refactor

`_add_common_arguments` is split into two helpers:

```python
def _add_base_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments universal to ALL subcommands (including measure)."""
    parser.add_argument("--work-dir", ...)
    parser.add_argument("--log-level", ...)

def _add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments specific to pipeline phases (NOT used by measure)."""
    parser.add_argument("-y", "--execute", ...)
    parser.add_argument("--cleanup", ...)
    parser.add_argument("--force", ...)
    parser.add_argument("--no-metrics", ...)
```

All existing phase subcommands call both helpers (preserving current behaviour). The `measure` subcommand calls only `_add_base_arguments`.

### `_create_measure_subcommand`

```python
def _create_measure_subcommand(subparsers) -> None:
    p = subparsers.add_parser("measure", help="Measure quality metrics between source and encoded video")
    p.add_argument("source", type=Path,
        help="Reference (original/lossless) video file. ORDER MATTERS: swapping source and target produces incorrect metrics (VMAF is not symmetric)")
    p.add_argument("targets", type=Path, nargs="*", default=[],
        help="Zero or more encoded/distorted video files to evaluate against the source. "
             "Omit all to run in screenshots-only mode (no metric computation)")
    _add_base_arguments(p)     # --work-dir, --log-level only (no -y/--execute)
    _add_crop_arguments(p)     # --crop / --no-crop mutually exclusive group
    p.add_argument(
        "--metrics-sampling", type=int, default=DEFAULT_METRICS_SAMPLING, metavar="N",
        help=f"Measure every N-th frame (default: {DEFAULT_METRICS_SAMPLING})",
    )
    p.add_argument(
        "--width", type=int, default=None, metavar="W",
        help="Scale both source and target to width W (preserving aspect ratio) during metric "
             "computation. Crop is applied first. Does not affect screenshots.",
    )
    p.add_argument(
        "--screenshots", type=int, default=DEFAULT_SCREENSHOT_COUNT, metavar="N",
        help=f"Screenshots to capture from each video (default: {DEFAULT_SCREENSHOT_COUNT}, min 1)",
    )
    p.add_argument(
        "--every", type=str, default=None, metavar="DURATION",
        help="Capture one screenshot per interval (e.g. 30, 30s, 5m, 1h30m). "
             "Can be combined with --screenshots to cap the total count.",
    )
    p.set_defaults(func=_cmd_measure)
```

### `_cmd_measure`

```python
def _cmd_measure(args: argparse.Namespace) -> int:
    from pyqenc.api import measure_quality

    crop_params = _resolve_crop_params(args)   # shared helper (see below)

    try:
        result = measure_quality(
            source_video     = args.source,
            target_videos    = args.targets,
            work_dir         = args.work_dir,
            crop_params      = crop_params,
            metrics_sampling = args.metrics_sampling,
            screenshot_count = args.screenshots,
        )
        logger.info("Measure complete: %s", result.sidecar)
        return 0
    except FileNotFoundError as e:
        logger.critical("%s", e)
        return 1
    except ValueError as e:
        logger.critical("Invalid arguments: %s", e)
        return 1
    except Exception as e:
        logger.critical("Measure failed: %s", e, exc_info=True)
        return 1
```

### Crop resolution helper `_resolve_crop_params`

The crop parsing logic currently exists inline in `_cmd_auto`. It is extracted into a shared private CLI helper so all callers can reuse it without duplication:

```python
def _resolve_crop_params(args: argparse.Namespace) -> CropParams | None:
    """Parse crop parameters from CLI args into a CropParams instance.

    Returns:
        - An explicit CropParams (including empty/no-op) if --crop or --no-crop given.
        - None as a sentinel meaning "auto-resolve from job.yaml" (handled by the phase layer).

    Raises ValueError on bad --crop format (caller should catch and log critical).
    Does NOT load job.yaml — that is the responsibility of the phase layer.
    """
    if args.no_crop:
        return CropParams(top=0, bottom=0, left=0, right=0)
    if args.crop:
        return CropParams.parse(args.crop)   # raises ValueError on bad format
    return None   # sentinel: auto-resolve from job.yaml
```

Callers updated to use this helper:
- `_cmd_auto` — replaces the existing inline crop parsing block
- `_cmd_extract` — currently has `_add_crop_arguments` on its parser but no crop parsing in its handler; this helper makes it consistent
- `_cmd_measure` — new caller

---

## `pyqenc/phases/measure.py`

All logic lives here. The module is not a `Phase` subclass — it is a standalone async function `run_measure` plus supporting helpers.

### Data models

```python
@dataclass
class TargetMeasureResult:
    """Artifacts produced for a single target video."""
    target_video:      Path
    graph:             Path | None          # <target_stem>.png in Measure_Dir
    sidecar:           Path | None          # <target_stem>.yaml in Measure_Dir
    screenshots_dir:   Path                 # <target_stem>.screenshots/ in Measure_Dir
    metrics:           ChunkQualityStats    # parsed metric statistics

@dataclass
class MeasureResult:
    """All artifacts produced by a measure run."""
    source_screenshots_dir: Path                      # <source_stem>.screenshots/ in Measure_Dir
    targets:                list[TargetMeasureResult] # one entry per target video
```

### `fmt_key_value_table` enhancement

The existing `fmt_key_value_table` in `pyqenc/utils/log_format.py` is updated to support list values with vertical alignment:

```python
def fmt_key_value_table(kv_to_show: dict[str, str | list[str]]) -> None:
    """Log a key-value table at INFO level with aligned columns.

    Values may be a single string or a list of strings. For list values,
    the first item is printed on the key line; subsequent items are printed
    on continuation lines indented to align with the value column (key
    column is blank). This avoids repeating the key for each list item.

    Type-check order: check `isinstance(v, str)` first (str is iterable,
    so it would incorrectly match a list check); only then check `isinstance(v, list)`.

    Example output:
        source    /path/to/source.mkv
        targets   target_a.mkv
                  target_b.mkv
                  target_c.mkv
        crop      top=138 bottom=138
    """
    max_key_len = max(len(k) for k in kv_to_show) + 1
    for key, value in kv_to_show.items():
        if isinstance(value, str):
            logger.info(f"{key:<{max_key_len}} {value}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if i == 0:
                    logger.info(f"{key:<{max_key_len}} {item}")
                else:
                    logger.info(f"{'':<{max_key_len}} {item}")
        else:
            logger.info(f"{key:<{max_key_len}} {value!s}")
```

All existing callers passing `str` values are unaffected. New callers (including `measure`) can pass `list[str]` for multi-value keys like targets.

```python
def _check_resolution_match(
    source_meta:  VideoMetadata,
    target_meta:  VideoMetadata,
    crop_params:  CropParams,
    width:  int | None,
) -> None:
    """Verify source and target have matching effective resolution.

    Called once per target, upfront before any processing begins.
    Effective resolution = raw resolution minus crop pixels, then scaled to
    width if provided.  Raises ValueError with a specific actionable
    suggestion if they differ:
    ...
    """
```

### Crop resolution

```python
def _resolve_crop(
    crop_params: CropParams | None,
    work_dir:    Path,
    source_video: Path,
) -> CropParams:
    """Resolve final crop parameters.

    Resolution order:
    1. If crop_params is a CropParams instance (including empty/no-op): use it directly.
    2. If crop_params is None: attempt to load job.yaml from work_dir.
       - If job.yaml exists, its source_video matches source_video arg, and has
         crop data: use it, log at debug.
       - Otherwise: use empty CropParams, log at info.

    job.yaml is NEVER written or modified by this function or any measure code path.
    """
```

`JobState.load(work_dir / "job.yaml")` is used for the auto-load path. This matches how other phases access crop data.

### Metric computation

```python
def _run_metrics(
    source_video:     Path,
    target_video:     Path,
    crop_params:      CropParams,
    width:      int | None,
    metrics_dir:      Path,
    graph_path:       Path,
    subsample_factor: int,
) -> ChunkQualityStats:
```

Delegates entirely to `QualityEvaluator(measure_dir).evaluate_chunk(...)`, passing `width` through to `run_metric` via the existing `width` parameter.

```python
evaluator = QualityEvaluator(measure_dir)
evaluation = evaluator.evaluate_chunk(
    encoded          = target_video,
    reference        = source_video,
    ref_crop         = crop_params,
    targets          = [],            # no targets — pure measurement
    output_dir       = metrics_dir,
    subsample_factor = subsample_factor,
    show_progress    = True,
    plot_path        = graph_path,
)
return evaluation.metrics
```

Passing `targets=[]` means `evaluation.targets_met` is always `True` and `failed_targets` is always empty — the result is ignored. Only `evaluation.metrics` and `evaluation.artifacts` are used.

### Sidecar YAML

Written to `measure_dir / f"{target_stem}.yaml"` using `.tmp`-then-rename via `write_yaml_atomic`.

Structure:

```yaml
source_video: "/path/to/source.mkv"
target_video: "/path/to/target.mkv"
source_duration_seconds: 5823.4
target_duration_seconds: 5820.1
effective_duration_seconds: 5820.1
subsample_factor: 10
crop_params:
  top: 0
  bottom: 138
  left: 0
  right: 0
metrics:
  vmaf:
    min: 91.4
    median: 96.2
    max: 99.1
    std: 1.8
  ssim:
    min: 0.97
    median: 0.99
    max: 1.0
    std: 0.004
  psnr:
    min: 38.2
    median: 44.1
    max: inf
    std: 3.1
```

`crop_params` is serialised as a mapping of the four integer fields; `null` when no crop was applied (i.e. all zeros — stored as the dict form for clarity). Metric values are stored as raw floats (not scaled). `inf` is written as the YAML literal `inf` via pyyaml's default float serialisation.

```python
def _write_sidecar(
    path:                      Path,
    source_video:              Path,
    target_video:              Path,
    subsample_factor:          int,
    crop_params:               CropParams,
    metrics:                   ChunkQualityStats,
    source_duration_seconds:   float,
    target_duration_seconds:   float,
    effective_duration_seconds: float,
) -> None:
```

On write failure: log warning, do not raise (screenshots and graph are not lost).

### Screenshot capture

#### Duration string parsing

```python
def _parse_duration(value: str) -> float:
    """Parse a duration string to seconds.

    Accepts:
    - Plain int/float: ``"30"``, ``"90.5"``
    - Human-friendly: ``"30s"``, ``"5m"``, ``"1h"``, ``"1h30m"``, ``"1h30m45s"``

    Returns seconds as float. Raises ValueError on invalid input.
    """
```

#### Timestamp selection

```python
def _screenshot_timestamps_count(duration: float, count: int) -> list[float]:
    """Return up to `count` evenly-spaced timestamps in the interior of [0, duration].

    step = duration / (count + 1); timestamps = [step, 2*step, ..., count*step].
    Filters out any timestamp >= duration. Returns fewer than count if duration is short.
    """

def _screenshot_timestamps_interval(duration: float, interval_s: float) -> list[float]:
    """Return timestamps at [interval_s, 2*interval_s, ...] up to duration (exclusive).

    First screenshot is at 1×interval (skipping frame 0). Returns empty list if
    interval_s >= duration.
    """
```

#### Filename format

```python
def _screenshot_filename(timestamp_s: float, video_stem: str) -> str:
    total_ms  = int(timestamp_s * 1000)
    ms        = total_ms % 1000
    total_s   = total_ms // 1000
    h, rem    = divmod(total_s, 3600)
    m, s      = divmod(rem, 60)
    sep       = TIME_SEPARATOR_SAFE   # ꞉
    ms_sep    = TIME_SEPARATOR_MS     # ․
    prefix    = f"{h:02d}{sep}{m:02d}{sep}{s:02d}{ms_sep}{ms:03d}"
    return f"{prefix}_{video_stem}.png"
```

Example: timestamp 3723.456 s → `01꞉02꞉03․456_my_video.png`.

This matches the chunk filename timestamp format (`HH꞉MM꞉SS․mmm`) exactly, using the same filesystem-safe separator constants.

#### Screenshot capture strategy

All screenshots for a single video are captured in **one ffmpeg pass** using the `select` filter. This is frame-perfect (no I-frame snapping from fast-seek), works on broken containers, and avoids N separate process launches.

Two selection modes, chosen based on available metadata:

**Primary: timestamp-based** (used when fps is known OR unknown — works for normal and VFR content)
```
select='eq(t,30.0)+eq(t,60.0)+eq(t,90.0)'
```
Uses exact timestamp matching. For VFR content this selects the frame whose presentation timestamp equals the target. This is the default mode.

**Fallback: frame-number-based** (used when timestamps are not embedded in the container)
```
select='eq(n,750)+eq(n,1500)+eq(n,2250)'
```
Frame numbers derived from `round(timestamp_s * fps)`. Requires fps to be known. Used when the container has no embedded timestamps (e.g. broken/truncated encodings).

```python
async def _capture_screenshots(
    video_path:      Path,
    timestamps_s:    list[float],
    screenshots_dir: Path,
    crop_params:     CropParams | None,
    fps:             float | None,
    has_timestamps:  bool,
) -> list[Path]:
    """Capture all screenshots for one video in a single ffmpeg pass.

    Selection mode:
    - Primary (default): timestamp-based ``select='eq(t,T1)+eq(t,T2)+...'``.
      Works for normal and VFR content. Used when ``has_timestamps=True`` or
      when fps is unknown (timestamp mode is still attempted).
    - Fallback: frame-number-based ``select='eq(n,F1)+eq(n,F2)+...'``.
      Used when ``has_timestamps=False`` AND ``fps`` is known.
      Frame numbers derived from ``round(timestamp_s * fps)``.

    Crop is applied in the filter chain. No scaling.
    Uses ``-vsync 0`` to avoid frame duplication.
    Output files written as ``%04d.png`` then renamed to timestamp-based names.
    """
```

The filter chain (timestamp mode with crop):
```
select='eq(t,30.0)+eq(t,60.0)+eq(t,90.0)',crop=iw:ih-276:0:138,setpts=N/FRAME_RATE/TB
```

Output files are written sequentially (`%04d.png` pattern into a temp subdir) then renamed to the `<HH꞉MM꞉SS․mmm>_<stem>.png` format using the known timestamp list. The `.tmp`-then-rename protocol applies to the final named files.

#### Capture loop

Screenshots for source and all targets are captured sequentially (not concurrently) to avoid saturating I/O. A single shared timestamp list is used for all videos so frames are directly comparable. Existing output files are overwritten — there is no skip-if-exists logic.

```python
async def _capture_screenshots(
    video_path:       Path,
    timestamps:       list[float],
    screenshots_dir:  Path,
    crop_params:      CropParams | None = None,
) -> list[Path]:
    """Capture screenshots from a single video at the given timestamps.

    Returns list of successfully written paths.
    Called once for source and once per target.
    """
```

### `run_measure` — top-level async entry point

```python
async def run_measure(
    source_video:        Path,
    target_videos:       list[Path],
    work_dir:            Path,
    crop_params:         CropParams | None,
    metrics_sampling:    int,
    width:               int | None,
    screenshot_count:    int | None,
    screenshot_interval: float | None,
) -> MeasureResult:
    """Execute a standalone quality measurement run.

    Args:
        source_video:        Reference video path.
        target_videos:       Encoded videos to evaluate. When empty, runs in
                             screenshots-only mode (no metrics, graph, or sidecar).
        work_dir:            Working directory; outputs go under work_dir/measure/.
        crop_params:         Explicit crop (or empty CropParams for no-crop).
                             Pass None to auto-load from job.yaml if present.
        metrics_sampling:    Frame subsampling factor (≥1). Ignored in screenshots-only mode.
        width:               Scale both inputs to this width during metric computation
                             (after cropping). None = no scaling. Does not affect screenshots.
                             Ignored in screenshots-only mode.
        screenshot_count:    Screenshots per video in count mode (≥1), or cap in interval mode.
        screenshot_interval: Interval in seconds between screenshots in interval
                             mode (>0). None = count mode.
        dry_run:             If True, log planned actions and return without writing.

    Returns:
        MeasureResult with source screenshots and per-target results.

    Raises:
        FileNotFoundError: If source_video or any path in target_videos does not exist.
        ValueError:        If metrics_sampling < 1, any resolution mismatch, or
                           invalid screenshot parameters.
    """
```

Execution flow:

1. Validate inputs: raise `FileNotFoundError` if `source_video` missing or any path in `target_videos` missing. Raise `ValueError` if `metrics_sampling < 1`.
2. Resolve crop via `_resolve_crop(crop_params, work_dir, source_video)`.
3. Compute output paths per target:
   - `measure_dir                  = work_dir / MEASURE_DIR`
   - `metrics_dir[t]               = measure_dir / f"{target_stem}{METRICS_SUBDIR_SUFFIX}"`
   - `target_screenshots_dir[t]    = measure_dir / f"{target_stem}{SCREENSHOTS_SUBDIR_SUFFIX}"`
   - `source_screenshots_dir       = measure_dir / f"{source_stem}{SCREENSHOTS_SUBDIR_SUFFIX}"`
   - `graph_path[t]                = measure_dir / f"{target_stem}.png"`
   - `sidecar_path[t]              = measure_dir / f"{target_stem}.yaml"`
4. Probe resolutions of source and ALL targets upfront. Call `_check_resolution_match(source_meta, target_meta, crop_params, width)` for each target. If any check fails, log a critical error for that target and stop entirely — no processing for any target.
6. Log key-value parameter summary via `fmt_key_value_table` at info level: source path, number of targets, target stems, crop, width scaling, screenshot mode.
7. Create directories: `measure_dir`, per-target `metrics_dir`, per-target `target_screenshots_dir`, `source_screenshots_dir`.
8. Probe durations of source and all targets. Compute `effective_duration[t] = min(source_duration, target_duration)` per target. Compute `shared_duration = min(effective_duration[t] for all t)` for screenshot timestamp generation. Log warning if any pair differs by >1s.
9. Compute shared screenshot timestamps from `shared_duration` using `_screenshot_timestamps_count` or `_screenshot_timestamps_interval`.
10. Capture source screenshots once using the shared timestamps → `source_screenshots_dir`.
11. For each target sequentially:
    a. Run metrics via `_run_metrics(...)` → `ChunkQualityStats`.
    b. Write sidecar via `_write_sidecar(...)` (warn on failure, continue).
    c. Capture target screenshots using the shared timestamps → `target_screenshots_dir[t]`.
12. Log summary at info level: metrics captured per target, total screenshot count.
13. Return `MeasureResult`.

---

## `pyqenc/api.py` — `measure_quality`

```python
def measure_quality(
    source_video:        Path,
    target_videos:       list[Path]        = [],
    work_dir:            Path              = Path("."),
    crop_params:         CropParams | None = None,
    metrics_sampling:    int               = DEFAULT_METRICS_SAMPLING,
    screenshot_count:    int | None        = DEFAULT_SCREENSHOT_COUNT,
    screenshot_interval: float | None      = None,
    dry_run:             bool              = False,
) -> MeasureResult:
    """Measure quality metrics between a source and one or more encoded videos.

    Computes VMAF, SSIM, and PSNR metrics for each target, writes a metrics
    sidecar YAML per target, generates a quality graph per target, and captures
    screenshots from the source (once, shared timestamps) and each target.

    All outputs are written under ``work_dir/measure/``.

    Args:
        source_video:     Path to the reference (original) video file.
        target_videos:    Paths to encoded/distorted videos to evaluate. Pass an
                          empty list to run in screenshots-only mode.
        work_dir:         Working directory. Outputs go under ``work_dir/measure/``.
        crop_params:      Crop parameters applied to the source during metric
                          computation. Pass ``None`` to auto-load from
                          ``job.yaml`` in ``work_dir`` if present; pass an
                          empty ``CropParams`` to explicitly disable cropping.
        metrics_sampling: Frame subsampling factor (≥1, default 10).
        screenshot_count: Screenshots to capture from each video (≥1, default 20).
        screenshot_interval: Interval in seconds between screenshots in interval
                          mode. None = count mode.

    Returns:
        ``MeasureResult`` containing source screenshots directory and per-target results
        (graph, sidecar, screenshots directory, metrics for each target).

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``target_videos`` does not exist.
        ValueError:        If ``metrics_sampling`` < 1 or ``screenshot_count`` < 1.
    """
    from pyqenc.phases.measure import run_measure
    import asyncio

    work_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_measure(
        source_video     = source_video,
        target_videos    = target_videos,
        work_dir         = work_dir,
        crop_params      = crop_params,
        metrics_sampling = metrics_sampling,
        screenshot_count = screenshot_count,
    ))
```

`asyncio.run` is used here because `api.py` functions are sync entry points (matching the pattern of other API functions that call `asyncio.run` internally via `QualityEvaluator.evaluate_chunk`).

---

## Output Layout

```
<work_dir>/
└── measure/
    ├── <target1_stem>.png          ← Metrics_Graph (per target)
    ├── <target1_stem>.yaml         ← Metrics_Sidecar (per target)
    ├── <target2_stem>.png
    ├── <target2_stem>.yaml
    ├── <target1_stem>.metrics/
    │   ├── <target1_stem>.psnr.log
    │   ├── <target1_stem>.ssim.log
    │   └── <target1_stem>.vmaf.json
    ├── <target2_stem>.metrics/
    │   ├── <target2_stem>.psnr.log
    │   ├── <target2_stem>.ssim.log
    │   └── <target2_stem>.vmaf.json
    ├── <target1_stem>.screenshots/
    │   ├── 00꞉05꞉12․000_target1_video.png
    │   └── ...
    ├── <target2_stem>.screenshots/
    │   ├── 00꞉05꞉12․000_target2_video.png
    │   └── ...
    └── <source_stem>.screenshots/  ← captured once, shared timestamps
        ├── 00꞉05꞉12․000_source_video.png
        └── ...
```

Screenshots from source and all targets share the same timestamp prefix, so they sort together and are directly comparable by filename.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `source_video` not found | Raise `FileNotFoundError` (propagates to CLI → critical log + exit 1) |
| Any path in `target_videos` not found | Raise `FileNotFoundError` |
| `metrics_sampling < 1` | Raise `ValueError` |
| `screenshot_count < 1` | Raise `ValueError` |
| Resolution mismatch after crop (any target) | Raise `ValueError` with actionable message for that target; stop all processing → CLI catches → critical + exit 1 |
| Metric computation fails entirely | `QualityEvaluator` raises; propagates → critical + exit 1 |
| Sidecar write fails | Log warning, continue (graph and screenshots not lost) |
| Individual screenshot fails | Log warning, continue capturing remaining |
| Duration probe fails for one video | Log warning; if `--every` mode: proceed (ffmpeg stops at EOF); if count mode: log error and skip screenshots. Metric computation continues unaffected. |
| Both duration probes fail | Same as above; `effective_duration` recorded as null in sidecar. |
| fps unavailable for a target video | Log warning; fall back to source fps with a note. If source fps also unavailable, frame-number selection is skipped and timestamp-based selection is used regardless. |

---

## Correctness Properties

### Property 1: Screenshot timestamp distribution

For any `duration > 0` and `count ≥ 1`, `_screenshot_timestamps(duration, count)` must return exactly `count` values, all strictly greater than `0` and strictly less than `duration`, and evenly spaced with step `duration / (count + 1)`.

**Validates: Requirement 7 AC #2**

### Property 2: Screenshot filename sort order

For any set of timestamps from the same video, sorting the filenames produced by `_screenshot_filename` lexicographically must yield the same order as sorting the timestamps numerically.

**Validates: Requirement 7 AC #5** (zero-padded for consistent sorting)

---

## Testing Strategy

### Unit tests (`tests/test_measure.py`)

- `_screenshot_timestamps` returns correct count, all values in `(0, duration)`, evenly spaced.
- `_screenshot_filename` produces correctly zero-padded `HH꞉MM꞉SS․mmm_stem.png` strings using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS`.
- `_screenshot_filename` sort order matches timestamp sort order (property test via `hypothesis`).
- `_resolve_crop` with explicit `CropParams` returns it unchanged.
- `_resolve_crop` with `None` and no `job.yaml` returns empty `CropParams` and logs info.
- `_resolve_crop` with `None` and a `job.yaml` containing crop data returns those params.
- `_write_sidecar` write failure (mocked `write_yaml_atomic` raising `OSError`) logs warning and does not raise.

### Integration notes

- Full integration test requires real video files; marked `@pytest.mark.integration` and skipped in CI unless `PYQENC_INTEGRATION=1`.
- Property tests use `hypothesis` (already in use in the project).
