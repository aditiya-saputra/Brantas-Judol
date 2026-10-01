# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-02

### Added

- **Coverage metrics.** `print_coverage()` reports entries actually read versus
  entries skipped by re-anchor, plus backlog depth. Previously no coverage number
  existed anywhere, so a collapse in cakupan would never have been noticed.
- **`LogResult`** named tuple replacing the positional tuple returned by
  `grab_log`, carrying `skipped` and `backlog` alongside the cursor positions.
- **`tests/test_orchestrator.py`** — 25 tests covering `fetch_range` and
  `grab_log` via `httpx.MockTransport` (no network). These two functions had zero
  test coverage despite holding the two-phase cursor and fair-share logic.
  Invariants now pinned: budget is a hard cap, cursors advance only on real
  progress, a failed `get-sth` leaves `updated_at` untouched, re-anchor never
  leaves a hole, and re-anchor skips are reported.
- **Parse failure reporting.** `except Exception` incremented a counter and
  discarded the reason, so programming bugs were indistinguishable from malformed
  certificates. The first five failures per run now print the exception type and
  message to stderr.
- `README.md`, `CHANGELOG.md`, `FLOWCHART.md`, `LICENSE` (MIT).

### Fixed

- **`fetch_range` could exceed its budget.** The loop checked `used < budget`
  before fetching but never capped the batch to the remaining allowance, so each
  call overshot by up to `batch - 1` entries. Across 27 logs that was roughly 7%
  over `--max-entries`, meaning the flag was not a hard cap. The batch end is now
  clamped to `budget - used`.
- **Misleading `backlog=` label.** The per-log line printed `next_index` under the
  name `backlog`, which read as "1,000 remaining" when the real backlog was in the
  billions. Renamed to `next=`; backlog depth is now reported once in the summary.

### Changed

- `DEFAULT_MAX_ENTRIES` raised from `100_000` to `200_000`, and the CI workflow
  with it. A benchmark of 4,000 entries against `argon2026h2` measured ~174
  entries/s with `sleep 0.25`, so the 1200s window fits ~209,000 entries. The old
  value ended the run at roughly 575s, leaving over half the window unused.
  `--max-seconds 1200` remains the hard guard; if throughput drops the deadline
  cuts cleanly because each log's cursor is already committed.
- Module docstring now states honestly that the backlog will not catch up, and
  that `tree_size - tail_index` must never be used as a coverage metric.

## [0.1.0] - 2026-09-26

### Added

- Initial CT grabber: registry log list (Google/Apple) → `get-sth` →
  `get-entries` → RFC 6962 TLS wire parsing → X.509 SAN/CN extraction →
  normalization → keyword filter → `domain.txt`.
- Hybrid cursor in SQLite (`ct.db`): `tail_index` for the newest entries,
  `next_index` for the backlog, `updated_at` for LRU rotation across logs.
- Two-phase per-log strategy — tail first, backlog with whatever budget remains.
- Dynamic fair-share across logs (`budget // remaining_logs`) with a
  `MIN_SLICE_SECONDS` guard so a starved log does not waste even its `get-sth`.
- Retry with exponential backoff honouring `Retry-After`; retryable statuses
  limited to 429/500/502/503/504.
- Schema migration that adds missing columns to a pre-existing `cursors` table.
- Boundary-aware `rtp` matching: matched only at a word boundary, because as a
  bare substring it produced hundreds of false positives from hostnames such as
  `digicertp` and `mazortp`.
- Fixture-based tests for the leaf parser and X.509 extraction, including a check
  that `parse_extra` returns a full DER `pre_certificate` rather than a bare
  `TBSCertificate`.
- GitHub Actions workflow running every 6 hours: test → grab → sort and dedupe →
  commit `domain.txt` and `ct.db` together.

[0.2.0]: https://github.com/aditiya-saputra/brantas-judol/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aditiya-saputra/brantas-judol/releases/tag/v0.1.0
