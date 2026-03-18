# Project Dependencies Standards

- Use `uv` for project management, dependencies, venv, etc.
- Use `ruff` for linting.
- Use `.venv` for running python.
- Targeting Python>=3.13 (latest in 3.13).
- External programs dependencies: `ffmpeg` and `mkvtoolnix`. Addition of any other external programs as dependencies must be explicitly approved.
- For packages we target to be open-source, but without any strict obligations from our side. Before addition of a dependency it must be clearly articulated - why do we need it, what is its license, what limitations it imposes on us. If a package is approved - its transitive dependencies are considered approved too.
- Approved packages (besides Python base packages): `pandas`, `alive-progress`, `tqdm`, `psutil`, `streamlit`, `pydantic`, `fastapi`, `matplotlib`, `pyyaml`, `ffmpeg-normalize`, `scenedetect[opencv]`.
- Packages used for user interaction (progress, graphing, UI, etc) should also consider aesthetics as an important criterion, not just features. Example: `tqdm` is more functional, but `alive-progress` is neater — so `alive-progress` wins.
