# Code Style

Rules for writing code: language, typing, docstrings, naming, constants, logging.

## Python Language & Style

- Targeting Python>=3.14 syntax. Interpreter pinned via `.python-version` (3.14.x line, not 3.15).
- Line endings are LF everywhere, enforced by `.gitattributes` (`eol=lf`) — it overrides core.autocrlf for this repo. Write LF; never convert.
- 4 spaces are used as indentation.
- Annotations rely on PEP 649 lazy evaluation (3.14 default): NEVER write `from __future__ import annotations` (a deprecated no-op on 3.14) and NEVER quote annotations (`"AppConfig"`) — bare names work for forward references and `TYPE_CHECKING`-only imports.
- For volatile things (file system) - try (not check).
- All functions, classes and class members MUST BE type-hinted, `Any` is not allowed. `object` is discouraged.
- Avoid optional (nullable), where reasonable. Use explicit typing and type upgrades instead of nullability. Producer guarantees should be explicit, upgradable (see VideoStream and ProbedVideoStream, or Fingerprint and SizedFingerprint).
- Docstrings are GOOGLE-STYLE, universally (every function, class, and class member): one-line summary, extended prose paragraphs, then structured sections — `Args:` (or `Arguments:`), `Returns:`, `Raises:`, `Yields:`, `Attributes:` for classes — with `name: description` entries and indented continuations. This format is parsed by the LSP (Pylance/Pyright hover help and per-parameter info) — keep it exact so the tooling works. No reST fields (`:param x:`), no NumPy-style underlined headers. Docstrings state the CURRENT CONTRACT only — summary, role, field semantics, invariants and their non-obvious why; dated reasoning, tombstones of removed fields, and discovery history live in commits and specs, never in the docstring.
- Type-hint using newer syntax: `int|None` instead of `Optional[int]`, newer generic classes without imports from `typing` where possible.
- Use top-level imports. In-place (local) imports are only acceptable when top-level imports are not possible, e.g. to break circular dependencies. Circular dependencies is a strong code smell -- add to todo for review.
- Use vertical alignment between arguments/parameters where it improves readability.
- Follow DRY. If code is repeated 2-3+ times — make it reusable.
- `getattr`/`setattr` usage needs explicit human approval per case. Prohibited in general. Explicit typing is a requirement. The only directly acceptable dynamic access is a genuinely dynamic container (`dict`). Exception: probing an `argparse.Namespace` while assembling optional CLI overrides.
- Clean, self-explanatory code is preferable over patterns-for-patterns'-sake.
- Disowned functions are strongly discouraged. We use OOP approach. Mechanics should be owned by the related class, not written as standalone functions operating on external state. Example when disowned functions could be ok - to reach uniform logging between different phases. Disowned functions could be ok if used across modules (helpers / tools).

## Naming (two-name doctrine)

- Every named element owns exactly two method accessors: `display_name()` (the single verbatim generator) and `safe_name()` (the sanitized filesystem form derived from it). No third accessors, no exemptions.
- **Filesystem work ALWAYS uses `safe_name()`** — building paths, comparing against on-disk names, anything that lands on or is read from disk. Everything else (printing, yaml payloads, dict keys) uses `display_name()`.
- The owning class is the only place a name is composed — no manual joins of name parts outside the owner; consumers take the composed name from the accessor.
- **Project vocabulary lives in [`docs/glossary.md`](../docs/glossary.md) and is reused verbatim** in code identifiers, docs, and user-facing text. Introducing a new project term requires explicit approval — the same gate as new package/dependency additions (see `dependencies.md`). The gate covers vocabulary appearing in more than one place or on persisted/user-facing surfaces, not one-off local names.

## Constants & Magic Values

- NO MAGIC NUMBERS or MAGIC STRINGS allowed. Use named constants or enum values. `"psnr"` is NOT allowed; `MetricType.PSNR.value` is.
- Constants used multiple times must go into `constants.py`. `constants.py` must have no imports from the module (to avoid cycles).
- A constant (or function) with a single consuming class belongs to that class (attribute/method). If it is internal to the class, prefix with `_`. If instance-independent, mark `staticmethod`/`classmethod`.

## Logging

Detailed logging is a MUST, separated by levels:

- `debug` — hidden by default, implementation details, internal steps.
- `info` — end-user notifications, progress milestones, starts of long-running processes.
- `warning` — non-critical issues that allow continuation
- `error` — failures that prevent a specific operation but not the whole run
- `critical` — failures that prevent the program from doing any useful work

Use our `ProgressBar` for progress display to the end user for long tasks.
