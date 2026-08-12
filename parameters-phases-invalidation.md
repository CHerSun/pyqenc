# Parameter vs Phase Invalidation

Legend:

- `I` = invalidates (triggers re-run),
- `U` = used but not tracked (no cross-run detection),
- `—` = not used

| Parameter                    | Job                                 | Extraction                    | Audio                    | Chunking           | Optimization                                                           | Encoding                    | Merge              |
| ---------------------------- | ----------------------------------- | ----------------------------- | ------------------------ | ------------------ | ---------------------------------------------------------------------- | --------------------------- | ------------------ |
| source_video                 | I (path+size mismatch → force_wipe) | —                             | —                        | —                  | —                                                                      | —                           | —                  |
| force                        | I (enables force_wipe on mismatch)  | I (via force_wipe)            | I (via force_wipe)       | I (via force_wipe) | I (via force_wipe)                                                     | I (via force_wipe)          | I (via force_wipe) |
| crop_params                  | I (stored in job.yaml)              | —                             | —                        | —                  | I (stored in optimization.yaml)                                        | I (stored in encoding.yaml) | —                  |
| include                      | —                                   | I (stored in extraction.yaml) | —                        | —                  | —                                                                      | —                           | —                  |
| exclude                      | —                                   | I (stored in extraction.yaml) | —                        | —                  | —                                                                      | —                           | —                  |
| chunking_mode                | —                                   | —                             | —                        | U ¹                | —                                                                      | —                           | —                  |
| quality_targets              | —                                   | —                             | —                        | —                  | I (stored in optimization.yaml; also deletes encoding result sidecars) | U ²                         | U ³                |
| strategies                   | —                                   | —                             | —                        | —                  | I (stored in optimization.yaml)                                        | U ⁴                         | —                  |
| strategy_selection_tolerance | —                                   | —                             | —                        | —                  | I (stored in optimization.yaml)                                        | —                           | —                  |
| optimize                     | —                                   | —                             | —                        | —                  | U (controls mode, not tracked)                                         | —                           | —                  |
| metrics_sampling             | —                                   | —                             | —                        | —                  | U                                                                      | U                           | U                  |
| max_parallel                 | —                                   | —                             | —                        | —                  | —                                                                      | U                           | —                  |
| audio_convert                | —                                   | —                             | U ⁵                      | —                  | —                                                                      | —                           | —                  |
| audio_codec                  | —                                   | —                             | I (stored in audio.yaml) | —                  | —                                                                      | —                           | —                  |
| audio_base_bitrate           | —                                   | —                             | I (stored in audio.yaml) | —                  | —                                                                      | —                           | —                  |
| cleanup                      | —                                   | —                             | —                        | —                  | —                                                                      | U                           | —                  |
| work_dir                     | U                                   | U                             | U                        | U                  | U                                                                      | U                           | U                  |
| log_level                    | —                                   | —                             | —                        | —                  | —                                                                      | —                           | —                  |
| visual_hash                  | —                                   | —                             | —                        | —                  | —                                                                      | U                           | —                  |

Notes:

¹ chunking_mode is not stored in any sidecar. Changing it without --force will leave existing chunks (produced with the old mode) classified as COMPLETE and reused as-is. Requires --force to actually re-chunk.

² quality_targets change is detected by OptimizationPhase, which deletes encoding result sidecars (.yaml per chunk/strategy pair). EncodingPhase then sees ARTIFACT_ONLY artifacts and re-evaluates them against the new targets using stored per-attempt metrics — no re-encode needed if metrics were already measured.

³ MergePhase re-measures final quality metrics against current targets, but existing merged .mkv files are reused if their sidecar is present. A target change alone doesn't wipe merge artifacts.

⁴ strategies list change is not tracked by EncodingPhase directly — it relies on OptimizationPhase to have already resolved the correct strategy set. Adding a new strategy will produce new artifacts; removing one leaves orphaned files on disk (subject to cleanup level).

⁵ audio_convert (the regex filter) is intentionally not tracked across runs — AudioEngine.build_plan() with the current filter defines the expected outputs, so the phase naturally produces/skips the right files without needing cross-run comparison.