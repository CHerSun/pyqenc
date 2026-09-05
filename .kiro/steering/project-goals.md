# Project Goals & Encoding Philosophy

## Core Mission

pyqenc is a video/audio re-encoding tool. The primary goal is to **preserve source qualities and properties** while performing re-encoding work — not to alter them.

## Source Fidelity Principles

- **Frame rate**: Preserve the source frame rate exactly — whether constant (CFR) or variable (VFR). Never force a uniform frame rate unless explicitly requested.
- **Audio sampling rate**: Preserve the source audio sample rate. Do not resample unless the target codec requires it.
- **Color space, bit depth, HDR metadata**: Pass through as-is unless the encoding task explicitly requires a change.
- **Timestamps and timing**: Preserve source timestamps faithfully, especially for VFR content.
- **Container properties**: Preserve track order, language tags, titles, and other metadata when muxing.

## ffmpeg Usage Philosophy

Prefer achieving correct behavior **through proper ffmpeg invocation** rather than compensating in code:

- Rely on ffmpeg's native passthrough, copy, and remux capabilities where possible.
- Avoid filters or options that silently change properties as side effects (e.g., `fps` filter changing VFR to CFR, `aresample` changing sample rate unintentionally).
- Be explicit about what should change; let everything else remain untouched by default.
- When in doubt, audit the ffmpeg command for unintended side effects before adding it to the pipeline.
