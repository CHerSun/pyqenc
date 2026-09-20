# Implementation Plan

This document outlines the implementation tasks for the quality-based encoding pipeline. Tasks are organized to build incrementally, with each task building on previous work. The plan focuses on creating a working skeleton first, then adding functionality phase by phase.

## Task Organization

Tasks are grouped into epics representing major components. Each task references specific requirements from the requirements document.

---

- [x] 1. Project Structure and Core Infrastructure

- [x] 1.1 Set up unified project structure with pyqenc package
  - Create `pyqenc/` directory with `__init__.py`
  - Create subdirectories: `phases/`, `utils/`, `legacy/`
  - Move existing modules to `legacy/` subdirectory
  - Update imports to maintain backward compatibility
  - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

- [x] 1.2 Create core data models in `pyqenc/models.py`
  - Implement `PhaseStatus` enum
  - Implement `PipelineConfig` dataclass
  - Implement `PipelineState` dataclass
  - Implement `PhaseState`, `ChunkState`, `StrategyState`, `AttemptInfo` dataclasses
  - Implement `QualityTarget` with `parse()` method
  - Implement `StrategyConfig` with `to_ffmpeg_args()` method
  - Implement `CropParams` dataclass with parsing and ffmpeg filter generation
  - _Requirements: 6.1, 6.2, 14.3, 2.4, 2.5_

- [x] 1.3 Implement configuration manager in `pyqenc/config.py`

  - Create `CodecConfig` dataclass with presets list
  - Create `EncodingProfile` dataclass
  - Implement `ConfigManager` class with YAML loading
  - Implement profile validation
  - Implement strategy parsing with wildcard support (preset+profile*, preset, +profile*, +profile, empty)
  - Implement preset validation against codec-specific preset lists
  - Create default configuration with h264-8bit and h265-10bit codecs
  - Add default profiles: h264, h265, h265-aq, h265-anime
  - Add default_strategies section to config: ["veryslow+h264*", "slow+h265*"]
  - Store FFmpeg encoder names (libx264, libx265) in codec definitions
  - Store codec-specific preset lists
  - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9, 12.10, 12.11, 12.12, 12.13, 12.14, 12.15, 12.16, 12.17, 12.18_

- [x] 1.4 Implement progress tracker in `pyqenc/progress.py`
  - Create `ProgressTracker` class
  - Implement `load_state()` and `save_state()` with JSON serialization
  - Implement `update_phase()` method
  - Implement `update_chunk()` method
  - Implement `get_chunk_state()` method
  - Implement `get_successful_crf_average()` method
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 13.5_

- [x] 1.5 Set up logging infrastructure in `pyqenc/utils/logging.py`
  - Configure logging with multiple levels (debug, info, warning, critical)
  - Create log formatters for console and file output
  - Implement log level configuration from CLI
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5_

- [x] 1.6 Update pyproject.toml for unified package

  - Change package name to `pyqenc`
  - Add new dependencies (pydantic, pyyaml, alive-progress, psutil)
  - Configure console script entry point as `pyqenc`
  - Update Python version requirement to >=3.13
  - Configure version to be read from pyqenc/__init__.py
  - _Requirements: 1.1, 14.1, 20.1, 20.2_

- [x] 1.7 Create version management in `pyqenc/__init__.py`

  - Define __version__ constant as single source of truth
  - Export version for external access
  - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5_

---

- [x] 2. CLI Interface and Orchestrator Skeleton

- [x] 2.1 Create CLI interface in `pyqenc/cli.py`

  - Implement main argument parser with subparsers
  - Add `auto` subcommand with all pipeline arguments
  - Add phase-specific subcommands (extract, chunk, encode, audio, merge)
  - Implement dry-run flag (`-y/--execute`) with phase limit support (default: dry-run, `-y` or `-y all` for all phases, `-y N` for N phases)
  - Implement common arguments with defaults:
    - --quality-target (default: vmaf-med:98)
    - --strategies (default: from config file, empty string for all combinations)
    - --all-strategies flag (disables optimization, encodes all strategies)
    - --work-dir, --log-level, --video-filter, --audio-filter
  - Implement crop arguments (--no-crop, --crop "top bottom [left right]")
  - Add --version flag to display version from pyqenc.__version__
  - Set main process priority to below normal using psutil
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.3, 2.4, 2.5, 2.6, 4.1, 4.7, 4.8, 12.16, 20.3_

- [x] 2.2 Implement pipeline orchestrator in `pyqenc/orchestrator.py`

  - Create `PipelineOrchestrator` class
  - Implement `run()` method with dry-run and max_phases support
  - Implement `_execute_phase()` method
  - Add phase execution loop with artifact-based resumption logic
  - Add dry-run mode: scan all phases, print status, stop at first incomplete phase
  - Each phase checks for existing artifacts and reuses them if valid
  - Support configuration changes (new strategies, quality targets) by detecting incomplete work
  - _Requirements: 7.4, 7.6, 8.1, 8.2, 8.3, 8.4, 13.1, 13.2, 13.3_

- [x] 2.3 Create public API in `pyqenc/api.py`

  - Implement `run_pipeline()` function
  - Implement phase-specific API functions (extract_streams, chunk_video, etc.)
  - Add comprehensive docstrings for all public functions
  - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

---

- [x] 3. Extraction Phase

- [x] 3.1 Implement extraction phase in `pyqenc/phases/extraction.py`

  - Create `extract_streams()` function
  - Integrate with `MKVTrackExtractor` from legacy pymkvextract
  - Implement artifact detection (check for existing extracted files)
  - Implement dry-run mode (report status without extraction)
  - Implement automatic black border detection using ffmpeg cropdetect filter
  - Sample multiple frames (skip first 60s, then beginning/middle/end)
  - Use conservative crop (largest area removing all borders)
  - Support manual crop override via --crop argument
  - Support disabling crop via --no-crop flag
  - Apply regex filters for video and audio streams
  - Return `ExtractionResult` with reused/needs_work flags and crop parameters
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 17.1_

- [x] 3.2 Add extraction phase to orchestrator

  - Wire extraction phase into pipeline execution
  - Add progress tracking for extraction
  - Add logging for extraction status
  - Store crop parameters in progress tracker for use in later phases
  - _Requirements: 8.1, 8.4_

- [x] 3.3 Add extraction subcommand to CLI

  - Implement `extract` subcommand handler
  - Add extraction-specific arguments (filters, crop options)
  - Connect to extraction phase function
  - _Requirements: 1.4_

---

- [x] 4. Chunking Phase

- [x] 4.1 Create FFmpeg wrapper utilities in `pyqenc/utils/ffmpeg.py`

  - Implement scene detection using ffmpeg scenedetect filter
  - Implement frame-accurate video segmentation
  - Implement frame count verification
  - Implement crop detection helper function
  - Add error handling for ffmpeg subprocess calls
  - _Requirements: 3.1, 3.2, 3.3, 2.4_

- [x] 4.2 Implement chunking phase in `pyqenc/phases/chunking.py`

  - Create `chunk_video()` function
  - Implement scene detection and chunk splitting
  - Apply crop parameters from extraction phase during chunking
  - Store chunks in cropped form to save disk space
  - Implement chunk manifest generation (JSON)
  - Implement artifact detection (validate existing chunks)
  - Verify total frame count matches source (after cropping)
  - Implement dry-run mode
  - Return `ChunkingResult` with chunk information
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 4.3 Add chunking phase to orchestrator

  - Wire chunking phase into pipeline execution
  - Add progress tracking with frame-based updates
  - Add logging for chunking status
  - _Requirements: 8.1, 8.2, 8.4_

- [x] 4.4 Add chunk subcommand to CLI

  - Implement `chunk` subcommand handler
  - Add chunking-specific arguments (scene threshold, min length)
  - Connect to chunking phase function
  - _Requirements: 1.4_

---

- [x] 5. Quality Evaluation System

- [x] 5.1 Implement quality evaluator in `pyqenc/quality.py`

  - Create `QualityEvaluator` class
  - Implement `evaluate_chunk()` method
  - Integrate with pymkvcompare for metric generation (SSIM, PSNR, VMAF)
  - Integrate with metrics_visualization for parsing and plotting
  - Implement target evaluation logic (compare against all quality targets)
  - Note: Both encoded and reference chunks are already cropped, no additional cropping needed
  - Return `QualityEvaluation` with metrics and artifacts
  - _Requirements: 6.3, 6.4, 6.5, 6.6, 17.2, 17.3_

- [x] 5.2 Implement CRF adjustment algorithm in `pyqenc/quality.py`

  - Create `CRFHistory` class for tracking attempts
  - Implement `normalize_metric_deficit()` function (handles SSIM 0-1, PSNR dB, VMAF 0-100)
  - Implement `adjust_crf()` function with bidirectional adjustment
  - Add cycle prevention using history bounds
  - Add metric-aware step sizing (variable steps based on deficit magnitude)
  - Round CRF to 0.25 granularity
  - Implement smart CRF selection (never re-attempt previously used CRF values)
  - Use binary search between known bounds when established
  - _Requirements: 5.3, 5.4, 5.6, 6.5_

---

- [x] 6. Encoding Phase

- [x] 6.1 Implement chunk encoder in `pyqenc/phases/encoding.py`

  - Create `ChunkEncoder` class
  - Implement `encode_chunk()` method with CRF adjustment loop
  - Integrate quality evaluator for each attempt
  - Track encoding attempts in progress tracker
  - Implement artifact detection (reuse chunks meeting current targets)
  - Smart CRF selection: never re-attempt previously used CRF values for same chunk+strategy
  - Use average of successful CRFs from other chunks as starting point
  - Note: Chunks are already cropped, no additional cropping needed
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.6_

- [x] 6.2 Implement parallel encoding orchestration

  - Create chunk queue manager
  - Implement parallel execution with semaphore (max 2 concurrent by default)
  - Prioritize completing started chunks before starting new ones
  - Share successful CRF values across chunks with same strategy
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 6.3 Implement main encoding phase function

  - Create `encode_all_chunks()` function
  - Scan for existing encodings and evaluate against current targets
  - Support configuration changes: only encode chunks for new strategies or that don't meet new targets
  - Implement dry-run mode (report what would be encoded)
  - Return `EncodingResult` with reused/encoded counts
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.6, 13.1, 13.2, 13.3_

- [x] 6.4 Add encoding phase to orchestrator

  - Wire encoding phase into pipeline execution
  - Add progress tracking with chunk-level updates
  - Add detailed logging for encoding attempts
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 6.5 Add encode subcommand to CLI

  - Implement `encode` subcommand handler
  - Add encoding-specific arguments (strategies, quality targets, max parallel)
  - Connect to encoding phase function
  - _Requirements: 1.4_

---

- [x] 7. Optimization Phase

- [x] 7.1 Implement optimization phase in `pyqenc/phases/optimization.py`

  - Create `find_optimal_strategy()` function
  - Implement test chunk selection (1%, min 3, exclude first/last 10%)
  - Encode test chunks with all strategies
  - Compare average file sizes
  - Select optimal strategy
  - Return `OptimizationResult` with test results
  - Phase is enabled by default, skipped only when --all-strategies flag is used
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

- [x] 7.2 Add optimization phase to orchestrator

  - Wire optimization phase into pipeline execution (when enabled)
  - Add progress tracking for test chunk encoding
  - Add logging for strategy comparison
  - _Requirements: 8.1, 8.4_

---

- [x] 8. Audio Processing Phase

- [x] 8.1 Implement audio phase in `pyqenc/phases/audio.py`

  - Create `process_audio_streams()` function
  - Integrate with `AudioEngine` from legacy pymkva2
  - Configure strategies for day/night mode
  - Output AAC format
  - Implement artifact detection
  - Implement dry-run mode
  - Return `AudioResult` with processed audio paths
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 17.4_

- [x] 8.2 Add audio phase to orchestrator

  - Wire audio phase into pipeline execution
  - Add progress tracking
  - Add logging for audio processing
  - _Requirements: 8.1, 8.4_

- [x] 8.3 Add audio subcommand to CLI

  - Implement `audio` subcommand handler
  - Add audio-specific arguments
  - Connect to audio phase function
  - _Requirements: 1.4_

---

- [x] 9. Merging Phase

- [x] 9.1 Implement merging phase in `pyqenc/phases/merge.py`

  - Create `merge_final_video()` function
  - Implement chunk concatenation using ffmpeg concat demuxer
  - Implement audio/video muxing using mkvmerge
  - Verify final frame count matches source
  - Handle multiple strategies (produce separate outputs)
  - Measure final quality metrics by comparing merged video against original source
  - Generate visual quality metrics plot for each final output
  - Report final metrics and plot path to user
  - Implement dry-run mode
  - Return `MergeResult` with output file info, metrics, and plot path
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10_

- [x] 9.2 Add merging phase to orchestrator

  - Wire merging phase into pipeline execution
  - Add progress tracking
  - Add logging for merge status
  - _Requirements: 8.1, 8.4_

- [x] 9.3 Add merge subcommand to CLI

  - Implement `merge` subcommand handler
  - Add merge-specific arguments
  - Connect to merging phase function
  - _Requirements: 1.4_

---

- [x] 10. Documentation and User Experience

- [x] 10.1 Create comprehensive README.md

  - Explain problem and solution (quality-based encoding with automatic cropping and optimization)
  - Document installation instructions
  - Document external dependencies (ffmpeg, mkvtoolnix)
  - Provide CLI usage examples for all subcommands
  - Document quality target specification format with default (vmaf-med:98)
  - Document strategy specification format with wildcards and defaults
  - Document configuration file format with default strategies
  - Document crop detection and manual crop options
  - Document optimization behavior and --all-strategies flag
  - Add troubleshooting section
  - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 1.8, 4.1, 4.7, 4.8, 12.16_

- [x] 10.2 Create default configuration file

  - Create `pyqenc/default_config.yaml` with all codecs and profiles
  - Add default_strategies section with ["veryslow+h264*", "slow+h265*"]
  - Define codecs with FFmpeg encoder names (libx264, libx265) and codec-specific presets
  - Define profiles: h264, h265, h265-aq, h265-anime
  - Add comments explaining each section
  - Document how to customize profiles
  - Document wildcard strategy syntax
  - _Requirements: 12.1, 12.2, 12.3, 12.6, 12.16_

- [x] 10.3 Add progress reporting enhancements

  - Implement visual progress bars using alive-progress
  - Add phase transition messages
  - Add chunk encoding status updates
  - Add estimated time remaining (where possible)
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 10.4 Add input validation in `pyqenc/utils/validation.py`

  - Validate source video exists and is readable
  - Validate working directory is writable
  - Validate external tools (ffmpeg, mkvtoolnix) are available
  - Validate quality target format
  - Validate strategy format
  - Validate profile names exist in configuration
  - Validate crop parameter format
  - _Requirements: 12.4, 12.5, 2.6_

---

- [x] 11. Testing and Quality Assurance

- [x] 11.1 Create test fixtures

  - Use sample videos under `samples` folder
  - Create sample configuration files
  - Create mock metric results
  - _Requirements: All_

- [x] 11.2 Write unit tests for core components

  - Test configuration loading and validation
  - Test quality target parsing
  - Test strategy parsing
  - Test CRF adjustment algorithm
  - Test progress tracker serialization
  - Test metric normalization
  - Test crop parameter parsing and ffmpeg filter generation
  - _Requirements: All_

- [x] 11.3 Write integration tests

  - Test extraction → chunking pipeline
  - Test encoding → quality evaluation pipeline
  - Test audio processing → merge pipeline
  - Test artifact-based resumption
  - Test configuration changes (new strategies, quality targets)
  - _Requirements: All_

- [x] 11.4 Write end-to-end test

  - Test complete pipeline with small test video
  - Test dry-run mode
  - Test resumption after interruption
  - Test configuration changes (new strategies, quality targets)
  - Test crop detection and manual crop override
  - _Requirements: All_

---

- [x] 12. Final Integration and Polish

- [x] 12.1 Implement cleanup functionality

  - Add prompt for intermediate file cleanup after success
  - Implement selective cleanup (keep metrics, remove encoded chunks)
  - Add `--keep-all` flag to prevent cleanup
  - _Requirements: 13.4_

- [x] 12.2 Add disk space checking

  - Estimate required disk space before starting
  - Check available disk space
  - Warn user if space is limited
  - _Requirements: 8.1_

- [x] 12.3 Optimize performance

  - Profile encoding phase for bottlenecks
  - Tune parallel execution parameters
  - Optimize progress tracker I/O
  - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [x] 12.4 Create developer documentation

  - Create CONTRIBUTING.md for developers
  - Add architecture diagrams to docs/ directory
  - Document design decisions and rationale
  - _Requirements: 14.5, 19.1, 19.3_

- [x] 12.5 Create LICENSE file

  - Add MIT or other permissive open-source license
  - _Requirements: 19.2_

- [x] 12.6 Package and distribution

  - Test installation with uv
  - Verify console script works correctly
  - Test on clean environment
  - _Requirements: 1.1_
