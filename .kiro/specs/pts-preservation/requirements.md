# Requirements Document: PTS Preservation

<!-- markdownlint-disable MD024 -->

- Created: 2026-04-29

## Introduction

The pyqenc pipeline produces a final merged MKV whose PTS (Presentation Timestamps)
differ from the source video, even though the frame count matches. This breaks
audio/subtitle sync when the output is later muxed with the original tracks.

Four root causes are addressed together:

1. The encoder resets PTS to 0 for each chunk.
2. The ffmpeg concat demuxer reconstructs a new timeline from 0.
3. `avoid_negative_ts make_zero` in the extraction command shifts the timeline.
4. A Python string concatenation bug in the merge command silently merges
   `"+genpts"` and `"-y"` into `"+genpts-y"`, so `-fflags` receives the wrong
   value and `-y` is never passed as a separate flag.

The solution has two parts:

- **Extraction phase**: remove `avoid_negative_ts make_zero`; extract per-frame
  PTS timestamps from the source using `ffprobe` and save them as
  `extracted/timestamps.txt` in `# timestamp format v2` (milliseconds, one per
  line, sorted). Also replace `mkvextract` for subtitles/chapters/attachments
  with `ffmpeg`, making extraction fully container-agnostic.

- **Merge phase**: replace the ffmpeg concat demuxer with `mkvmerge`, passing
  all arguments via a JSON options file (`@options.json`) to handle thousands of
  chunks without hitting OS command-line length limits. Apply the extracted
  timestamps file via `--timestamps 0:timestamps.txt` on the first chunk to
  restore exact source PTS. Fix the Python string concatenation bug in the
  existing ffmpeg concat command (even though it becomes dead code after the
  switch).

### Alternative solutions considered

- **Option A — fix encoder with `-copyts`**: interacts poorly with some encoders
  and does not address the concat demuxer timeline reset.
- **Option B — `setpts=PTS-STARTPTS` + inpoint/outpoint in concat list**: does
  not actually rewrite PTS in the output file.
- **Option C — `mkvmerge` append without timestamps**: handles VFR correctly but
  does not guarantee bit-perfect PTS reconstruction.
- **Option D (chosen) — extract timestamps from source + apply at merge**: bit-perfect
  PTS reconstruction, container-agnostic extraction, no new tool dependencies.

## Glossary

- **PTS (Presentation Timestamp)**: The timestamp embedded in each video frame
  that tells the player when to display it. Mismatched PTS causes audio/subtitle
  sync drift when tracks are muxed together.
- **Timestamp format v2**: A plain-text file format used by `mkvmerge --timestamps`.
  First line is `# timestamp format v2`. Subsequent lines are one integer
  millisecond timestamp per line, sorted ascending, one per frame.
- **ExtractionPhase**: The pipeline phase that extracts video, audio, subtitle,
  chapter, and attachment streams from the source file.
- **MergePhase**: The pipeline phase that concatenates encoded video chunks into
  the final MKV output.
- **TimestampArtifact**: The `extracted/timestamps.txt` file produced by
  `ExtractionPhase`. Contains per-frame PTS values from the source video in
  `# timestamp format v2` format.
- **mkvmerge options file**: A JSON array of strings passed to `mkvmerge` via
  `@options.json`. Each element is one command-line argument. Allows thousands
  of arguments without hitting OS shell length limits.
- **ffprobe**: The ffmpeg companion tool for media inspection. Used here to
  extract per-frame PTS values from the source video.
- **mkvmerge**: The MKVToolNix tool for MKV muxing and concatenation. Already a
  project dependency. Used in the merge phase to concatenate chunks and apply
  the timestamps file.
- **ArtifactState**: The four-value enum (`ABSENT`, `ARTIFACT_ONLY`, `STALE`,
  `COMPLETE`) used throughout the pipeline to classify recovery state of each
  artifact.
- **VFR (Variable Frame Rate)**: A video where the interval between frames is
  not constant. `mkvmerge` handles VFR correctly when a timestamps file is
  provided.

## Requirements

### Requirement 1: Container-Agnostic Extraction

**User Story:** As a pipeline operator, I want the extraction phase to work on
any input container (MP4, AVI, TS, MKV, WebM, etc.), so that I am not limited
to MKV/WebM sources.

#### Acceptance Criteria

1. THE ExtractionPhase SHALL use `ffmpeg` (via the unified runner) to extract
   subtitle, chapter, and attachment streams, replacing all `mkvextract` calls
   for these stream types.
2. THE ExtractionPhase SHALL continue to use `ffprobe` for stream detection,
   unchanged from the current implementation.
3. THE ExtractionPhase SHALL continue to use `ffmpeg` for video and audio track
   extraction, unchanged in tool choice.
4. THE ExtractionPhase SHALL NOT call `mkvextract` for any stream type after
   this change.
5. WHEN extracting a subtitle stream with `ffmpeg`, THE ExtractionPhase SHALL
   use `-map 0:<track_id> -c copy` and write to the appropriate output file.
6. WHEN extracting chapters with `ffmpeg`, THE ExtractionPhase SHALL use
   `-f ffmetadata` or an equivalent format that preserves chapter metadata.
7. WHEN extracting attachments with `ffmpeg`, THE ExtractionPhase SHALL use
   `-map 0:<track_id> -c copy` and write to the appropriate output file.

---

### Requirement 2: PTS Preservation in Video Extraction

**User Story:** As a pipeline operator, I want the extracted video to retain the
original PTS from the source, so that the final merged output can be reconstructed
with bit-perfect timestamps.

#### Acceptance Criteria

1. THE ExtractionPhase SHALL remove `-avoid_negative_ts make_zero` from the
   video extraction ffmpeg command.
2. THE ExtractionPhase SHALL remove `-avoid_negative_ts make_zero` from the
   audio extraction ffmpeg command.
3. THE ExtractionPhase SHALL add `-f matroska` to the video extraction ffmpeg
   command so that ffmpeg can infer the output format when writing to a `.tmp`
   file (since ffmpeg cannot infer format from the `.tmp` extension).
4. WHEN the video extraction command runs, THE ExtractionPhase SHALL NOT apply
   any PTS shifting or normalization to the extracted video stream.

---

### Requirement 3: Timestamp Artifact Extraction

**User Story:** As a pipeline operator, I want the extraction phase to produce a
`timestamps.txt` file containing per-frame PTS values from the source video, so
that the merge phase can restore exact source timestamps.

#### Acceptance Criteria

1. THE ExtractionPhase SHALL run `ffprobe` with
   `-v error -select_streams v:0 -show_packets -show_entries packet=pts_time -of csv=print_section=0`
   on the source video to obtain per-frame PTS values in seconds (floating-point).
2. THE ExtractionPhase SHALL convert the `ffprobe` output to `# timestamp format v2`:
   first line is `# timestamp format v2`, followed by one integer millisecond
   timestamp per line, sorted ascending, one per frame.
3. THE ExtractionPhase SHALL write the result to `extracted/timestamps.txt` using
   the `.tmp`-then-rename protocol for atomicity.
4. THE TimestampArtifact SHALL NOT be subject to include/exclude stream filtering —
   it is always extracted regardless of the `--include` / `--exclude` flags.
5. THE ExtractionPhase SHALL manage the TimestampArtifact with `COMPLETE` and
   `ABSENT` states only (no `STALE` state for this artifact).
6. WHEN `timestamps.txt` is present and non-empty, THE ExtractionPhase SHALL
   classify the TimestampArtifact as `COMPLETE`.
7. WHEN `timestamps.txt` is absent or empty, THE ExtractionPhase SHALL classify
   the TimestampArtifact as `ABSENT` and extract it.
8. WHEN `force_wipe` is active, THE ExtractionPhase SHALL delete `timestamps.txt`
   along with all other extraction artifacts.

---

### Requirement 4: Merge via mkvmerge with Options File

**User Story:** As a pipeline operator, I want the merge phase to use `mkvmerge`
with a JSON options file for concatenation, so that thousands of chunks can be
merged without hitting OS command-line length limits.

#### Acceptance Criteria

1. THE MergePhase SHALL use `mkvmerge` for chunk concatenation, replacing the
   ffmpeg concat demuxer.
2. THE MergePhase SHALL write all `mkvmerge` arguments to a JSON options file
   (`concat_<safe_name>.json`) in the `final/` directory, then invoke
   `mkvmerge @concat_<safe_name>.json`.
3. THE options file SHALL be a JSON array of strings where each element is one
   `mkvmerge` argument.
4. THE options file SHALL use the append syntax: the first chunk is listed
   without a prefix, and each subsequent chunk is preceded by `"+"` as a
   separate array element.
5. THE MergePhase SHALL delete the options file after a successful merge.
6. IF the merge fails, THE MergePhase SHALL leave the options file on disk for
   debugging.
7. THE MergePhase SHALL pass `-o <output_file>` as the output argument in the
   options file.
8. WHEN the number of chunks exceeds any reasonable OS argument-length limit,
   THE MergePhase SHALL still complete successfully because all arguments are
   passed via the options file, not the shell command line.

---

### Requirement 5: PTS Restoration at Merge

**User Story:** As a pipeline operator, I want the merge phase to apply the
extracted timestamps file so that the final merged video has frame-exact,
PTS-exact timestamps matching the source.

#### Acceptance Criteria

1. THE MergePhase SHALL pass `--timestamps 0:<path_to_timestamps.txt>` in the
   mkvmerge options file, applied to track 0 of the first chunk.
2. THE MergePhase SHALL read the path to `timestamps.txt` from the
   `ExtractionPhaseResult` (via the `TimestampArtifact`).
3. IF `timestamps.txt` is absent or the `ExtractionPhaseResult` does not provide
   it, THEN THE MergePhase SHALL log an error and abort the merge for that
   strategy rather than producing output with incorrect PTS.
4. THE MergePhase SHALL NOT apply `--timestamps` to any chunk other than the
   first chunk in the concatenation sequence.
5. WHEN the merge completes, THE MergePhase SHALL verify that the output file
   exists and is non-empty.

---

### Requirement 6: Frame-Exact and PTS-Exact Output

**User Story:** As a pipeline operator, I want the final merged video to have
the same frame count and PTS sequence as the source video, so that muxing with
the original audio/subtitle tracks produces correct sync.

#### Acceptance Criteria

1. WHEN the merge phase completes, THE MergePhase SHALL verify that the frame
   count of the merged output equals the frame count of the source video, and
   SHALL log a warning if they differ.
2. THE merged output SHALL have a PTS sequence that is monotonically increasing.
3. THE PTS values of the merged output SHALL match the source PTS values within
   1 ms tolerance (the precision of `# timestamp format v2`).
4. THE MergePhase SHALL handle VFR (variable frame rate) sources correctly by
   relying on `mkvmerge`'s native VFR support combined with the timestamps file.

---

### Requirement 7: Fix Python String Concatenation Bug

**User Story:** As a developer, I want the Python string concatenation bug in
the existing ffmpeg concat command to be fixed, so that the codebase is correct
even if the ffmpeg concat path becomes dead code.

#### Acceptance Criteria

1. THE MergePhase SHALL fix the adjacent string literal bug where `"+genpts"`
   and `"-y"` are written without a separating comma, causing them to be
   silently concatenated into `"+genpts-y"` at compile time.
2. AFTER the fix, the `-fflags` argument SHALL receive the value `"+genpts"` and
   `-y` SHALL be a separate element in the command list.
3. THE fix SHALL be applied regardless of whether the ffmpeg concat path is
   reachable at runtime.

---

### Requirement 8: No New External Tool Dependencies

**User Story:** As a developer, I want the PTS preservation feature to use only
already-approved tools, so that the dependency footprint does not grow.

#### Acceptance Criteria

1. THE PTS preservation implementation SHALL use only `ffmpeg`, `ffprobe`, and
   `mkvmerge` — all of which are already approved project dependencies.
2. THE implementation SHALL NOT introduce any new external tool dependencies.
3. THE implementation SHALL NOT introduce any new Python package dependencies.

---

### Requirement 9: Cross-Spec Review

**User Story:** As a developer, I want this spec reviewed against related specs
and annotated with supersession notes, so that the spec timeline is clear and
stale guidance is flagged.

#### Acceptance Criteria

1. THE spec author SHALL review this spec against `merge-phase-revamp`,
   `ffmpeg-unified-runner`, `metrics-two-tier`, and `phase-recovery-refactor`
   specs and add a cross-spec summary section at the top of this document and
   at the top of each related spec where relevant changes or supersessions exist.
2. THE cross-spec summary SHALL note the timeline (using Created/Completed dates
   or file timestamps) and describe what this spec changes relative to each
   related spec.
