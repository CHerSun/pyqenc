# Project Dependencies Standards

- Use `uv` for project management, dependencies, venv, etc.
- Use `ruff` for linting.
- Use `.venv` for running python.
- Targeting Python>=3.13 (latest in 3.13).
- External programs dependencies: `ffmpeg` package and `mkvtoolnix` package. Addition of any other external programs as dependencies must be explicitly approved.
- For packages we target to be open-source, but without any strict obligations from our side. Before addition of dependency it must be clearly articulated - why do we need it, what is its license, what limitation it imposes on us. If package is approved - its dependencies are considered to be approved too. Approved packages (besides Python base packages): pandas, alive-progress, tqdm, psutil, streamlit, pydantic, fastapi, matplotlib. Packages used for user interaction (progress, graphing, UI, etc) should also consider aesthetics as important criteria (not just feature requirements; like: tqdm is more functional, but alive-progress is neater, so alive-progress > tqdm).
