# Pipeline Phase Contract

These rules govern how pipeline phases interact with each other and manage their own state.

- **Sidecar ownership.** Each phase owns its sidecar YAML file. It may persist results needed between reruns (detected crop, frame counts, etc.) and incoming settings whose change invalidates the phase's work (e.g. crop parameters for encoding — changing them requires a full re-encode). A phase is prohibited from reading or writing another phase's sidecar directly; it must go through the phase object's API.

- **Artifact ownership.** Each phase owns its inputs, intermediate results, and output artifacts. Other phases must obtain resulting artifacts only by calling the phase object — never by scanning the filesystem directly. For example: do not scan for successful encoding attempts; get them from the encoding phase along with their artifact status.

- **Artifact states.** A phase produces params and artifacts. Each artifact carries an explicit state: wanted & fully produced, wanted but failed/partial, or not wanted. The phase's public result must contain all wanted artifacts with their status — callers pick what they need directly and immediately see which artifacts succeeded or failed, without needing to reason about what was unwanted. Unwanted artifacts are internal phase mechanics and must never leak to callers.

- **Recovery protocol.** Each phase owns its own recovery from incoming parameters and on-disk data. Recovery must be exhaustive: recover every artifact the phase can account for — wanted and complete, wanted but partial, and unwanted but present on disk — each with its correct status. This gives the phase a complete picture of current state before deciding what work remains. For each artifact: attempt full recovery first; if that fails, check intermediate results to salvage partial work; only then plan the remaining work. Phase must use deterministic, reproducible naming so recovery is reliable.

- **Atomicity.** All results — intermediate, final, or otherwise — must follow the `.tmp`-then-rename protocol. There must never be a partial result without a `.tmp` extension on disk. This guarantees clean recovery and allows full trust in any non-`.tmp` artifact found on disk.

- **Invalidation.** Each phase is responsible for invalidating its own artifacts and intermediate results when incoming state changes between reruns. Parameters required for change detection must be persisted in the phase's sidecar.

- **Cleanup.** Each phase must respect the user-configured cleanup level and clean up its intermediate results accordingly.

- **Forced invalidation.** Each phase must respect forced invalidation requests from the user.
