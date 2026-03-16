# Contributing to pyqenc

<!-- markdownlint-disable MD026 -->

Thank you for your interest in contributing to pyqenc! This document provides guidelines and information for developers.

## Development Setup

### Prerequisites

1. **Python 3.13+**: This project targets Python 3.13 syntax and features.
2. **uv**: Package manager for dependencies and virtual environments.
3. **External tools**: FFmpeg and mkvtoolnix must be installed and available on PATH.

### Initial Setup

```sh
# Clone the repository
git clone https://github.com/CHerSun/pyqenc.git
cd pyqenc

# Create virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"
# OPTIONAL: activate venv
## On Windows:
# .venv\Scripts\activate
## On Linux:
# source .venv/bin/activate

# Install external dependencies
## On Windows:
scoop install ffmpeg mkvtoolnix
## On Ubuntu/Debian:
sudo apt-get install ffmpeg mkvtoolnix
## On macOS:
brew install ffmpeg mkvtoolnix
```

### Project Structure

```log
pyqenc/
├── pyqenc/                      # Main package
│   ├── __init__.py
│   ├── api.py                  # Public API
│   ├── cli.py                  # CLI interface
│   ├── config.py               # Configuration management
│   ├── constants.py            # Global constants
│   ├── models.py               # Data models
│   ├── orchestrator.py         # Pipeline orchestration
│   ├── progress.py             # Progress tracking
│   ├── quality.py              # Quality evaluation
│   ├── default_config.yaml     # Built-in default config
│   ├── phases/                 # Phase implementations
│   │   ├── audio.py                # Phase 5 - audio works
│   │   ├── chunking.py             # Phase 3 - scene detection & chunking
│   │   ├── encoding.py             # Phase 4 - encoding of chunks
│   │   ├── extraction.py           # Phase 1 - extraction
│   │   ├── merge.py                # Phase 6 - merging back videos
│   │   └── optimization.py         # Phase 2 - optimization [optional] - best strategy search
│   └── utils/                  # Utility modules
│       ├── cleanup.py              # Automatic cleanup routines
│       ├── disk_space.py           # Required disk space estimations
│       ├── ffmpeg.py
│       ├── ffmpeg_wrapper.py
│       ├── log_format.py           # Unified formatting of events
│       ├── logging.py
│       ├── performance.py
│       ├── progress_reporter.py    # Progress tracking & persistence
│       ├── validation.py
│       └── visualization.py        # Metrics graph plotting
├── tests/                      # Test suite
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docs/                       # Documentation
├── samples/                    # Sample files for testing
└── pyproject.toml             # Project configuration
```

## Coding Standards

### Python Style

- **Python Version**: Target Python 3.13+ syntax.
- **Type Hints**: All functions, classes, and class members MUST be type-hinted. Avoid `Any` too.
- **Modern Syntax**: Use `int | None` instead of `Optional[int]`.
- **Path Handling**: Use `pathlib.Path` for all file paths (no strings).
- **Constants**: NO MAGIC NUMBERS - use named constants or enums.
- **Async**: Use async where required for responsiveness, but we do not have to use it where it's useless.
- **Docstrings**: Public API and functions must have explanatory docstrings.

### Code Organization

- **KISS**: Keep it as simple as possible.
- **DRY**: If code is repeated 2+ times - make it reusable.
- **Rule of Three**: If 3+ similar entities exist, create a common interface.
- **Clean Code**: Self-explanatory, simple code is preferable over "patterns" over-engineering.
- **Vertical Alignment**: Use vertical alignment for arguments/parameters when sensible.

### Logging

- **Debug**: Hidden by default, detailed operation information.
- **Info**: End-user notifying, phase transitions, progress. Must be concise to avoid walls of text for end-user.
- **Warning**: Non-critical errors allowing continuation.
- **Critical**: Problems preventing actual work.

### Error Handling

- **EAFP (Easier to Ask for Forgiveness than Permission)**: Use try/except for volatile operations (not LBYL - Look Before You Leap), I/O operations in particular.
- **Specific Exceptions**: Catch specific exceptions. Avoid bare `except:` if possible.
- **Error Categories**:
  - `CriticalError`: Halts execution
  - `RecoverableError`: Allows continuation
  - `ValidationError`: Early detection of invalid input

## Development Workflow

### Making Changes

1. **Fork the repo**.
2. **Create a branch** in your forked repo for your commits and check it out.
   - **Write tests** for new functionality. Tests are not concrete: if code changes are required - update existing tests.
   - **Follow coding standards** outlined above.
   - **Run linting**: `ruff check .`
   - **Run tests**: `pytest`
   - **Update documentation** if needed
   - **Commit** changes to your branch.
3. **Create a pull request** to origin repository, add concise explanation of changes.

### Testing

See [tests/README.md](tests/README.md).

```sh
# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_config.py

# Run with coverage
pytest --cov=pyqenc --cov-report=html

# Run only unit tests
pytest tests/unit/

# Run only integration tests
pytest tests/integration/
```

### Linting

```sh
# Check code style
ruff check .

# Auto-fix issues
ruff check --fix .
```

## Architecture Overview

### Pipeline Phases

The pipeline follows a phased architecture where each phase:

- Has clear inputs and outputs.
- Can be executed independently via CLI subcommands.
- Maintains its state in the progress tracker.
- Produces artifacts in the working directory.

#### Phase Order:

1. **Extraction**: Extract video/audio streams, detect black borders.
2. **Chunking**: Split video into scene-based chunks.
3. **Optimization** (optional): Test strategies to find optimal one.
4. **Encoding**: Encode chunks with CRF adjustment to meet quality targets.
5. **Audio**: Process audio with day/night normalization.
6. **Merge**: Concatenate chunks and merge with audio.

### Key Design Principles

1. **Resumability**: All operations are tracked persistently.
2. **Modularity**: Each phase is independent with clear APIs.
3. **Reusability**: Leverage existing tested modules.
4. **CPU-First**: Default to CPU processing for consistency and quality.
5. **Quality-First**: Never compromise on quality targets.
6. **Transparency**: Preserve artifacts until user confirmation.
7. **Content-Aware**: Automatic black border detection, color spaces, etc.

### Artifact-Based Resumption

The pipeline doesn't have explicit "resume" logic. Instead:

- Each phase checks for existing artifacts.
- Valid artifacts are reused automatically.
- Missing/invalid artifacts trigger re-work.
- Configuration changes (new strategies, quality targets) detected automatically.

This approach supports:

- Recovering from interruptions with minimal overhead.
- Adding new strategies midway.
- Changing quality targets.
- Manual artifact cleanup for re-encoding (user intervention between reruns).

## Adding New Features

### Adding a New Codec

1. Add codec configuration to `pyqenc/default_config.yaml`:

   ```yaml
   codecs:
     av1-10bit:
       encoder: libsvtav1
       pixel_format: yuv420p10le
       default_crf: 30
       crf_range: [0, 63]
   ```

2. Add profiles for the codec:

   ```yaml
   profiles:
     av1-default:
       codec: av1-10bit
       description: "Default AV1 10-bit encoding"
       extra_args: []
   ```

3. Test with existing pipeline - no code changes should be needed for ffmpeg supported codecs!

### Adding a New Quality Metric

1. Update `pyqenc/quality.py` to support the new metric
2. Add metric calculation in quality evaluator
3. Update normalization logic in `normalize_metric_deficit()`
4. Add tests for the new metric

### Adding a New Phase

1. Create phase module in `pyqenc/phases/`
2. Implement phase function with standard signature
3. Add phase to `Phase` enum in `orchestrator.py`
4. Add phase execution method in `PipelineOrchestrator`
5. Add CLI subcommand in `cli.py`
6. Add API function in `api.py`
7. Write tests for the phase

## Testing Guidelines

### Unit Tests

- Test individual functions and classes in isolation
- Mock external dependencies (FFmpeg, file I/O)
- Focus on logic and edge cases
- Fast execution (< 1 second per test)
- Explicitly mark long tests with `slow`

### Integration Tests

- Test phase interactions
- Use real files but small test videos
- Verify artifact creation and reuse
- Test resumption scenarios

### End-to-End Tests

- Test complete pipeline with small video
- Verify final output quality and frame count
- Test dry-run mode
- Test configuration changes

### Test Fixtures

- Use sample videos in `samples/` directory (post links of videos for other devs reuse; videos are not to be included into the repo)
- Create reusable fixtures in `tests/fixtures/`
- Keep test data small (< 10 MB)

## Documentation

### Code Documentation

- **Public API**: Comprehensive docstrings with examples
- **Internal Functions**: Brief docstrings explaining purpose
- **Complex Logic**: Inline comments for clarity
- **Type Hints**: Always use type hints

### User Documentation

- `README.md`: User-facing documentation.
- `CONTRIBUTING.md`: Developer documentation (this file).
- `docs/**`: Architecture diagrams and design decisions.
- `.kiro/**`: Specs that were used to work with AWS Kiro IDE.

### Architecture Documentation

See `docs/architecture.md` for:

- System architecture diagrams
- Component interactions
- Design decisions and rationale
- Sequence diagrams for key flows

## Dependency Management

### Adding Dependencies

Before adding a new dependency:

1. **Justify the need**: Why is this dependency necessary?
2. **Check license**: Must be permissive (MIT, Apache, BSD) open-source license.
3. **Consider alternatives**: Are there lighter alternatives?
4. **Document**: Add to this file with justification

### Approved Dependencies

#### Core:

- `ffmpeg-normalize`: a wrapper for ffmpeg for audio normalization
- `alive-progress`: Progress bars (chosen for aesthetics and printing support over more functional `tqdm`).
- `matplotlib`: Plotting for quality metrics
- `pandas`: Data analysis for metrics
- `pydantic`: Configuration validation
- `psutil`: Process management and priority control
- `pyyaml`: YAML configuration parsing

#### Development:

- `pytest`: Testing framework
- `pytest-asyncio`: Async test support
- `ruff`: Linting and formatting
- `uv`: Project and package management
- `ty`: Type checking (`mypy` replacement)

#### External Programs:

- `ffmpeg`: Video encoding, scene detection, metrics
- `mkvtoolnix`: MKV stream extraction and merging

## Release Process

1. **Update version** in `pyqenc/__init__.py`
2. **Run full test suite**: `pytest`
3. **Build package**: `uv build`
4. **Test installation**: `uv pip install dist/*.whl`
5. **Create GitHub release**

## Getting Help

- **Questions**: Open a discussion on the repository
- **Bugs**: Open an issue with reproduction steps
- **Features**: Open an issue with use case description
- **Security**: Open an issue. If you consider the problem severe - do not disclose details, post a summary and your contact details.

## Code of Conduct

- Be respectful
- Focus on constructive feedback
- Help others learn and grow
- Assume good intentions

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see [LICENSE](LICENSE) file).
