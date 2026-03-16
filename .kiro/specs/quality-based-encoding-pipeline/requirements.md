# Requirements Document

<!-- markdownlint-disable MD024 -->

## Introduction

This document specifies requirements for a quality-based video encoding pipeline that orchestrates extraction, chunking, encoding, quality measurement, audio processing, and merging of h.265 video files. The pipeline aims to achieve user-specified quality targets while optimizing file size, with support for both automated end-to-end processing and manual phase-by-phase execution.

## Glossary

- **Pipeline**: The complete system that orchestrates all video processing phases from source to final output
- **Source Video**: The input MKV file containing video and audio streams to be processed
- **Chunk**: A segment of video split at scene boundaries, processed independently for encoding
- **Strategy**: A combination of encoder profile and custom settings used for video encoding
- **CRF (Constant Rate Factor)**: A quality parameter for video encoding (lower = higher quality, larger file)
- **Quality Target**: User-specified minimum acceptable quality metrics (SSIM, PSNR, or VMAF based on min or median score)
- **Working Directory**: The directory where all intermediate files and progress tracking are stored
- **Phase**: A distinct stage of the pipeline (extraction, chunking, encoding, audio processing, merging)
- **Attempt**: A single encoding trial of a chunk with specific strategy and CRF settings
- **Progress Tracker**: A persistent file storing the current state of pipeline execution
- **Profile**: A named set of encoder configuration parameters defined in the config file
- **Metric**: A quantitative measure of video quality (SSIM, PSNR, or VMAF)
- **Test Chunk**: A representative chunk used during optimal strategy selection

## Requirements

### Requirement 1

**User Story:** As a video encoder, I want a single CLI entry point with subcommands, so that I can execute the entire pipeline or individual phases as needed

#### Acceptance Criteria

1. THE Pipeline SHALL provide a CLI entry point named "pyqenc"
2. THE Pipeline SHALL provide subcommands for "extract", "chunk", "encode", "compare", "audio", "merge", and "auto"
3. WHEN the user invokes "pyqenc auto", THE Pipeline SHALL execute all phases sequentially from extraction to final merge. Continuing from last known status, if there was any prior work.
4. WHEN the user invokes a phase-specific subcommand, THE Pipeline SHALL execute only that phase with phase-specific arguments
5. THE Pipeline SHALL accept the Source Video file path as a required argument for the "auto" subcommand
6. We take actual actions only if `-y` flag is specified. Without it we only run a preview of the phase and stop. `-y 1` to approve only 1 phase running.
7. Main process starts with lowered priority, so that all subprocesses also use lowered priority - not to ruin user other interactions.
8. WHERE the user does not specify a quality target, THE Pipeline SHALL use vmaf-med:98 as the default quality target

### Requirement 2

**User Story:** As a video encoder, I want the pipeline to extract streams from the source MKV file, so that I can process video and audio independently

#### Acceptance Criteria

1. WHEN the extraction phase begins, THE Pipeline SHALL extract all video streams from the Source Video into separate files
2. WHEN the extraction phase begins, THE Pipeline SHALL extract all audio streams from the Source Video into separate files
3. This phase should accept a regex pattern for include and/or exclude to filter away unwanted things from processing (like, to keep only video and audio with ENG language; see pymkvextract)
4. THE Pipeline SHALL automatically detect black borders in the Source Video using multiple sample frames
5. WHERE black borders are detected, THE Pipeline SHALL store crop parameters in the Progress Tracker for use in subsequent phases
6. WHERE the user specifies manual crop parameters, THE Pipeline SHALL use those parameters instead of automatic detection
7. WHERE the user disables cropping, THE Pipeline SHALL skip black border detection
8. THE Pipeline SHALL store extracted streams in the Working Directory under a subdirectory named after the Source Video
9. WHEN extraction completes successfully, THE Pipeline SHALL record the extraction phase status and crop parameters as completed in the Progress Tracker
10. IF extraction fails for any stream, THEN THE Pipeline SHALL log a critical error and halt execution
11. We take actual actions only if `-y` flag is specified. Without it we only run a preview of the phase and stop

### Requirement 3

**User Story:** As a video encoder, I want the pipeline to split video into scene-based chunks, so that I can encode each segment independently while maintaining frame accuracy

#### Acceptance Criteria

1. WHEN the chunking phase begins, THE Pipeline SHALL detect scene boundaries in the extracted video stream
2. WHERE crop parameters exist from the extraction phase, THE Pipeline SHALL apply the crop filter during chunking
3. THE Pipeline SHALL split the video at detected scene boundaries into separate Chunk files
4. THE Pipeline SHALL ensure frame-perfect splitting where the sum of all Chunk frame counts equals the Source Video frame count after cropping
5. THE Pipeline SHALL store each Chunk in the Working Directory with a sequential zero-padded identifier
6. THE Pipeline SHALL store Chunks in cropped form to save disk space and processing time
7. WHEN chunking completes successfully, THE Pipeline SHALL record the chunking phase status as completed in the Progress Tracker
8. We take actual actions only if `-y` flag is specified. Without it we only run a preview of the phase and stop

### Requirement 4

**User Story:** As a video encoder, I want to search for the optimal encoding strategy automatically, so that I can achieve target quality with minimal file size

#### Acceptance Criteria

1. THE Pipeline SHALL enable optimal strategy search by default unless the all-strategies flag is used
2. WHEN optimal strategy search is enabled, THE Pipeline SHALL randomly select at least 3 Test Chunks representing approximately 1 percent of total duration
3. WHEN optimal strategy search is enabled, THE Pipeline SHALL exclude Chunks from the first 10 percent and last 10 percent of the video when selecting Test Chunks
4. WHEN optimal strategy search is enabled and encoding Test Chunks, THE Pipeline SHALL try all user-specified Strategies
5. WHEN optimal strategy search is enabled, THE Pipeline SHALL adjust CRF programmatically for each Strategy until the Quality Target is met
6. WHEN optimal strategy search is enabled and all Test Chunks meet the Quality Target, THE Pipeline SHALL select the Strategy producing the smallest average file size
7. WHERE the user specifies multiple strategies without the all-strategies flag, THE Pipeline SHALL produce output only for the optimal strategy
8. WHERE the user specifies the all-strategies flag, THE Pipeline SHALL skip optimization and encode all chunks with all specified strategies

### Requirement 5

**User Story:** As a video encoder, I want each chunk encoded to meet my quality targets, so that the final video maintains consistent quality throughout

#### Acceptance Criteria

1. WHEN encoding a Chunk with the strategy, THE Pipeline SHALL use the h.265 10-bit software encoder
2. WHEN encoding a Chunk with the strategy, THE Pipeline SHALL start with a base CRF value of 20 for the first Attempt if no previous CRF measures are available.
3. WHEN encoding a Chunk with the strategy, IF the measured quality is below the Quality Target, THEN THE Pipeline SHALL decrease CRF and re-encode
4. WHEN encoding a Chunk with the strategy, IF the measured quality significantly exceeds the Quality Target, THEN THE Pipeline SHALL increase CRF and re-encode
5. WHEN encoding a Chunk with the strategy, THE Pipeline SHALL record each Attempt with its CRF, Strategy, and resulting quality metrics in the Progress Tracker
6. WHEN choosing CRF for a new attempt - we must NOT choose previously attempted CRFs, i.e. smart choice using previous chunk-strategy attempts.
7. THE Pipeline SHALL consider a Chunk successfully encoded with the specific strategy only when all user-specified Quality Targets are met (based on minimum or median score of specific metrics).

### Requirement 6

**User Story:** As a video encoder, I want to specify quality targets using specific metrics and thresholds, so that I can control the minimum acceptable quality

#### Acceptance Criteria

1. THE Pipeline SHALL accept quality targets in the form of metric type (SSIM, PSNR, or VMAF) and metric statistic (minimum or median)
2. THE Pipeline SHALL accept multiple quality targets simultaneously
3. WHEN measuring Chunk, THE Pipeline SHALL generate SSIM, PSNR, and VMAF metric files for every encoding Attempt
4. WHEN evaluating Chunk, THE Pipeline SHALL compare measured metrics against all user-specified Quality Targets
5. THE Pipeline SHALL reject a Chunk encoding Attempt if any Quality Target is not met
6. WHEN comparing encoded and reference chunks, THE Pipeline SHALL compare them at identical dimensions since both are already cropped

### Requirement 7

**User Story:** As a video encoder, I want the pipeline to track progress persistently, so that I can resume processing after interruptions

#### Acceptance Criteria

1. THE Pipeline SHALL create a Progress Tracker file in the Working Directory at pipeline initialization
2. THE Pipeline SHALL record the current Phase and its status in the Progress Tracker
3. THE Pipeline SHALL record each Chunk status, including Strategy, Attempt number, CRF used, and resulting quality metrics scores.
4. WHEN the Pipeline starts, IF a Progress Tracker exists, THEN THE Pipeline SHALL resume from the last incomplete Phase or Chunk
5. THE Pipeline SHALL update the Progress Tracker after completing each Chunk Attempt or Phase transition
6. WHEN the Pipeline starts the phase - phase should check the progress if it is fully finished or not. User should be able to modify parameters (like, add an encoding strategy) even after full pipeline completion if he is not satisfied with results. In this case only newly added part should be processed.

### Requirement 8

**User Story:** As a video encoder, I want detailed progress reporting for each phase, so that I can monitor pipeline execution

#### Acceptance Criteria

1. WHEN a Phase begins, THE Pipeline SHALL log the Phase name and estimated total steps at info level
2. WHILE a Phase executes, THE Pipeline SHALL display live progress updates showing completed and remaining steps
3. THE Pipeline SHALL display progress using a visual progress bar with percentage completion
4. WHEN a Phase completes, THE Pipeline SHALL log the Phase completion status and summary at info level
5. THE Pipeline SHALL log detailed operation information at debug level for troubleshooting

### Requirement 9

**User Story:** As a video encoder, I want chunks encoded in parallel, so that I can reduce total processing time

#### Acceptance Criteria

1. THE Pipeline SHALL encode at least 2 Chunks concurrently during the encoding Phase
2. THE Pipeline SHALL prioritize completing started Chunks before beginning new Chunks
3. WHEN a Chunk encoding succeeds, THE Pipeline SHALL use its successful CRF (or average of all successful CRFs) as a starting point for subsequent Chunks with the same Strategy
4. THE Pipeline SHALL limit concurrent encoding processes to prevent CPU overutilization
5. THE Pipeline SHALL coordinate parallel encoding to maintain Progress Tracker consistency

### Requirement 10

**User Story:** As a video encoder, I want audio processing applied to extracted audio streams, so that I can include normalized stereo variants in the final video

#### Acceptance Criteria

1. WHEN the audio processing Phase begins, THE Pipeline SHALL apply audio Strategies to extracted audio streams
2. THE Pipeline SHALL generate normalized stereo day mode audio in AAC format
3. THE Pipeline SHALL generate normalized stereo night mode audio in AAC format
4. THE Pipeline SHALL store processed audio files in the Working Directory
5. WHEN audio processing completes successfully, THE Pipeline SHALL record the audio Phase status as completed in the Progress Tracker

### Requirement 11

**User Story:** As a video encoder, I want all encoded chunks merged into a final video with quality verification and visual plots, so that I can produce a complete output file and verify the final result meets quality targets

#### Acceptance Criteria

1. WHEN the merging Phase begins, THE Pipeline SHALL concatenate all successfully encoded Chunks in sequential order
2. THE Pipeline SHALL verify that the merged video frame count equals the Source Video frame count
3. THE Pipeline SHALL include the merged video stream in the final MKV file
4. THE Pipeline SHALL include day mode and night mode stereo audio streams in the final MKV file
5. WHERE the user requests all Strategies, THE Pipeline SHALL produce separate final MKV files for each Strategy with identical audio
6. WHEN the merging Phase completes, THE Pipeline SHALL measure quality metrics for each complete final video stream
7. WHEN measuring final video quality, THE Pipeline SHALL compare the final merged video against the original source video
8. THE Pipeline SHALL report final quality metrics (SSIM, PSNR, VMAF) for each output video to verify quality targets were met
9. THE Pipeline SHALL generate a visual quality metrics plot for each final output video showing metrics over time
10. THE Pipeline SHALL provide the plot file path to the user for quality verification

### Requirement 12

**User Story:** As a video encoder, I want to define custom encoding profiles in a configuration file and use flexible strategy specifications with wildcards, so that I can reuse encoding settings and easily test multiple configurations

#### Acceptance Criteria

1. THE Pipeline SHALL read Profile definitions from a configuration file in YAML or TOML format
2. THE Pipeline SHALL read default strategy patterns from the configuration file
3. THE Pipeline SHALL provide default Profiles: h264 (default), h265 (default), h265-aq, h265-anime
4. THE Pipeline SHALL provide a built-in default Profile for each codec with a simple name (e.g., "h265", "h264") that adds no extra encoder arguments
5. THE Pipeline SHALL store encoder-specific presets within each codec definition
6. THE Pipeline SHALL specify the exact FFmpeg encoder name for each codec (e.g., libx264, libx265)
7. THE Pipeline SHALL allow users to specify Profile names as part of video encoding Strategy definitions
8. THE Pipeline SHALL validate Profile definitions at pipeline initialization and log errors for invalid Profiles
9. THE Pipeline SHALL validate that presets are supported by the profile's codec during strategy expansion
10. THE Pipeline SHALL support flexible strategy specifications including: specific preset+profile combinations, preset with profile wildcard, all profiles with a single preset, a single profile with all presets, and all combinations of profiles and presets
11. THE Pipeline SHALL support wildcard matching for profiles using asterisk (*) character (e.g., "h265*" matches h265, h265-aq, h265-anime)
12. WHEN the user specifies a preset with profile wildcard (e.g., "slow+h265*"), THE Pipeline SHALL test all matching profiles with that preset
13. WHEN the user specifies a preset without a profile (e.g., "slow"), THE Pipeline SHALL test all available profiles with that preset, validating preset support per codec
14. WHEN the user specifies a profile wildcard without a preset (e.g., "+h265*"), THE Pipeline SHALL test all presets supported by matching profiles' codec with those profiles
15. WHEN the user specifies a profile without a preset (e.g., "+h265-aq"), THE Pipeline SHALL test all presets supported by that profile's codec with that profile
16. WHEN the user does not specify strategies, THE Pipeline SHALL use default strategies from the configuration file
17. WHEN the user specifies empty string for strategies, THE Pipeline SHALL test all available preset and profile combinations across all codecs
18. THE Pipeline SHALL support comma-separated strategy specifications to combine multiple strategy patterns

### Requirement 13

**User Story:** As a video encoder, I want all intermediate files preserved until completion, so that I can inspect results and resume if needed

#### Acceptance Criteria

1. THE Pipeline SHALL store all Chunk video files in the Working Directory until pipeline completion
2. THE Pipeline SHALL store all metric log files (SSIM, PSNR, VMAF) for every Attempt in the Working Directory
3. THE Pipeline SHALL store all metric statistics files and plots for every Attempt in the Working Directory
4. WHEN the Pipeline completes successfully, THE Pipeline SHALL notify the user that working directory can be deleted if user is satisfied
5. THE Pipeline SHALL retain the Progress Tracker file even after successful completion

### Requirement 14

**User Story:** As a developer, I want a programmatic API for the pipeline, so that I can integrate it into other processes

#### Acceptance Criteria

1. THE Pipeline SHALL provide a public API function for executing the complete end-to-end pipeline
2. THE Pipeline SHALL provide public API functions for each individual Phase
3. THE Pipeline SHALL accept all required parameters through API function arguments
4. THE Pipeline SHALL return structured results indicating success or failure with detailed error information
5. THE Pipeline SHALL document all public API functions with docstrings describing parameters, return values, and exceptions
6. THE Pipeline SHALL provide current progress.

### Requirement 15

**User Story:** As a video encoder, I want the pipeline to use CPU-based processing by default, so that I can ensure consistent results across different hardware

#### Acceptance Criteria

1. THE Pipeline SHALL use CPU-based video encoding by default
2. THE Pipeline SHALL use CPU-based scene detection for chunking by default
3. THE Pipeline SHALL use CPU-based quality metric calculation by default
4. WHERE GPU acceleration is available and provides significant performance improvement, THE Pipeline MAY use GPU for non-encoding operations. This must be transparent for end-user, requiring no extra actions on his side and providing consistent results.
5. THE Pipeline SHALL never use GPU-based encoding for video Chunks, as GPU encoding targets speed for live streaming and not quality.

### Requirement 16

**User Story:** As a video encoder, I want comprehensive logging at multiple levels, so that I can troubleshoot issues and monitor execution

#### Acceptance Criteria

1. THE Pipeline SHALL log debug-level messages for detailed operation information
2. THE Pipeline SHALL log info-level messages for Phase transitions and user-relevant status updates
3. THE Pipeline SHALL log warning-level messages for non-critical errors that allow continued execution
4. THE Pipeline SHALL log critical-level messages for errors that prevent pipeline execution
5. THE Pipeline SHALL set the default logging level to info
6. THE Pipeline SHALL allow users to specify the logging level via CLI argument

### Requirement 17

**User Story:** As a developer, I want the pipeline to reuse existing working code in modules, so that I benefit from tested functionality and approaches.

#### Acceptance Criteria

1. THE Pipeline SHALL use the pymkvextract module approaches for stream extraction
2. THE Pipeline SHALL use the pymkvcompare module approaches for quality metric generation
3. THE Pipeline SHALL use the metrics_visualization module approaches for metric statistics and plotting
4. THE Pipeline SHALL use the pymkva2 module approaches for audio Strategy processing
5. THE Pipeline SHALL can integrate approaches or reuse specific code without the requirement for unchanged code. Modules were used for testing and fine-tuning purposes previously. Public API was never their target. Pipeline SHOULD make its own API.

### Requirement 18

**User Story:** As a video encoder, I want clear documentation explaining the pipeline purpose and usage, so that I can understand how to use it effectively

#### Acceptance Criteria

1. THE Pipeline SHALL provide a README.md file describing the problem solved and target use cases
2. THE Pipeline SHALL document CLI usage with examples for each subcommand in the README.md
3. THE Pipeline SHALL document the Quality Target specification format with examples in the README.md
4. THE Pipeline SHALL document the Profile configuration file format with examples in the README.md
5. THE Pipeline SHALL document external dependencies (ffmpeg, mkvtoolnix) and installation instructions in the README.md

### Requirement 19

**User Story:** As a contributor, I want clear contribution, license and other dev-specific guidelines

#### Acceptance Criteria

1. THE Pipeline SHALL provide files describing dev-specific guidelines.
2. THE Pipeline targets to be published as open-source under permissive license (MIT?)
3. THE Pipeline SHALL reference architectural diagrams and decisions clearly in concise form.

### Requirement 20

**User Story:** As a developer and user, I want version information managed in a single location, so that version numbers are consistent across the codebase and displayed correctly

#### Acceptance Criteria

1. THE Pipeline SHALL define version information in a single source of truth file (e.g., `version.py` or `__init__.py`)
2. THE Pipeline SHALL use the single version source for package building and distribution
3. THE Pipeline SHALL display the version number when the user requests help or version information via CLI
4. THE Pipeline SHALL include the version number in the progress tracker state for compatibility tracking
5. THE Pipeline SHALL NOT duplicate version strings across multiple files
