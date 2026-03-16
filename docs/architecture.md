# Architecture Documentation

This document describes the architecture, design decisions, and key flows of the pyqenc quality-based encoding pipeline.

## Table of Contents

- [System Overview](#system-overview)
- [Architecture Diagrams](#architecture-diagrams)
- [Component Details](#component-details)
- [Key Flows](#key-flows)
- [Design Decisions](#design-decisions)
- [Performance Considerations](#performance-considerations)

## System Overview

The pyqenc pipeline is a quality-first video encoding system that orchestrates multiple phases to achieve user-specified quality targets while optimizing file size. The system follows a phased pipeline architecture with artifact-based resumption.

### Key Characteristics

- **Quality-First**: Never compromises on user-specified quality targets
- **Resumable**: Automatically resumes from interruptions using artifact detection
- **Modular**: Each phase is independent with clear APIs
- **Transparent**: Preserves all intermediate artifacts for inspection
- **Content-Aware**: Automatically detects and removes black borders
- **Parallel**: Encodes multiple chunks concurrently

## Architecture Diagrams

### System Context

```log
┌─────────────┐
│    User     │
└──────┬──────┘
       │
       │ CLI Commands / API Calls
       │
       ▼
┌─────────────────────────────────────────┐
│  Quality-Based Encoding Pipeline        │
│                                         │
│  ┌────────────┐  ┌──────────────────┐   │
│  │    CLI     │  │   Public API     │   │
│  └─────┬──────┘  └────────┬─────────┘   │
│        │                  │             │
│        └──────────┬───────┘             │
│                   │                     │
│                   ▼                     │
│         ┌──────────────────┐            │
│         │  Orchestrator    │            │
│         └────────┬─────────┘            │
│                  │                      │
│         ┌────────┴────────┐             │
│         │                 │             │
│    ┌────▼────┐      ┌────▼────┐         │
│    │ Phases  │      │Progress │         │
│    │         │      │Tracker  │         │
│    └─────────┘      └─────────┘         │
└─────────────────────────────────────────┘
       │                    │
       │                    │
       ▼                    ▼
┌──────────────┐    ┌──────────────┐
│   FFmpeg     │    │  MKVToolnix  │
│   FFprobe    │    │              │
└──────────────┘    └──────────────┘
```

### Component Architecture

```log
┌─────────────────────────────────────────────────────────┐
│                    Pipeline Orchestrator                │
│  - Coordinates phase execution                          │
│  - Manages state transitions                            │
│  - Handles artifact-based resumption                    │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
        ▼            ▼            ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Extraction  │ │   Chunking   │ │Optimization  │
│    Phase     │ │    Phase     │ │    Phase     │
└──────────────┘ └──────────────┘ └──────────────┘
        │            │            │
        ▼            ▼            ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│   Encoding   │ │    Audio     │ │    Merge     │
│    Phase     │ │    Phase     │ │    Phase     │
└──────────────┘ └──────────────┘ └──────────────┘
        │            │            │
        └────────────┼────────────┘
                     │
                     ▼
        ┌────────────────────────┐
        │   Progress Tracker     │
        │  - State persistence   │
        │  - Chunk tracking      │
        │  - CRF history         │
        └────────────────────────┘
```

### Data Flow

```log
Source Video
    │
    ▼
[Extraction] ──────► Extracted Streams + Crop Parameters
    │
    ▼
[Chunking] ─────────► Scene-Based Chunks (Cropped)
    │
    ▼
[Optimization] ─────► Optimal Strategy (Optional)
    │
    ▼
[Encoding] ─────────► Encoded Chunks + Quality Metrics
    │                  (Multiple Strategies)
    ▼
[Audio] ────────────► Processed Audio (Day/Night)
    │
    ▼
[Merge] ────────────► Final MKV Files
```

## Component Details

### CLI Interface (`pyqenc.cli`)

**Responsibility**: Provide command-line interface with subcommands

**Key Features**:

- Main `auto` command for full pipeline
- Phase-specific subcommands for manual execution
- Dry-run mode by default (requires `-y` to execute)
- Process priority management (below normal)

**Arguments**:

- `--work-dir`: Working directory for intermediate files
- `--quality-target`: Quality targets (e.g., "vmaf-min:95")
- `--strategies`: Encoding strategies (e.g., "slow+h265-aq")
- `--max-parallel`: Concurrent encoding processes
- `-y/--execute`: Execute phases (default: dry-run)
- `--keep-all`: Skip cleanup prompt

### Pipeline Orchestrator (`pyqenc.orchestrator`)

**Responsibility**: Coordinate phase execution and manage state

**Key Features**:

- Executes all phases in order
- Each phase checks for existing artifacts
- Dry-run mode support
- Cleanup prompting after completion
- Disk space checking before execution

**Phases**:

1. Extraction
2. Chunking
3. Optimization (optional)
4. Encoding
5. Audio
6. Merge

### Progress Tracker (`pyqenc.progress`)

**Responsibility**: Persistent state management

**Key Features**:

- JSON-based state file
- Atomic writes using temporary files
- Batched updates to reduce I/O
- Phase status tracking
- Chunk attempt history
- CRF averaging for strategy

**State Structure**:

```json
{
  "version": "1.0",
  "source_video": "movie.mkv",
  "current_phase": "encoding",
  "phases": {
    "extraction": {"status": "completed", "timestamp": "..."},
    "chunking": {"status": "completed", "chunks_count": 150}
  },
  "chunks": {
    "chunk_001": {
      "strategies": {
        "slow+aq": {
          "status": "completed",
          "attempts": [...],
          "final_crf": 18.0
        }
      }
    }
  }
}
```

### Configuration Manager (`pyqenc.config`)

**Responsibility**: Load and validate encoding profiles

**Key Features**:

- YAML-based configuration
- Codec definitions (h264-8bit, h265-10bit)
- Encoding profiles with extra arguments
- Strategy parsing (preset+profile format)
- Profile validation

### Quality Evaluator (`pyqenc.quality`)

**Responsibility**: Evaluate encoded chunks against quality targets

**Key Features**:

- Integration with pymkvcompare for metrics
- Integration with metrics_visualization for plots
- Multi-metric support (VMAF, SSIM, PSNR)
- Target evaluation (min, median, max statistics)
- Metric normalization for CRF adjustment

### CRF Adjustment Algorithm

**Responsibility**: Intelligently adjust CRF to meet quality targets

**Key Features**:

- Bidirectional adjustment (increase/decrease CRF)
- Metric-aware step sizing
- History-based cycle prevention
- Binary search between known bounds
- 0.25 CRF granularity

**Algorithm**:

1. Encode chunk with initial CRF (from average or default)
2. Calculate quality metrics
3. Compare against all targets
4. If all targets met: success
5. If targets not met: adjust CRF based on deficit
6. Use history bounds to prevent cycles
7. Repeat until targets met or max attempts reached

## Key Flows

### Complete Pipeline Flow

```log
1. User runs: pyqenc auto source.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y

2. CLI parses arguments and creates PipelineConfig

3. Orchestrator.run():
   a. Check disk space
   b. Load existing state (if any)
   c. Execute phases in order:
      - Extraction: Extract streams, detect crop
      - Chunking: Split into scenes, apply crop
      - Encoding: Encode chunks with CRF adjustment
      - Audio: Process audio streams
      - Merge: Concatenate and mux
   d. Prompt for cleanup

4. Each phase:
   a. Check for existing artifacts
   b. Reuse if valid, perform work if needed
   c. Update progress tracker
   d. Return result

5. Cleanup (if user confirms):
   a. Remove chunks
   b. Remove encoded attempts
   c. Keep or remove metrics
   d. Keep final output and progress tracker
```

### Chunk Encoding with CRF Adjustment

```log
1. ChunkEncoder.encode_chunk():
   a. Get initial CRF from successful chunks average or default
   b. Check if chunk already encoded and meets current targets
   c. If yes: reuse existing encoding
   d. If no: start encoding loop

2. Encoding Loop:
   a. Encode chunk with current CRF
   b. Calculate quality metrics (SSIM, PSNR, VMAF)
   c. Evaluate against all quality targets
   d. If all targets met: success, save chunk
   e. If targets not met:
      - Calculate normalized deficit
      - Adjust CRF using history bounds
      - Check if CRF already attempted
      - If new CRF: repeat from step a
      - If no new CRF: fail (max attempts)

3. Update progress tracker with attempt info

4. Return encoded chunk path and metrics
```

### Artifact-Based Resumption

```log
1. Pipeline starts (after interruption or config change)

2. Orchestrator loads existing state from progress.json

3. For each phase:
   a. Phase scans for existing artifacts
   b. Validates artifacts against current configuration
   c. If valid: reuse (mark as reused)
   d. If invalid or missing: perform work

4. Example: Encoding phase
   a. Scan encoded/ directory for all strategies
   b. For each chunk+strategy:
      - Check if encoded file exists
      - Check if metrics exist
      - Parse metrics and evaluate against current targets
      - If targets met: reuse
      - If targets not met or missing: encode

5. This approach handles:
   - Interruptions: Resume where left off
   - New strategies: Only encode new strategy
   - Changed targets: Re-encode chunks not meeting new targets
   - Manual cleanup: Re-encode deleted chunks
```

## Design Decisions

### 1. Artifact-Based Resumption vs Explicit Resume

**Decision**: Use artifact-based detection instead of explicit resume logic

**Rationale**:

- Simpler implementation (no complex state machine)
- More flexible (handles configuration changes automatically)
- More robust (works even if state file corrupted)
- Transparent (user can inspect and manipulate artifacts)

**Trade-offs**:

- Slightly more I/O (scanning directories)
- Need to validate artifacts each time

### 2. JSON for Progress Tracking

**Decision**: Use JSON for state file

**Rationale**:

- Human-readable for debugging
- Standard library support (no dependencies)
- Easy to parse and validate
- Sufficient for state complexity

**Alternatives Considered**:

- SQLite: Overkill for single-process access
- Pickle: Not human-readable, version issues
- YAML: Slower parsing, unnecessary features

### 3. CRF-Based Encoding

**Decision**: Use CRF with iterative adjustment

**Rationale**:

- Quality-first approach
- Predictable results across chunks
- Bidirectional optimization
- Industry standard

**Alternatives Considered**:

- Target bitrate: Less predictable quality
- Two-pass: Slower, unnecessary for quality targets

### 4. Automatic Black Border Detection

**Decision**: Detect and remove black borders automatically

**Rationale**:

- Encoding efficiency (don't waste bits on black)
- File size reduction
- Quality metrics accuracy
- Storage savings (cropped chunks)

**Implementation**:

- Detect once during extraction
- Sample multiple frames for accuracy
- Apply crop during chunking
- Store chunks in cropped form

### 5. Batched Progress Updates

**Decision**: Batch progress tracker updates

**Rationale**:

- Reduce disk I/O during parallel encoding
- Improve performance (10x fewer writes)
- Still safe (force write on phase transitions)

**Configuration**:

- Default batch size: 10 updates
- Phase updates: Always immediate
- Chunk updates: Batched

### 6. Cleanup Prompt After Completion

**Decision**: Prompt user for cleanup instead of automatic

**Rationale**:

- User control over disk space
- Transparency (user sees what's kept/removed)
- Safety (no accidental deletion)
- Flexibility (keep metrics for analysis)

**Options**:

- Keep metrics: Remove chunks and encoded files, keep metrics
- Remove all: Remove all intermediate files
- No cleanup: Keep everything
- `--keep-all` flag: Skip prompt entirely

## Performance Considerations

### Parallel Encoding

- Default: 2 concurrent chunks
- Recommended: Physical CPU cores / 2
- Prioritize completing started chunks
- Share successful CRF values across chunks

### Disk I/O Optimization

- Batched progress tracker updates (10x reduction)
- Atomic writes using temporary files
- SSD recommended for working directory

### Memory Usage

- Chunks processed independently (low memory)
- Metrics stored on disk (not in memory)
- State file kept small (< 1 MB typically)

### CPU Usage

- Main process runs at below-normal priority
- All subprocesses inherit lowered priority
- Prevents system interference

### Disk Space

- Estimation: $Size_{source} * (2.2 + 2.3 * Num_{strategies})$
- Check before starting (with 10% margin)
- Warn if space is tight

## Future Extensibility

### Adding New Codecs

- Add to `default_config.yaml`
- Define codec parameters
- Create profiles
- No code changes needed

### Adding New Metrics

- Update `quality.py`
- Add metric calculation
- Update normalization logic
- Add tests

### Adding New Phases

- Create phase module
- Add to orchestrator
- Add CLI subcommand
- Add API function
- Write tests

### Distributed Processing

- Chunk-based processing is naturally parallelizable
- Progress tracker can be extended to distributed store
- API-based design allows remote workers

### Web Interface

- CLI and API separation enables web UI
- REST API wrapper around public API
- WebSocket for real-time progress
- Visual quality comparison viewer

## References

- Design Document: `.kiro/specs/quality-based-encoding-pipeline/design.md`
- Requirements: `.kiro/specs/quality-based-encoding-pipeline/requirements.md`
- Tasks: `.kiro/specs/quality-based-encoding-pipeline/tasks.md`
