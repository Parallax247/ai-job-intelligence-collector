# Architecture notes

## Main components

1. **Streamlit control panel** — builds validated run configurations, manages reusable keywords, displays browser health, and controls the scanner process.
2. **Scan process controller** — guarantees a single active run, streams logs, handles stop/continue actions, and preserves partial results.
3. **Browser service** — connects to a dedicated Chrome instance through CDP and binds the correct platform tab without owning the user session.
4. **Platform adapters** — isolate selectors and page-specific behavior behind a shared scraper interface.
5. **Storage pipeline** — writes JSONL incrementally, deduplicates records, finalizes run directories, and rewrites artifact paths.
6. **Report exporter** — produces a workbook for review, filtering, interview feedback, and downstream analysis.

## Reliability choices

- Incremental JSONL writes reduce loss when a run stops unexpectedly.
- Browser disconnects pause the run instead of marking remaining jobs invalid.
- Platform selectors are isolated so one website change does not affect other adapters.
- The UI process controller never terminates the dedicated Chrome session unless explicitly requested.
- Completed runs retain user-facing artifacts at the top level and move technical diagnostics into `internal/`.
