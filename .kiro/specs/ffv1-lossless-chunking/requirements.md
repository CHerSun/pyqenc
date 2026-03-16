# Requirements Document

<!-- markdownlint-disable MD024 -->

## Introduction

The chunking phase currently splits the source video using `ffmpeg -c copy` with input-side `-ss`. Because stream-copy can only cut at existing I-frames, each chunk boundary snaps to the nearest I-frame *before* the requested scene timestamp. This means chunk boundaries are not frame-perfect.

The fix is to re-encode each chunk to FFV1 lossless with `-g 1` (all-intra) during the split step. Every output frame becomes an I-frame, so the split lands exactly on the scene-change frame. Extraction remains unchanged (stream-copy). A `--remux-chunking` flag is provided to fall back to the original stream-copy behavior when disk space or encode time is a concern.

## Glossary

- **FFV1**: A lossless, all-intra video codec supported natively by ffmpeg. With `-g 1` every frame is an I-frame, enabling frame-perfect splits.
- **Lossless chunking**: The new default mode — each chunk is re-encoded to FFV1 with `-g 1 -level 3 -coder 1 -context 1 -slices 24` during the split step.
- **Remux chunking**: The legacy mode — chunks are split with `-c copy`, preserving the original codec and I-frame structure. Boundaries may be off by up to one GOP.
- **ChunkingMode**: An enum with values `LOSSLESS` and `REMUX` that controls which split path is taken.
- **Frame-perfect split**: A chunk boundary that lands on the exact frame indicated by the scene detector, with no I-frame snapping offset.

## Requirements

### Requirement 1

**User Story:** As a developer, I want chunk splitting to default to FFV1 lossless re-encoding so that each chunk starts and ends on the exact frame the scene detector identified.

#### Acceptance Criteria

1. THE Pipeline SHALL define a `ChunkingMode` enum in `pyqenc/models.py` with values `LOSSLESS` and `REMUX`.
2. THE Pipeline SHALL add a `chunking_mode: ChunkingMode` field to `PipelineConfig` defaulting to `ChunkingMode.LOSSLESS`.
3. WHEN `chunk_video` / `split_chunks_from_state` is called in lossless mode, THE Pipeline SHALL re-encode each chunk using `ffmpeg -c:v ffv1 -g 1 -level 3 -coder 1 -context 1 -slices 24` instead of `-c copy`.
4. WHEN `chunk_video` / `split_chunks_from_state` is called in remux mode, THE Pipeline SHALL use the existing `-c copy` split path unchanged.
5. THE FFV1 chunk SHALL preserve the source pixel format, color space, color primaries, and color transfer without modification. The source pixel format SHALL be obtained from `VideoMetadata.pix_fmt` (a new lazy property following the same pattern as `fps` and `resolution`) and passed explicitly as `-pix_fmt <pix_fmt>` to prevent any color space conversion.
6. THE Pipeline SHALL log the chunking mode (lossless / remux) at info level before splitting begins.
7. THE Pipeline SHALL NOT change the extraction phase in any way. Extraction continues to use `-c copy` regardless of chunking mode.
8. THE Pipeline SHALL NOT apply crop parameters during chunking. The `crop_params` argument and all crop filter logic SHALL be removed from `split_chunks_from_state` and `chunk_video`. Cropping is applied only during encoding and metrics calculation.

### Requirement 2

**User Story:** As a developer, I want a `--remux-chunking` CLI flag so that users can opt out of FFV1 re-encoding when disk space or encode time is a concern.

#### Acceptance Criteria

1. THE CLI `auto` subcommand SHALL accept a `--remux-chunking` flag (boolean, default absent = lossless mode).
2. WHEN `--remux-chunking` is passed, THE Pipeline SHALL set `chunking_mode=ChunkingMode.REMUX` in `PipelineConfig`.
3. THE CLI `chunk` subcommand SHALL also accept `--remux-chunking` with the same semantics.
4. THE CLI help text for `--remux-chunking` SHALL clearly state that lossless FFV1 is the default and that this flag trades frame-perfect splits for faster chunking and smaller intermediate chunk files.

### Requirement 3

**User Story:** As a developer, I want the existing-file reuse logic to work correctly regardless of chunking mode, so that re-running the pipeline does not re-split unnecessarily.

#### Acceptance Criteria

1. WHEN `split_chunks_from_state` is called and a chunk file already exists and is non-empty, THE Pipeline SHALL reuse it regardless of chunking mode, as it does today.
2. THE chunking mode SHALL NOT affect chunk file naming — chunk files are always named by frame range (e.g. `chunk.000000-000319.mkv`).

### Requirement 5

**User Story:** As a developer, I want disk space estimation to account for chunking mode so that the pre-flight check reflects actual expected usage.

#### Acceptance Criteria

1. THE `estimate_required_space` function in `pyqenc/utils/disk_space.py` SHALL accept a `chunking_mode: ChunkingMode` parameter.
2. WHEN `chunking_mode` is `LOSSLESS`, THE Pipeline SHALL use a chunks overhead multiplier of `5.0x` source size (reflecting FFV1 all-intra expansion, measured at ~5x for typical 1080p h264 content).
3. WHEN `chunking_mode` is `REMUX`, THE Pipeline SHALL use the existing chunks overhead multiplier of `1.0x` source size (stream-copy, near-identical size).
4. THE `check_disk_space` and `log_disk_space_info` functions SHALL accept and forward `chunking_mode` to `estimate_required_space`.
5. THE orchestrator SHALL pass `self.config.chunking_mode` when calling `log_disk_space_info`.

### Requirement 4

**User Story:** As a developer, I want tests and documentation updated to reflect the new default behavior.

#### Acceptance Criteria

1. THE existing chunking unit/integration tests SHALL be updated to pass `chunking_mode=ChunkingMode.REMUX` (or equivalent) where they test the stream-copy path, so they continue to pass without requiring a full FFV1 encode.
2. THE Pipeline SHALL add at least one unit test that verifies the FFV1 ffmpeg command is constructed correctly (correct codec flags, `-g 1`, `-pix_fmt` passthrough) without actually running ffmpeg (mock subprocess).
3. THE Pipeline SHALL add at least one integration test (marked slow / requiring ffmpeg) that verifies a short clip is chunked to FFV1 and each output chunk is a valid video with the expected frame count.
4. THE `README` or relevant docs SHALL be updated to describe the two chunking modes and the `--remux-chunking` flag.
