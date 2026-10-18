# Types, Optionality & Contracts

How to model presence, absence, and invariants in types.

Design by types and their flow.

## Optionality (None policy)

- Prefer non-Optional: if a value is always present by construction, the type must say so (`work_dir: Path`, never `Path | None`). An Optional footprint consumed unconditionally is a lie the type checker will flag.
- Avoid `None` as a sentinel where an empty container expresses it. Empty containers are checked Python-style (`if not mapping:`); direct `.get()`/`in` are fine. Use `None` only where "absent" is semantically distinct from "empty".
- Presence guaranteed by our own construction is enforced with `assert` at the consuming site — not by widening footprints to Optional (see Contracts below).

## Contracts vs External Validation

- `isinstance` / `is None` checks are for EXTERNAL optionality: CLI arguments not supplied, config-file fields absent, sidecar/media (ffprobe)-sourced data missing, or a genuine API Optional. These are normal conditionals.
- PROGRAMMATIC contracts — values our own construction guarantees (a dependency result after the dependency walk, a COMPLETE recovery row's winning file, a resolved config after `resolve()`), are enforced with `assert` (with a short why-message) or by tightening the footprint to the concrete type. Never with silent `if x is None` fallbacks, and not with exception raises for programmer errors.
- Mandatory contract methods are `@abstractmethod` — never a bare `raise NotImplementedError`.
- Model fields capturing external facts (ffprobe `start_time`, `pix_fmt`, …) are retained even when currently unread — data preservation is not dead code.
