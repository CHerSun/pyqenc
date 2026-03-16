# pyqenc Test Suite

This directory contains the test suite for the pyqenc quality-based encoding pipeline.

## Test Organization

```log
tests/
├── fixtures/          # Test fixtures and mock data
│   ├── config_fixtures.py      # Configuration file fixtures
│   ├── metric_fixtures.py      # Mock metric results
│   ├── state_fixtures.py       # Progress state fixtures
│   └── video_fixtures.py       # Video file fixtures
├── unit/              # Unit tests for core components
│   ├── test_models.py          # Data model tests
│   ├── test_config.py          # Configuration management tests
│   ├── test_progress.py        # Progress tracking tests
│   └── test_quality.py         # Quality evaluation tests
├── integration/       # Integration tests for phase interactions
│   ├── test_extraction_chunking.py  # Extraction → chunking flow
│   ├── test_encoding_quality.py     # Encoding → quality evaluation
│   └── test_resumption.py           # Artifact-based resumption
└── e2e/               # End-to-end tests
    └── test_complete_pipeline.py    # Complete pipeline execution
```

## Running Tests

### Install Test Dependencies

```sh
uv sync --group test
```

### Run All Tests

```sh
pytest
```

### Run Specific Test Categories

```sh
# Unit tests only
pytest tests/unit/

# Integration tests only
pytest tests/integration/

# End-to-end tests only
pytest tests/e2e/

# Exclude slow tests
pytest -m "not slow"
```

### Run Specific Test Files

```sh
# Test configuration management
pytest tests/unit/test_config.py

# Test quality evaluation
pytest tests/unit/test_quality.py

# Test complete pipeline
pytest tests/e2e/test_complete_pipeline.py
```

### Run with Coverage

```sh
pytest --cov=pyqenc --cov-report=html
```

## Test Requirements

### Sample Videos

Some tests require sample video files to be present in the `samples/` directory:

- `samples/sample-lion-fullhd.mkv`
- `samples/sample-sunrise-4k.mkv`

Tests that require sample videos are automatically skipped if the files are not available.

### External Dependencies

Integration and end-to-end tests require external tools:

- `ffmpeg` - Video processing
- `mkvtoolnix` - MKV file manipulation

Ensure these are installed and available in your PATH.

## Test Markers

Tests are marked with the following markers:

- `@pytest.mark.slow` - Tests that take significant time to run
- `@pytest.mark.skipif(...)` - Tests that are conditionally skipped (e.g., missing sample videos)

## Writing New Tests

### Unit Tests

Unit tests should:

- Test individual functions/classes in isolation
- Use mock data from fixtures
- Be fast and deterministic
- Not require external dependencies

Example:

```python
def test_quality_target_parsing():
    target = QualityTarget.parse("vmaf-min:95")
    assert target.metric == "vmaf"
    assert target.value == 95.0
```

### Integration Tests

Integration tests should:

- Test interactions between components
- Use real implementations (not mocks)
- May use sample videos if available
- Test artifact-based resumption logic

Example:

```python
@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
def test_extraction_to_chunking(tmp_path):
    # Extract streams
    extract_result = extract_streams(...)

    # Chunk video
    chunk_result = chunk_video(...)

    # Verify integration
    assert chunk_result.success
```

### End-to-End Tests

End-to-end tests should:

- Test complete pipeline workflows
- Use dry-run mode when possible to save time
- Test configuration changes and resumption
- Be marked as `@pytest.mark.slow`

Example:

```python
@pytest.mark.slow
def test_complete_pipeline_dry_run(tmp_path):
    config = PipelineConfig(...)
    orchestrator = PipelineOrchestrator(...)
    result = orchestrator.run(dry_run=True)
    assert result is not None
```

## Continuous Integration

Tests are designed to run in CI environments:

- Unit tests run on every commit
- Integration tests run on pull requests
- End-to-end tests run nightly or on release branches
- Tests without sample videos are automatically skipped

## Troubleshooting

### Tests Fail with "Sample video not available"

Download or create sample video files in the `samples/` directory, or skip these tests:

```sh
pytest -m "not slow"
```

### Tests Fail with "ffmpeg not found"

Install ffmpeg:

```sh
# Windows (using Scoop)
scoop install ffmpeg

# macOS
brew install ffmpeg

# Linux
sudo apt-get install ffmpeg
```

### Tests Fail with "mkvmerge not found"

Install mkvtoolnix:

```sh
# Windows (using Scoop)
scoop install mkvtoolnix

# macOS
brew install mkvtoolnix

# Linux
sudo apt-get install mkvtoolnix
```
