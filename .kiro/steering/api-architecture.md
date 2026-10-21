# API & Architecture

Public surface, CLI-first design, async policy, and the config/plan doctrine.

- Non-public functions must be prefixed with `_`, or `__` for internal implementation details.
- Public API and functions must have explanatory docstrings with required details. Only truly necessary functions should be public — clean, intent-driven API surface.
- CLI is the mandatory starting point, but the final target is a client-server solution. The API MUST NOT be tailored only towards CLI.
- CLI script entry point must be defined in `pyproject.toml` so the end-user can call the program directly without `python ...`.
- Use `async` where it keeps the UI responsive or avoids blocking on I/O. There is NO goal to be 100% async.
- The default config object is the single source of truth for all config defaults. Everywhere else (function signatures, constructors, internal calls) values must be required explicitly — no default parameter values that could silently diverge from the canonical defaults.
- **Config is declaration; resolved values are per-run objects.** Raw strings (`encoding.strategies`, `encoding.targets`) exist only as the YAML parse form and as sidecar serialization for recovering to objects — never as read state. The CLI derives the `EncodingPlan` exactly once via `AppConfig.build_encoding_plan(...)` (CLI overrides are arguments to that call, not field writes) and threads it through `JobPhaseResult` like every other volatile per-run parameter. Phases consume the plan; nobody re-resolves downstream, and the config object is never written after load. One run = one stable config + one plan.
- Derived result indices group by the axis consumers actually iterate (`dict[strategy_name] -> list[EncodedChunk]`, not chunk-major nesting): the payload owns its identity, and grouping is a convenience — ordering is always established explicitly at the consuming site, never inherited from insertion order.
- Chunk-chronological ordering keys on `start_timestamp` (the quantity itself) — never on formatted names or file names, whose lexicographic order coincides with time order only by zero-padding convention.
