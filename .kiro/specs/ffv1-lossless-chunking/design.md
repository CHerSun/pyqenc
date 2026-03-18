# Design Document

- Created: 2026-03-15
- Completed: 2026-03-15

## Overview

Chunk splitting currently uses `ffmpeg -c copy` with input-side `-ss`, which snaps each chunk's start to the nearest I-frame before the requested timestamp. With FFV1 `-g 1` every output frame is an I-frame, so the split is frame-perfect. The re-encode happens per-chunk during splitting — no intermediate whole-file FFV1 copy is created, so there is no double storage overhead.

Extraction is untouched. The pipeline data flow becomes:

```log
source.mkv
    │
    ▼ extract_streams (unchanged, -c copy)
extracted/
  video.mkv   ← original codec, original I-frame structure
  audio.mka
    │
    ▼ split_chunks_from_state (lossless mode)
chunks/
  chunk.000000-000319.mkv   ← FFV1 all-intra, frame-perfect boundary
  chunk.000320-000891.mkv
  ...
    │
    ▼ encode_all_chunks (unchanged — decoder handles FFV1 fine)
```

## Architecture

### New enum: `ChunkingMode`

Added to `pyqenc/models.py`:

```python
class ChunkingMode(Enum):
    LOSSLESS = "lossless"   # FFV1 all-intra re-encode (default)
    REMUX    = "remux"      # stream-copy, original codec preserved
```

### `PipelineConfig` change

```python
chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS
```

### `chunk_video` / `split_chunks_from_state` signature change

```python
def chunk_video(
    ...,
    chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS,
) -> ChunkingResult:

def split_chunks_from_state(
    ...,
    chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS,
) -> list[ChunkVideoMetadata]:
```

### FFV1 command construction

A module-level constant in `pyqenc/phases/chunking.py`:

```python
FFV1_VIDEO_ARGS: list[str] = [
    "-c:v",     "ffv1",
    "-g",       "1",
    "-level",   "3",
    "-coder",   "1",
    "-context", "1",
    "-slices",  "24",
]
```

The source pixel format is probed once before the split loop (fast ffprobe call on the source video) and passed as `-pix_fmt <pix_fmt>` to every chunk command. This guarantees no color space conversion regardless of source format (8-bit, 10-bit, HDR, etc.).

### Split command selection

In `split_chunks_from_state`, the existing command-building block becomes:

```python
if chunking_mode == ChunkingMode.LOSSLESS:
    pix_fmt = video_meta.pix_fmt or "yuv420p"  # lazy, cached, no extra probe
    video_args = [*FFV1_VIDEO_ARGS, "-pix_fmt", pix_fmt]
else:
    video_args = ["-c", "copy"]

cmd = [
    "ffmpeg", "-y",
    "-ss", str(start_ts),
    "-i", str(video_meta.path),
    "-t", str(duration),
    *video_args,
    "-an", str(chunk_file),
]
```

**Cropping is not applied during chunking.** Crop parameters belong to the encoding phase (where the final output dimensions are determined) and to metrics calculations (where reference and encoded frames must match). Chunking produces full-frame chunks regardless of any crop configuration. The existing `crop_params` argument and `crop_filter_args` logic in `split_chunks_from_state` must be removed as part of this change.

### Pixel format on `VideoMetadata`

`pix_fmt` is added to `VideoMetadata` as a lazy property backed by a `PrivateAttr`, following the exact same pattern as `fps` and `resolution`:

- populated by the existing `_probe_metadata()` call (same ffprobe run, no extra subprocess)
- filled opportunistically by `populate_from_ffprobe()` and `populate_from_ffmpeg_output()`
- persisted and restored via `model_dump_full()` / `model_validate_full()`

```python
_pix_fmt: str | None = PrivateAttr(default=None)

@property
def pix_fmt(self) -> str | None:
    """Pixel format of the first video stream; probed on first access."""
    if self._pix_fmt is None:
        self._probe_metadata()
    return self._pix_fmt
```

`populate_from_ffprobe` reads `stream.get("pix_fmt")` from the existing ffprobe JSON. No separate probe call or chunking-phase helper is needed.

### Disk space estimation

`estimate_required_space` in `pyqenc/utils/disk_space.py` currently uses a flat `2.2x` base overhead that bundles extraction and chunking together. With lossless chunking the chunks alone are ~5x the source, so the base overhead needs to be mode-aware.

The revised formula separates the two concerns:

```python
# Remux mode (current behavior)
chunks_multiplier = 1.0   # stream-copy ≈ same size as source video stream

# Lossless mode
chunks_multiplier = 5.0   # FFV1 all-intra, measured ~5x for 1080p h264 source

base_overhead = 1.2 + chunks_multiplier   # 1.2 = extraction + audio
```

`check_disk_space` and `log_disk_space_info` accept and forward `chunking_mode`. The orchestrator passes `self.config.chunking_mode` at the pre-flight check call site.

### Orchestrator

`_execute_chunking` no longer passes `crop_params` to `chunk_video` (crop is not a chunking concern). It passes `chunking_mode` from `self.config` instead:

```python
result = chunk_video(
    ...,
    chunking_mode=self.config.chunking_mode,
)
```

### Existing TODO resolved

`pyqenc/phases/chunking.py` has a module-level TODO comment about I-frame snapping being a known limitation of stream-copy splitting. This TODO is resolved by this change and should be removed when lossless mode is implemented.

## Key design decisions

### Why re-encode per-chunk rather than the whole extracted file?

Re-encoding the whole extracted file to FFV1 first would double the storage: ~5x source for the FFV1 intermediate, plus ~5x source again for the chunks. Re-encoding per-chunk during splitting produces only the chunks — same storage as today.

### Why FFV1 and not h264 lossless (`-crf 0`)?

FFV1 handles any pixel format and bit depth (8-bit, 10-bit, 12-bit, HDR) without special handling. h264 lossless does not support 12-bit or some HDR pixel formats. FFV1 is also the archival standard and has no patent concerns.

### Why keep remux mode?

Disk space and speed. FFV1 chunks are ~5x larger than stream-copy chunks and take longer to produce. For users processing many files or with constrained storage, remux mode is a practical escape hatch. The frame-imprecision is documented and acceptable for many use cases.

### Does the encoding phase need changes?

No. All encoders (libx264, libx265, etc.) decode FFV1 fine via ffmpeg's built-in decoder. The encoding phase just reads chunk files — it doesn't care about their codec.
