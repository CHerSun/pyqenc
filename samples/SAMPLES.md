# Samples

- Video samples are not going to be included with the project. Use your own videos.
- Samples include metrics and their plot from 2 approaches to encoding for comparison.
  - `full_encoding` sub-folder - stores data from full-file encoding with constant quality.
  - `pyqenc` sub-folder - stores data from `pyqenc` encoding of the same video.

## Building the e2e sample (`sample-e2e.mkv`)

The e2e suites (`tests/e2e/`) skip without a sample at this exact path. It is
a small MKV exercising every stream kind, built from any real video you own:

```sh
uv run python -m tests.fixtures.build_e2e_sample <real_video.mkv>
```

The script cuts a real-content window (`--start`/`--duration` to steer),
generates the audio/subtitle/chapter tracks, reuses the source's cover
attachment (or a synthetic one), muxes, and verifies the result. The source
is never modified; intermediates land in `samples/_build/` (gitignored).
