# Chunking rework — change spec (draft for review)

Replaces the threshold-based scene detection with a two-mode statistical
chunking. Research basis: `research.md` (same folder).

## Goals

- Encoding scenes: segments homogeneous in encoding difficulty.
- Stability: nested cut family — any re-tune (k, bounds, mode) only
  subdivides; boundaries never move or vanish.
- No opaque per-source values: the end-user tunes duration bounds only.
- Both modes share ONE splitter; only the scoring differs.

## Non-goals

- No change to `build_chunks`, recovery, or downstream phases (the boundary
  list remains the only contract; scenario injection proved the existing
  sidecar shape carries new boundaries unchanged).
- The probe phase stays metadata-only — the slow-mode probe encode is
  chunking's own work, not probe's.
- No per-frame trace persistence in sidecars: traces are workspace
  intermediates (cleanup-level governed); sidecars store mode + results.

## Architecture — separation of concerns

Three seams inside ChunkingPhase:

1. **Scoring provider** (mode-dependent, config-selected):
   - `fast` (default): one decode pass producing the per-frame content-score
     trace (today's PySceneDetect dependency, used as a scorer via its stats
     manager — the trace is the splitter's only input; no threshold, and the
     library's own cut-decision logic is no longer consumed).
   - `slow`: probe encode (h265 ultrafast, fixed CRF, full res) + per-frame
     PSNR/SSIM vs source + per-packet sizes → feature traces
     [psnr, ssim, log-bits]. Cost ≈ real-time on the source (measured: a
     couple of hours for a 92-min 1080p movie at 2 cores).
2. **Splitter** (mode-independent, pure function: traces + params →
   boundaries): measure-once sliding Welch t per feature (W = min-scene per
   side; σ from adjacent disjoint window means — self-scaling per source;
   firing-feature placement) → sorted-application tree (nested
   in k) → duration bounds: hard `min_scene_duration` always;
   `target_scene_duration` via best-candidate
   descent gated by the order-statistics floor √(2·ln m) → PTS anchoring
   through the extraction timestamps index.
3. **Phase wiring**: ChunkingPhase runs provider → splitter, persists
   mode + scenes. Base cuts: in `fast` mode the statistical tree over the
   content-score trace IS the whole boundary set — the detector's linear
   cut list is retired (no threshold anywhere); in `slow` mode the content
   detector's cut list is the base layer (the validated shape: content cuts
   for ordinary scenes) with the probe-trace tree adding gate cuts.

## Config

`chunking:` section:

| key | new/removed | default | meaning |
|---|---|---|---|
| `mode` | new | `fast` | `fast` \| `slow` (extensible for future modes) |
| `min_scene_duration` | new (replaces `min_scene_length`) | 1.0 s | hard minimum, seconds |
| `target_scene_duration` | new | 15.0 s | what the splitter aims for — segments above it split (best-candidate descent) unless statistically uniform; house "target" semantics: aim, possible legitimate miss, outcome visible |
| `scene_threshold` | REMOVED | — | replaced by the statistical bar |
| `min_scene_length` | REMOVED | — | replaced by min_scene_duration |

Probe operating point (slow mode) stays internal constants for now — it is
a scoring calibration, not a user surface; revisit if a need appears.

CLI: `--scene-threshold` / `--scene-min-length` are replaced by
`--chunking-mode` and duration overrides (same one-flag-one-meaning rule as
the rest of the CLI).

## Sidecar (`chunking.yaml`)

Persists: source fingerprint (unchanged), `mode`, `min_scene_duration`,
`target_scene_duration`, `scenes` (PTS-anchored, frames + timestamps as today).
Invalidation: any of the three parameter keys changing re-detects (same
semantics as today's threshold key). The k significance bar and the floor
are algorithm constants, not config — they are not invalidation keys.

## Where the change belongs

- `phases/chunking.py`: scoring providers + splitter wiring + new sidecar
  keys; `detect_scenes()` retired.
- New module (e.g. `pyqenc/chunking_split.py` or under `utils/`): the
  splitter as a pure function — home to be settled at implementation.
- `app_config.py` + `default_config.yaml`: config keys above.
- `cli.py`: flag changes.
- Tests: property suite for the splitter (nested-in-k, bounds respected,
  Σ counts telescope, PTS anchoring) + provider smoke tests.

## Companion fixes (land independently, regardless)

1. `DURATION*` language-suffixed tag parsing in the stream inventory
   (already local, gates green).
2. Merged-output measurement applies the per-chunk first-reading skip at
   every chunk boundary (merge knows the boundary set) — removes the
   chunk-count bias in merged minima.

## Validation plan (implementation gate)

1. Fast mode on the short reference source vs stock output: boundary-set
   comparison + one search run (the content-score-under-the-tree variant is
   mechanically identical to the validated codec-trace trees but was not
   itself a validated scenario — this run closes that gap).
2. Slow mode replay on the same source must reproduce the validated
   boundary set (−4.5% ladder set).
3. Slow mode A/B already validated at movie scale (−2.21%); no rerun needed.
4. Full static + suite gates.

## Open questions

- Duration bounds in seconds (proposed) vs frames (current) — seconds
  proposed as the user-facing unit; frame conversion internal.
