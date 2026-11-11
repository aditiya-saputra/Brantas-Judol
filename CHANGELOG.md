# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **`classify.py` no longer calls urlscan.io.** Evidence now comes from
  `probe.py` via a new `--evidence` flag, so there is one evidence collector
  instead of two. Motivated by classify run #1 (2026-11-11, `42846859232`): the
  classifier searched urlscan itself, hit `HTTP 429` on the first three domains
  and stopped with zero domains scored. `probe.py` now records every result UUID
  under `uuids` so up to `--links` result pages can be cited per domain.
- **`.github/workflows/classify.yml`** runs `probe.py` first, then
  `classify.py --evidence`, and uploads `hasil/probe.json` alongside the three
  output files. A failing probe emits a warning and classification continues
  without evidence rather than aborting the run.

### Fixed

- Probe failures were attributed to Gemini and counted toward the
  three-consecutive-failures limit, so a urlscan rate limit could stop the run
  before the model was ever called. Evidence collection is now optional:
  classify scores with empty evidence and labels it `(tanpa bukti urlscan)`.

### Added

- **Experimental Gemini classifier (`classify.py`).** Scores candidates 0–100
  using up to 10 urlscan.io result URLs as evidence plus Google Search grounding,
  then buckets them into `blocklist.txt`, `whitelist.txt` or `review.txt`.
  Thresholds are **80/20**, not 50/50: a 49 and a 51 are indistinguishable, and
  the project rule is that an LLM must never on its own add to a public blocklist,
  so the grey zone routes to `review.txt`. Overridable via
  `--block-threshold` / `--allow-threshold`.
- **`.github/workflows/classify.yml`** — daily at `41 7 * * *` plus dispatch,
  `permissions: contents: read`, requires the `GEMINI_API_KEY` secret, uploads
  the three files as a 14-day artifact and commits nothing.
- `probe.search_scans()` — returns every urlscan.io hit for a domain, refactored
  out of `probe()` with shared request logic in `_fetch_results()`.
  `probe.probe()` behaviour is unchanged.
- `blocklist.txt`, `whitelist.txt`, `review.txt` and probe JSON outputs added to
  `.gitignore` — unreviewed LLM output must not be published from a repo.
- 28 tests in `tests/test_classify.py` on `httpx.MockTransport` — no network.

### Notes

- The classifier uses the **Interactions API** (`POST /v1beta/interactions`,
  `Api-Revision: 2026-05-20`, `tools: [{"type": "google_search"}]`,
  `response_format.mime_type: application/json`), not `generateContent`. Default
  model `gemini-3.8-flash`; both verified against the official docs on 2026-11-10.
- Only URL/title/domain metadata from urlscan.io is sent to the model — never DOM
  content — and the system instruction treats all evidence as data, not
  instructions.
- **Read-only urlscan.io evidence probe.** `probe.py` checks whether candidates
  from `domain.txt` have ever been scanned by urlscan.io and prints a summary with
  links to result pages. Search only — no classification, no blocklist export.
  Samples `--limit` candidates with a fixed `--seed` (default 25, seed 0), sends
  `API-Key` when `PROBE_API_KEY` is set, retries on 429/5xx honouring
  `Retry-After`, and stops after `--max-failures` consecutive failures.
- **`.github/workflows/urlscan-probe.yml`** — daily at `11 5 * * *` plus manual
  dispatch, with `limit`/`size`/`sleep`/`seed` inputs. `permissions: contents: read`
  and it commits nothing; results upload as a 14-day artifact. Deliberately
  separate from `ct-grab.yml`, which holds `contents: write` + `git push`.
- 15 tests in `tests/test_probe.py` on `httpx.MockTransport` — no network.

### Notes

- urlscan.io does not publish the numeric rate limits or the `size` default and
  maximum. Defaults here (25 domains, `size=1`, 6s between requests, 3 retries)
  are deliberately conservative rather than derived from a documented quota.

### Fixed

- **Infrastructure hostnames were emitted as candidates.**
  `0digicert8i7rtp.aws-prod24…sfdc.net`, `…rtpuyq.usw2.cache.amazonaws.com`,
  `…rtpxxx.mongodb-dev.net` and similar machine-generated names passed the filter
  because `rtp` there is followed by `.`, which the left-hand boundary rule in
  `is_candidate()` never examined. `INFRA_SUFFIXES` now rejects hostnames under
  shared infrastructure (Salesforce, AWS, Azure, observability, dev database
  endpoints, wildcard resolvers). Applied to the committed `domain.txt`:
  **97,334 → 97,183** rows; all 151 removed entries were infrastructure hosts.

  Shared hosting (`pages.dev`, `workers.dev`, `vercel.app`) is deliberately
  **not** rejected — thousands of real gambling sites are hosted there.

### Notes

- Narrowing the `rtp` rule itself was measured and rejected. Of the 2,998 rows
  containing `rtp`, 2,020 have `rtp` followed by a letter, and most are genuine
  brands (`rtperigo4d.top`, `rtplivejackpot338.com`, `rtpibetslot4.life`).
  Requiring a digit or separator after `rtp` would have discarded them. Filtering
  is therefore done per host, not by tightening the keyword boundary.

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
