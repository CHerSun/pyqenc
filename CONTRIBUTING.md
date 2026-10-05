# Contributing to pyqenc

<!-- markdownlint-disable MD026 -->

Thank you for your interest in contributing to pyqenc! This document provides guidelines and information for developers.

## Development Setup

### Prerequisites

1. **Python 3.14+**: This project targets Python 3.14 syntax and features. The exact minor line is pinned in `.python-version` (uv reads it automatically).
2. **uv**: Package manager for dependencies and virtual environments.
3. **External tools**: FFmpeg and mkvtoolnix must be installed and available on PATH.

### Initial Setup

```sh
# Clone the repository
git clone https://github.com/CHerSun/pyqenc.git
cd pyqenc

# Install everything (interpreter per .python-version, .venv, all dependency groups)
uv sync --all-groups
# No manual venv activation needed — prefix everything with `uv run`

# Enable the pre-commit gate hook (ruff + ty block bad commits)
git config core.hooksPath .githooks

# Install external dependencies
## On Windows:
scoop install ffmpeg mkvtoolnix
## On Ubuntu/Debian:
sudo apt-get install ffmpeg mkvtoolnix
## On macOS:
brew install ffmpeg mkvtoolnix
```

Line endings are **LF everywhere**, enforced by `.gitattributes` (`eol=lf`) — it overrides any platform/editor defaults, so you never need to think about CRLF. New files default to LF in VS Code via the project settings.

### Project Structure

```log
pyqenc/
├── pyqenc/                      # Main package
│   ├── api.py                  # Public API
│   ├── cli.py                  # CLI interface
│   ├── app_config.py           # Configuration models + default config loading
│   ├── default_config.yaml     # Built-in default config
│   ├── constants.py            # Global constants
│   ├── models.py               # Data models
│   ├── phase.py                # Phase base class, Artifact, registry
│   ├── runner.py               # Phase-sequence runner
│   ├── state.py                # Pydantic sidecar models (phase persistence)
│   ├── stream_model.py         # Typed stream/file composition family
│   ├── quality.py              # Quality search + metrics plumbing
│   ├── metrics.py              # Metrics collector (timing instrumentation)
│   ├── audio/                  # Audio filter chains, layouts, selection
│   ├── phases/                 # Phase implementations (job, extraction, probe,
│   │                           # audio, chunking, optimization, encoding, merge,
│   │                           # plus standalone measure)
│   └── utils/                  # ffmpeg_runner, alive (progress bars), long_path,
│                               # visualization, naming, fs, yaml_utils, ...
├── tests/                      # Test suite
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
├── docs/                       # Documentation (architecture.md is canonical)
├── .kiro/                      # steering/ (living project rules) + specs/ (design history)
├── samples/                    # Sample video links (SAMPLES.md) — not committed
└── pyproject.toml             # Project configuration
```

## Coding Standards

The complete, living standard is [`.kiro/steering/coding-standards.md`](.kiro/steering/coding-standards.md); the essentials:

### Python Style

- **Python Version**: Target Python 3.14+ syntax.
- **Type Hints**: All functions, classes, and class members MUST be type-hinted. The `ty` type checker is a standing gate — code that does not type-check does not commit.
- **Annotations**: Rely on PEP 649 lazy evaluation (3.14 default). NEVER write `from __future__ import annotations` (deprecated no-op) and NEVER quote annotations (`"AppConfig"`).
- **Modern Syntax**: Use `int | None` instead of `Optional[int]`.
- **Paths**: `pathlib.Path` for all file paths (no strings). `LongPath` is constructed at system boundaries (CLI args, sidecar loads) and travels through code as `Path` — signatures declare `Path`, composition via `/` preserves the runtime subtype.
- **Optionality**: Prefer non-Optional types when construction guarantees presence; empty containers are checked Python-style (`if not xs:`); programmatic contracts are enforced with `assert` at the consuming site, not by widening footprints to `| None`.
- **Constants**: NO MAGIC NUMBERS or MAGIC STRINGS — use named constants or enums.
- **Naming**: named elements own exactly `display_name()` and `safe_name()`; filesystem work always uses `safe_name()`.
- **Async**: Use async where required for responsiveness, but we do not have to use it where it's useless.
- **Docstrings**: Public API and functions must have explanatory docstrings.

### Code Organization

- **KISS**: Keep it as simple as possible.
- **DRY**: If code is repeated 2-3+ times - make it reusable.
- **Rule of Three**: If 3+ similar entities exist, create a common interface.
- **Clean Code**: Self-explanatory, simple code is preferable over "patterns" over-engineering.
- **Vertical Alignment**: Use vertical alignment for arguments/parameters when sensible.
- **Single source of truth**: Where possible - prefer single source of truth / ownership.
- **No backwards compatibility**: Pre-alpha project, no public API stability yet. Clean code over legacy shims and migrations.

### Logging

- **Debug**: Hidden by default, detailed operation information.
- **Info**: End-user notifying, phase transitions, progress. Must be concise to avoid walls of text for the end-user.
- **Warning**: Non-critical errors allowing continuation.
- **Error**: Failures preventing a specific operation but not the whole run.
- **Critical**: Problems preventing actual work.

### Error Handling

- **EAFP (Easier to Ask for Forgiveness than Permission)**: Use try/except for volatile operations (not LBYL - Look Before You Leap), I/O operations in particular.
- **Specific Exceptions**: Catch specific exceptions. Avoid bare `except:` if possible.
- **Use .tmp-then-rename protocol**: to ensure any produced artifacts get final name only after they are fully complete.

## Development Workflow

### The Three Gates

A change is green only when ALL THREE pass:

```sh
uvx ruff check .              # lint (check only — ruff format is NOT used)
uvx ty check                  # type check
uv run python -m pytest       # test suite
```

The pre-commit hook runs the two static gates automatically on every commit (after `git config core.hooksPath .githooks`). pytest is deliberately not in the hook — minutes-long, the wrong timescale for a commit.

### Making Changes

1. **Fork the repo**.
2. **Create a branch** in your forked repo for your commits and check it out.
   - **Write tests** for new functionality. Tests are not concrete: if code changes are required - update existing tests.
   - **Follow coding standards** outlined above.
   - **Run the three gates** (above).
   - **Update documentation** if needed.
   - **Commit** changes to your branch.
3. **Create a pull request** to origin repository, add explanation of changes.

### Commit Messages

Conventional commits with a semver bump in the same commit (e.g. `feat(encoding): persist frame counts (0.16.1)`), version lives in `pyqenc/__init__.py`. The body lists actual changes only — no test results or verification notes.

### Testing

See [tests/README.md](tests/README.md).

```sh
# Run all tests
uv run python -m pytest

# Run specific test file
uv run python -m pytest tests/unit/test_models.py

# Run only unit / integration tests
uv run python -m pytest tests/unit/
uv run python -m pytest tests/integration/
```

For any e2e run that includes encoding, always pass `--strategies "h265*+ultrafast"` — slow presets turn a CRF search on a minutes-long clip into hours.

## Architecture Overview

Canonical documentation lives in [docs/architecture.md](docs/architecture.md); the summary:

### Pipeline Phases

The pipeline follows a phased architecture where each phase:

- Extends the `Phase` base class (`pyqenc/phase.py`), declares dependencies via `DEPENDS_ON`, and is wired by the phase registry.
- Can be executed independently via CLI subcommands.
- Recovers its state from its own sidecar and on-disk artifacts (recovery is presence-based: a present non-`.tmp` file is complete).
- Produces artifacts in the working directory.

#### Phase Order:

0. **Job**: starting point for all runs — source identity, work dir, config hand-off.
1. **Extraction**: enumerate source streams, extract container artifacts (subtitles, chapters, attachments, timestamps).
2. **Probe**: resolve the slow video facet (frame count, crop detection).
3. **Audio**: process audio through configurable filter chains (normalization, downmixing).
4. **Chunking**: split the video stream into scene-based chunks.
5. **Optimization** (optional): test strategies to find the optimal one.
6. **Encoding**: encode chunks with quality-targeted CRF search per chunk.
7. **Merge**: concatenate winning encoded chunks; final container assembly.
   - Merging video and audio streams is left to the end-user, as we don't know what exactly they want.
- **Measure**: a standalone subcommand for quality measurement outside the pipeline.

### Key Design Principles

1. **Resumability**: All operations can be recovered from persisted state with focus on recovery from artifacts.
2. **Modularity**: Each phase is independent with clear APIs.
3. **Reusability**: Leverage existing tested modules.
4. **CPU-First**: Default to CPU processing for compatibility, consistency and quality. GPU could be used, but never must be the only way to do things.
5. **Quality-First**: Never compromise on quality targets.
6. **Transparency**: Preserve all reusable artifacts, unless user tells differently. They can be used for resuming and manual inspection.
7. **Content-Aware**: Automatic black border detection, color spaces, best strategy selection, etc.

### Artifact-Based Resumption

The pipeline doesn't have explicit "resume" logic. Instead:

- Each phase
  - Requests previous phase for its results - to be used as inputs
  - Checks for existing artifacts and synchronizes state
- Valid artifacts are reused automatically.
- Missing/invalid artifacts trigger re-work.
- Configuration changes (new strategies, quality targets) detected automatically with proper state invalidation.

This approach supports:

- Recovering from interruptions with minimal overhead.
- Changing parameters midway: strategies, target qualities, metrics subsampling, etc.
- Manual interventions between reruns, like artifact/state cleanup (the pipeline isn't bug free, this allows better control and testing).

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

2. Add at least one profile for the codec:

   ```yaml
   profiles:
     av1-default:
       codec: av1-10bit
       description: "Default AV1 10-bit encoding"
       extra_args: []
   ```

3. Test with existing pipeline - no code changes should be needed for ffmpeg supported codecs!

### Adding a New Quality Metric

1. Update `pyqenc/quality.py` to support the new metric via MetricInfo. Mind the normalization
2. Add metric calculation in the quality evaluator (`pyqenc/utils/visualization.py`). This is currently head-ache, especially for the graph.
3. Add tests for the new metric

### Adding a New Phase

Shouldn't be required, but just in case:

1. Create the phase module in `pyqenc/phases/`, subclassing `Phase[YourPhaseResult]`
2. Implement the contract hooks: `_recover()`, `_execute()`, `_make_result()` (see `pyqenc/phase.py` docstrings; optional hooks as needed)
3. Declare its `DEPENDS_ON` — the registry is derived from the declarations (`dependency_closure` of the run's terminals), so the declaration alone decides membership and construction order
4. Add CLI subcommand in `cli.py` (a `_SubcommandSpec` entry in the declarative table, or a dedicated handler for tool-shaped commands)
5. Add API function in `api.py`
6. Write tests for the phase

## Testing Guidelines

### Unit Tests

- Test individual functions and classes in isolation
- Mock external dependencies (FFmpeg, file I/O)
- Focus on observable behavior, not internal state; each test guards a specific bug condition
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

- Sample video links live in `samples/SAMPLES.md` (videos are not to be included into the repo)
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
- `docs/**`: Architecture diagrams and design decisions. `docs/architecture.md` is the canonical architecture description.
- `.kiro/steering/**`: Living project rules (coding standards, commands, environment notes).
- `.kiro/specs/**`: Design specs and historical decision records.

### Architecture Documentation

See [docs/architecture.md](docs/architecture.md) for:

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

Routine `uv lock --upgrade` stays within current majors; taking a NEW major of any dependency is a deliberately reviewed pass (see TODO.md).

### Approved Dependencies

#### Core:

- `alive-progress`: Progress bars (chosen for aesthetics and printing support over more functional `tqdm`).
- `matplotlib`: Plotting for quality metrics
- `pandas`: Data analysis for metrics
- `pydantic`: Configuration validation and sidecar models
- `psutil`: Process management and priority control
- `pyyaml`: YAML configuration parsing
- `scenedetect-headless`: Scene detection for chunking

#### Development:

- `pytest`: Testing framework
- `pytest-asyncio`: Async test support
- `hypothesis`: Property-based tests
- `debugpy`: Debugger support
- `ruff`: Linting (check only — `ruff format` is not used; the house style has intentional vertical alignment)
- `uv`: Project and package management
- `ty`: Type checking (`mypy` replacement)

#### External Programs:

- `ffmpeg`: Video encoding, scene detection, metrics
- `mkvtoolnix`: MKV stream extraction and merging

## Release Process

1. **Update version** in `pyqenc/__init__.py`
2. **Run the three gates** (ruff, ty, pytest — all must pass)
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
