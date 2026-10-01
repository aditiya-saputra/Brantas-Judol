# Brantas-Judol

Certificate Transparency–based **discovery layer** for suspected Indonesian online
gambling (`judol`) domains.

> **Status: only the discovery layer is implemented.** The probing, classification,
> blocklist-export and feedback stages described in the project vision are **not
> built yet**. Everything below documents what actually runs today.

---

## What this is (and what it is not)

A domain found by this tool is a **candidate signal, not a confirmed gambling
service.** `casino-example.com` proves only that somebody requested a certificate
containing that name.

This repository currently produces a **candidate list**. It does **not** produce a
blocklist, and nothing here should be treated as one.

---

## Pipeline (as implemented)

```
CT log registry (Google + Apple)
        │  state == "usable", dedup by log_id  → 27 logs
        ▼
    get-sth                     → tree_size
        ▼
    get-entries                 → batch 256, sleep 0.25s
        ▼
  TLS wire parse (RFC 6962)     → TLSReader, hand-written
        ▼
  X.509 parse                   → SAN + CN
        ▼
  normalize                     → lowercase, strip wildcard, IDNA, validate
        ▼
  keyword filter                → strong keywords + boundary-aware "rtp"
        ▼
   domain.txt                   ← appended, sorted+deduped by CI
```

> Full diagrams — covering error paths, the two-phase cursor, the state rules
> that prevent holes, and the CI workflow — are in **[FLOWCHART.md](FLOWCHART.md)**.
> Release history is in **[CHANGELOG.md](CHANGELOG.md)**.

Scan position lives in **`ct.db`** (SQLite), two pointers per log:

| Column | Meaning |
|---|---|
| `tail_index` | Newest entry already processed — the primary pointer |
| `next_index` | Backlog position scanning from the start of the log |
| `updated_at` | Used for LRU rotation across logs |

**`ct.db` and `domain.txt` are committed in the same commit.** If a run fails,
both roll back together, so the cursor can never advance past output that was
lost. This lockstep property is deliberate — keep it that way.

---

## Honest coverage

Read this before tuning any budget. Measured on **2026-10-01** against the 27 logs
in `ct.db`:

| Metric | Value |
|---|---|
| Total entries across all logs | ~41.2 billion |
| New entries arriving per 6h run | ~21.6 million |
| Budget per run | 200,000 |
| **Coverage of newly issued entries** | **~0.46%** |
| Entries never scanned (backlog) | ~41.2 billion |
| Measured throughput | ~174 entries/s (batch 256, sleep 0.25) |

Two consequences worth understanding:

**1. This is a sampler, not a full scanner.** Each run reads the newest slice of
each log. The rest is skipped by *re-anchoring* — `tail_index` jumps forward to
near `tree_size` when the lag exceeds the budget.

**2. `tree_size - tail_index` is a lying metric.** Because of re-anchoring it
reads **99.95%** while real coverage is **0.46%**. Never use it. Use
`print_coverage()`, which counts entries actually read versus entries skipped:

```
cakupan aliran : 6,000 dari 6,000 entri baru = 100.00% (dilewati re-anchor: 0)
backlog         : 6,644,700,363 entri belum pernah discan (next_index belum menyusul ujung tree)
```

**Why the backlog does not catch up.** `next_index` scans from 0 over ~41.2
billion entries while the budget is ~200,000 per run. Full coverage is
arithmetically out of reach at this budget. Deliberately, *all* budget goes to
the tail: those are the newly issued certificates, and for this mission they are
the only ones with value. Splitting budget to "feed" the backlog would cost real
recall on fresh domains and buy essentially nothing against 41 billion.

Raising coverage requires either substantially more throughput (see
[Roadmap](#roadmap)), a source that filters server-side, or accepting sampling —
not a different budget split.

---

## Install

```bash
pip install -r requirements.txt
```

Tested on Python 3.12; CI runs 3.13. Two dependencies only: `httpx` and
`cryptography`.

---

## Usage

```bash
# List usable CT logs from the registries, then exit
python grab.py --list-logs

# Full run (defaults: 200k entries, 1200s, writes domain.txt + ct.db)
python grab.py

# Debug against a single log
python grab.py --log-url https://ct.googleapis.com/logs/us1/argon2026h2
```

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `ct.db` | SQLite cursor path |
| `--out` | `domain.txt` | Output path (opened in append mode) |
| `--max-entries` | `200000` | Hard budget of entries per run |
| `--max-seconds` | `1200` | Wall-clock deadline per run |
| `--tail-window` | `100000` | Newest entries taken when bootstrapping |
| `--batch` | `256` | Entries per request (clamped to 4096) |
| `--sleep` | `0.25` | Delay between batches |
| `--log-url` | — | Restrict to one log (debugging) |
| `--list-logs` | — | Print usable logs and exit |

`--max-seconds` is the real guard. If throughput drops, the deadline cuts the run
cleanly — each log's cursor is already committed, and logs left untouched keep
their position at the front of the next run's rotation.

---

## Output

**`domain.txt`** — one candidate per line, sorted and deduplicated by CI.

```bash
wc -l domain.txt      # 7165 as of 2026-10-01
```

Running locally appends without cross-run deduplication; `sort -u` in the
workflow is what makes it unique. Expect duplicates if you run repeatedly by hand.

**`ct.db`** — scan position. This is state, not data; it must move in lockstep
with `domain.txt`.

---

## Tests

```bash
python -m pytest -q
```

**81 passed, 1 skipped.** Everything runs on `httpx.MockTransport` — no network.

Coverage spans the TLS reader, X.509 extraction (fixture-based, one `x509_entry`
and one `precert_entry`), normalization, keyword filtering, cursor schema
migration, and the orchestrator (`fetch_range` / `grab_log`) — including the
invariants that matter most:

- the budget is a hard cap
- cursors only advance when there is real progress, so a mid-range fetch failure
  cannot open a hole
- `updated_at` is not touched when a log fails, keeping it first in the next run
- re-anchor skips are reported rather than silently discarded

---

## Automation

`.github/workflows/ct-grab.yml` runs every 6 hours (`23 */6 * * *`):

```
pytest → grab.py → sort -u domain.txt → commit domain.txt + ct.db
```

Concurrency is serialized (`cancel-in-progress: false`) so two runs never race on
the cursors. If nothing changed, no commit is made.

---

## Roadmap

Ordered by expected impact. Not built yet.

1. **Parallelize across logs** — throughput is the binding constraint. Logs live
   on independent hosts (Google, Cloudflare, DigiCert, Sectigo) and each log's
   cursor is already independent in SQLite, so a worker pool is a contained
   change. Needs a concurrency decision: aggressive parallelism risks `429`s from
   CT operators. Realistic ceiling roughly **8–15×** current throughput.
2. **Reduce input volume** — the 27 logs contain heavily overlapping certificates.
   Picking a subset, or moving filtering server-side (e.g. crt.sh), would raise
   recall per byte far more than raw speed.
3. **Normalize to registrable domain (eTLD+1)** — output currently contains full
   hostnames such as `…eastasia.redis.azure.net`. Care required: shared platform
   suffixes (`pages.dev`, `plesk.page`) must **never** become blocklist entries,
   or blocking one would break an entire platform for everyone.
4. **HTTP probing → evidence → classification** — the downstream pipeline. Note
   that the 1,000 domains/6h target was implicitly calibrated to the current ~0.5%
   sampling rate; fixing discovery multiplies candidate volume and will saturate
   this stage. `domain.txt` is currently an unbounded queue with no backpressure.
5. **Harden CI** — dependencies are unpinned in a job holding `contents: write`
   and `git push`. Move to a lock file with `--require-hashes`.
6. **Repository hygiene** — `ct.db` is a binary committed every run; `domain.txt`
   grows monotonically. Both need rotation or an artifact store.

If the full pipeline is built, **prompt injection is the top security concern**:
page content fetched from attacker-controlled sites will be fed to an LLM that
decides what gets blocked. Never let site text act as instructions, and never let
an LLM alone add an entry to a public blocklist.

---

## Disclaimer

Detection output is probabilistic. A domain listed here is **not** an assertion
that it operates an online gambling service. The project's goal is an
evidence-driven pipeline whose mistakes are auditable — not the largest possible
list.

---

## License

[MIT](LICENSE) © 2026 Aditiya Saputra.
