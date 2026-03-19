# Design Document — Pipeline UX Improvements

- Created: 2026-03-15
- Completed: 2026-03-15

## Overview

Four targeted improvements to the pyqenc encoding pipeline:

1. **Crop correctness** — detect crop once on the extracted stream; thread `CropParams` into `ChunkEncoder` so every chunk attempt applies it uniformly.
2. **Uniform chunk attempt logging** — standardize all per-attempt log lines to `{strategy}/{chunk} attempt {N}: …` with a distinct ✔ final-result line.
3. **Distinctive optimization summaries** — per-strategy result blocks and a final summary that are visually scannable.
4. **alive_progress bars** — live progress for chunking (scene detection spinner → finite split bar), optimization, and encoding phases.

No new external dependencies are introduced beyond `alive-progress`, which is already approved.

---

## Architecture

The changes are localized to four files with one new helper module:

```log
pyqenc/
  phases/
    encoding.py       ← crop threading + uniform logging
    optimization.py   ← crop threading + distinctive summaries + progress bar
    chunking.py       ← progress bar (scene detection + splitting)
  orchestrator.py     ← reads crop from extraction state; passes to encoding/optimization
  utils/
    log_format.py     ← NEW: shared log-formatting helpers
```

`PhaseMetadata.crop_params` (already a `str | None` field in `models.py`) is the persistence vehicle — no model changes needed.

---

## Components and Interfaces

### 1. `pyqenc/utils/log_format.py` (new)

Centralizes the two formatting concerns so neither `encoding.py` nor `optimization.py` duplicates them.

```python
def fmt_attempt(strategy: str, chunk_id: str, attempt: int, msg: str) -> str:
    """Return '{strategy}/{chunk} attempt {N}: {msg}'."""

def fmt_chunk_final(strategy: str, chunk_id: str, crf: float, attempts: int) -> str:
    """Return '✔ {strategy}/{chunk}: final CRF {crf:.2f} after {n} attempts'."""

def fmt_strategy_result_block(
    strategy: str,
    avg_crf: float,
    total_size_mb: float,
    num_chunks: int,
    passed: bool,
    error: str | None = None,
) -> list[str]:
    """Return a list of log lines forming a visually distinct strategy result block."""

def fmt_optimization_summary(
    optimal: str,
    results: dict[str, StrategyTestResult],   # from optimization.py
) -> list[str]:
    """Return a list of log lines forming the final optimization summary."""
```

The block delimiter is a line of `─` (U+2500) characters, 72 wide — narrow enough to fit 80-column terminals without wrapping.

### 2. `pyqenc/phases/encoding.py` — `ChunkEncoder`

#### Crop threading

`ChunkEncoder.__init__` gains an optional `crop_params: CropParams | None = None` parameter. It is stored as `self._crop_params`. `_encode_with_ffmpeg` applies the crop filter when `self._crop_params` is set and non-empty:

```python
# inside to_ffmpeg_args equivalent — added to the ffmpeg cmd build
if self._crop_params and not self._crop_params.is_empty():
    ffmpeg_args = ["-vf", self._crop_params.to_ffmpeg_filter(), *ffmpeg_args]
```

Because `StrategyConfig.to_ffmpeg_args` does not accept a crop filter, the crop is injected directly in `_encode_with_ffmpeg` before the strategy args, keeping `StrategyConfig` unchanged.

#### Uniform logging

All per-attempt `_logger.info` / `_logger.warning` calls inside `encode_chunk` are replaced with calls to `fmt_attempt` / `fmt_chunk_final` from `log_format.py`:

| Old message                                                       | New message                                                                                                     |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `"Encoding chunk {id} with strategy {s}"`                         | `"{s}/{id} attempt 1: starting with CRF {crf:.2f}"` (moved into loop)                                           |
| `"Chunk {id}: attempt {n}, CRF {crf:.2f}"`                        | `fmt_attempt(s, id, n, f"starting with CRF {crf:.2f}")`                                                         |
| `"Chunk {id}: new best CRF {crf:.2f} meets targets"`              | `fmt_attempt(s, id, n, f"new best CRF {crf:.2f}")`                                                              |
| `"Chunk {id}: optimal CRF found at {crf:.2f} after {n} attempts"` | `fmt_chunk_final(s, id, crf, n)` at INFO                                                                        |
| Metrics evaluation result                                         | `fmt_attempt(s, id, n, f"Metrics: vmaf={v:.1f} ssim={s:.4f} psnr={p:.1f} — {'✓ pass' if met else '✗ fail'}")` |

The `encode_chunk` signature does **not** change — `crop_params` is set at construction time.

### 3. `pyqenc/phases/optimization.py` — `find_optimal_strategy`

<!-- markdownlint-disable-next-line MD024 -->
#### Crop threading

`find_optimal_strategy` gains `crop_params: CropParams | None = None`. It passes this to `ChunkEncoder(…, crop_params=crop_params)`.

#### Parallel workers

`find_optimal_strategy` gains `max_parallel: int = 2` (same default as encoding). Test chunks within each strategy are encoded in parallel using `asyncio` + `ThreadPoolExecutor`, mirroring the `_encode_chunks_parallel` pattern already used in `encoding.py`. The strategy loop remains sequential (one strategy at a time) so per-strategy result blocks can be logged cleanly after all its chunks finish.

```python
async def _encode_strategy_chunks_parallel(
    encoder: ChunkEncoder,
    test_chunks: list[ChunkInfo],
    reference_dir: Path,
    strategy: str,
    quality_targets: list[QualityTarget],
    max_parallel: int,
    bar: ...,   # alive_bar handle
) -> StrategyTestResult: ...
```

`find_optimal_strategy` calls `asyncio.run(_encode_strategy_chunks_parallel(…))` for each strategy.

#### Distinctive summaries

After each strategy loop iteration, emit `fmt_strategy_result_block(…)` lines via `_logger.info`. After all strategies, emit `fmt_optimization_summary(…)` lines.

#### Progress bar

```python
from alive_progress import alive_bar

total = len(strategies) * len(test_chunks)
with alive_bar(total, title="Optimization", unit="chunk") as bar:
    for strategy in strategies:
        # parallel encode all test_chunks for this strategy
        # bar incremented inside _encode_strategy_chunks_parallel on each chunk completion
        result = asyncio.run(_encode_strategy_chunks_parallel(..., bar=bar))
        # log strategy result block after all chunks done
```

On failure the bar is still incremented (with a warning log) so it never stalls.

### 4. `pyqenc/phases/chunking.py` — `_chunk_video_tracked` / `split_chunks_from_state`

#### Scene detection — spinner

`detect_scenes_to_state` wraps the `detect(…)` call in an `alive_bar` with `unknown="waves"` (infinite spinner):

```python
with alive_bar(title="Scene detection", unknown="waves") as bar:
    scene_list = detect(…)
    bar()   # single tick to close
```

#### Chunk splitting — finite bar

`split_chunks_from_state` receives the boundary count before the loop, so a finite bar is used:

```python
total_chunks = len(boundaries)
with alive_bar(total_chunks, title="Chunking") as bar:
    for idx, boundary in enumerate(boundaries):
        # ... split ...
        bar(text=chunk_stem)
```

The stateless path (`_chunk_video_stateless`) gets the same treatment.

### 5. `pyqenc/orchestrator.py` — crop propagation

`_execute_extraction` already stores `crop_params` in `PhaseMetadata`. `_execute_optimization` and `_execute_encoding` need to read it back:

```python
def _resolve_crop_params(self) -> CropParams | None:
    """Read crop_params from persisted extraction phase metadata."""
    state = self.tracker._state
    if state is None:
        return None
    ext_phase = state.phases.get(Phase.EXTRACTION.value)
    if ext_phase and ext_phase.metadata and ext_phase.metadata.crop_params:
        try:
            return CropParams.parse(ext_phase.metadata.crop_params)
        except ValueError:
            logger.warning("Could not parse persisted crop_params; proceeding without crop")
    return None
```

Both `_execute_optimization` and `_execute_encoding` call `_resolve_crop_params()` and pass the result to `find_optimal_strategy` / `encode_all_chunks` respectively.

`_execute_optimization` also passes `self.config.max_parallel` to `find_optimal_strategy`.

`encode_all_chunks` in turn passes `crop_params` down to `ChunkEncoder`.

---

## Data Models

No model changes. `PhaseMetadata.crop_params: str | None` already exists and is already persisted by the extraction phase. The only change is that downstream phases now read it.

---

## Error Handling

| Scenario                                       | Behavior                                                                        |
| ---------------------------------------------- | ------------------------------------------------------------------------------- |
| `crop_params` missing from extraction metadata | Log INFO "No crop params found; encoding without crop", proceed                 |
| `crop_params` string fails to parse            | Log WARNING, proceed without crop                                               |
| Progress bar chunk fails                       | Increment bar, log WARNING with `fmt_attempt` prefix                            |
| Strategy fails all test chunks                 | `fmt_strategy_result_block` marks it as failed; excluded from optimal selection |

---

## Testing Strategy

- Unit test `log_format.py` helpers: verify output strings match expected patterns.
- Unit test `ChunkEncoder` with a mock `_encode_with_ffmpeg`: verify crop filter is injected into the ffmpeg command when `crop_params` is non-empty, and absent when `crop_params` is `None`.
- Integration smoke: run the pipeline on a short test clip and assert that `progress.json` extraction phase metadata contains `crop_params`, and that encoded chunk filenames reflect the cropped resolution.

---

## Mermaid — Data Flow for Crop Params

```mermaid
sequenceDiagram
    participant Orch as PipelineOrchestrator
    participant Ext  as extraction.extract_streams
    participant Tracker as ProgressTracker
    participant Opt  as optimization.find_optimal_strategy
    participant Enc  as encoding.encode_all_chunks
    participant CE   as ChunkEncoder

    Orch->>Ext: extract_streams(detect_crop=True)
    Ext-->>Orch: ExtractionResult(crop_params=CropParams(140,140,0,0))
    Orch->>Tracker: update_phase(extraction, metadata.crop_params="140 140 0 0")

    Orch->>Orch: _resolve_crop_params() → CropParams(140,140,0,0)
    Orch->>Opt: find_optimal_strategy(..., crop_params=CropParams(...))
    Opt->>CE: ChunkEncoder(..., crop_params=CropParams(...))
    CE->>CE: _encode_with_ffmpeg → injects -vf crop=...

    Orch->>Enc: encode_all_chunks(..., crop_params=CropParams(...))
    Enc->>CE: ChunkEncoder(..., crop_params=CropParams(...))
    CE->>CE: _encode_with_ffmpeg → injects -vf crop=...
```
