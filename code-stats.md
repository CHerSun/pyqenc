# Project Code Statistics Snapshot

**Date:** 2026-04-12
**Project:** Quality-targeting video encoding pipeline (implemented from scratch, no external library dependencies for core functionality)
**Development context:** Solo developer, AI‑assisted, 100% time over 3 weeks (~21 days)

---

## Summary of Code Lines (Actual Code Only)

| Component          | Files | Code Lines | Comment Lines | Blank Lines | Total Lines | Complexity |
|--------------------|-------|------------|---------------|-------------|-------------|------------|
| **Source**         | 32    | 14,633¹    | 3,469         | 1,508       | 19,610      | 1,606      |
| **Tests**          | 35    | 7,062²     | 1,277         | 1,555       | 9,894       | 310        |
| **Specifications** | 75    | 14,455³    | 40            | 5,118       | 19,613      | 56         |
| **Total project**  | 142   | 36,150     | 4,786         | 8,181       | 49,117      | 1,972      |

¹ Includes 14,382 Python + 251 YAML
² Includes 6,912 Python + 150 Markdown
³ Includes 13,697 Markdown + 758 Python

---

## Key Metrics and Ratios

| Metric                                | Value                  |
| ------------------------------------- | ---------------------- |
| **Test‑to‑source code ratio**         | 0.48 (6,912 / 14,382)  |
| **Spec‑to‑source code ratio**         | 0.95 (13,697 / 14,382) |
| **Comment density (source)**          | 22.9% of code lines    |
| **Comment density (tests)**           | 18.5% of code lines    |
| **Complexity per code line (source)** | 0.11 (1,606 / 14,382)  |
| **Complexity per code line (tests)**  | 0.045 (310 / 6,912)    |
| **Average code lines per day**        | ~1,720 (36,150 / 21)   |

---

## What the Numbers Mean (Objective Explanation)

- **Code lines** – Lines that contain actual statements (no blanks, no comments). Measured by `scc` (a standard code counter).
- **Comment lines** – Lines consisting only of comments (including docstrings). Markdown has no comment syntax, hence 0.
- **Complexity** – Cyclomatic complexity estimate (number of decision points). Lower is simpler.
- **Test‑to‑source ratio** – Industry benchmarks vary; 0.48 is within typical range for solo projects.
- **High spec size** – The specification (Markdown + helper Python) is comparable in volume to the source code. This is factual from the count.

---

## Interpretation (Objective)

- The source code has moderate complexity (0.11 per line) and reasonable comment coverage (23%).
- Tests have lower complexity (0.045 per line) and slightly lower comment density (18.5%), which is typical for test suites.
- The specification folder contains 70 Markdown files (13,697 non‑blank, non‑comment lines) and 5 Python helper scripts (758 code lines).
- Total code output (source + tests + spec code) over 3 weeks is 36,150 lines. This includes both human‑written (specs) and AI‑generated (source/tests) content, all hand‑verified.
- 2:1:2 ratio for specs:tests:source. A spec-first approach.

*Generated from `scc` output. No subjective evaluation or speculation is included above.*
