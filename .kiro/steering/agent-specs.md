# Requirements to agent when making specs

- BEFORE changing any code - discuss the strategy and get user confirmation.
- Add HTML comment to specs to disable MD024 markdownlint rule `<!-- markdownlint-disable MD024 -->` after the first header to prevent excessive warnings about duplicate headers.
- Add dates in ISO format to specs to have a clear indication of specs order: "- Created: " - when we started working on the spec, "- Completed: " date - update once all tasks were done (add as a last task). In form of a list under the top header, after MD024 comment.
- Add task to review current spec against other specs and add a summary to the top of both specs with things that were superceeded / changed between specs (i.e. difference in time). Timeline should be recovered either by created/completed dates in specs or by file timestamps from filesystem.
- Any diagrams must be made using Mermaid code syntax. If in markdown - embedded via codeblock with `mermaid` hinting.

## Folder naming and lifecycle

- New spec folders MUST be named `YYYY-MM-DD <kebab-topic>`. The date is the spec's creation date: the in-file `- Created:` value when it is earlier than or equal to the folder's first git commit, otherwise the first git commit date (fix the header to match). Folders created before this rule keep their original name after the date prefix.
- Specs have exactly two states: **active** (default — lives directly under `.kiro/specs/`, no marker) and **no longer actual** (moved to `.kiro/specs/_archive/` via `git mv`). When a later spec consumes most or all of an older spec's content, record the successor in the older spec's Cross-Spec Notes, then move the folder to `_archive/`.
- There is no "deprecated" state: a spec we decide is bad is deleted outright — we either implement a spec or don't store it.
- In-progress state needs no marker — a blank `- Completed:` and unchecked tasks already show it.
- Naming vocabulary: `app-metrics-*` is reserved for app metrics (wall-clock per phase, disk, recovery observability of the tool itself); unqualified `metrics` means quality measurement (VIF/PSNR/SSIM/VMAF and their statistics). Never mix the two domains in one supersession chain.
- Name specs after the capability they define, not the effort (avoid `refactor`/`revamp`/`overhaul`/`maturity` suffixes in new names). Existing effort-flavored names are left as-is — history stays recognizable.
