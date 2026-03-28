# Project Dependencies Standards

- Use `uv` for project management, dependencies, venv management, project and tests running.
- Use `ruff` for linting.
- Targeting Python>=3.13 (latest in 3.13).
- External programs dependencies: `ffmpeg` and `mkvtoolnix`. Addition of any other external programs as dependencies must be explicitly approved.
- Approved packages (besides Python base packages): `pandas`, `alive-progress`, `tqdm`, `psutil`, `streamlit`, `pydantic`, `fastapi`, `matplotlib`, `pyyaml`, `ffmpeg-normalize`, `scenedetect[opencv]`. Addition of any other package dependencies must be explicitly approved.

