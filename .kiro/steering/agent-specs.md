# Requirements to agent when making specs

- Add HTML comment to specs to disable MD024 markdownlint rule `<!-- markdownlint-disable MD024 -->` after the first header to prevent excessive warnings about duplicate headers.
- Add dates in ISO format to specs to have a clear indication of specs order: "- Created: " - when we started working on the spec, "- Completed: " date - update once all tasks were done (add as a last task). In form of a list under the top header, after MD024 comment.
- Add task to review current spec against other specs and add a summary to the top of both specs with things that were superceeded / changed between specs (i.e. difference in time). Timeline should be recovered either by created/completed dates in specs or by file timestamps from filesystem.
