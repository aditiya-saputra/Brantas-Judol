# Brantas-Judol

Certificate Transparency–based **discovery layer** for suspected Indonesian online
gambling (`judol`) domains.

> **Status: discovery, evidence probe, and an experimental LLM classifier.**
> `probe.py` checks urlscan.io for existing scans; `classify.py` asks Gemini to
> score a candidate 0–100 and buckets it into `blocklist.txt`, `whitelist.txt`,
> or `review.txt`. **None of that output is committed** — it is LLM output, not
> human review. A listed domain is still a candidate signal.

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

`.github/workflows/urlscan-evidence.yml` runs daily at `17 4 * * *`:

```
pytest → probe.py --merge-out → commit urlscan-evidence.json (only if changed)
```

It is the only workflow besides `ct-grab.yml` that holds `contents: write`, and
the only thing it writes is the read-only urlscan.io search result. The ledger
it produces is what `classify.yml` reads as evidence.

`.github/workflows/classify.yml` runs daily at `41 7 * * *` plus manual
dispatch, needs the `GEMINI_API_KEY` secret, and is `contents: read`. It uses
the committed `urlscan-evidence.json` when that file exists and falls back to
running `probe.py` itself otherwise — so it never spends urlscan.io quota on a
normal day.

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
   **[urlscan.io](https://urlscan.io) is the evidence source for this stage** —
   a read-only search exists (`probe.py`), classification does not. See
   [Evidence probing](#evidence-probing-urlscanio).
5. **Harden CI** — dependencies are unpinned in a job holding `contents: write`
   and `git push`. Move to a lock file with `--require-hashes`.
6. **Repository hygiene** — `ct.db` is a binary committed every run; `domain.txt`
   grows monotonically. Both need rotation or an artifact store.

If the full pipeline is built, **prompt injection is the top security concern**:
page content fetched from attacker-controlled sites will be fed to an LLM that
decides what gets blocked. Never let site text act as instructions, and never let
an LLM alone add an entry to a public blocklist.

---

## Evidence probing (urlscan.io)

urlscan.io scans URLs and stores the HTTP transactions, DOM, and screenshots in
a public, searchable archive. For this project it answers the question
`domain.txt` deliberately does not: *does this candidate actually serve gambling
content?* Discovery produces candidates; urlscan.io supplies the evidence.

**What exists today: search only.** `probe.py` checks whether a candidate has
ever been scanned and prints the summary. There is no classification and no
blocklist export — a row in `domain.txt` still proves only that a certificate
was issued.

Probing **never** runs inside `ct-grab.yml`: that job holds `contents: write` and
`git push`, and adding a third-party API call there adds a failure mode to the
job that mutates state. Use the separate `urlscan-probe` workflow, which is
read-only (`contents: read`) and commits nothing.

### Search

```
GET https://urlscan.io/api/v1/search/?q=domain:<domain>&size=<n>
```

| | |
|---|---|
| Query | `q` — Lucene-style; `domain:example.com` matches scans contacting that domain |
| Page size | `size` — undocumented default and maximum; keep requests small |
| Response keys | `results`, `total`, `has_more`, `took`, `search_date_limit_days` |
| Hidden result page | `https://urlscan.io/result/<uuid>/` — cite this as evidence |

Unauthenticated search returns `200 OK` — verified 2026-11-10 against a domain
with no scans (`total: 0`, no error field). An API key is still expected for
sustained use: the [API introduction](https://docs.urlscan.io/pages/api-intro)
states that unauthenticated users only receive minor quotas, while not publishing
the numeric limits. Treat the quotas as unknown until measured.

### Running it

```bash
# Cek 25 kandidat acak, jeda 6s antar request
python probe.py

# Lebih spesifik
python probe.py --limit 50 --size 5 --sleep 4 --seed 42 --json-out probe.json
```

| Flag | Default | Purpose |
|---|---|---|
| `--domains` | `domain.txt` | Source of candidates |
| `--limit` | `25` | How many domains to check |
| `--size` | `1` | Scans requested per domain |
| `--sleep` | `6.0` | Delay between requests, in seconds |
| `--seed` | `0` | Sample seed — same seed, same set |
| `--max-failures` | `3` | Stop after N consecutive failed requests |
| `--json-out` | — | Write results as JSON |
| `--merge-out` | — | Merge results into the committed evidence ledger |

Set `PROBE_API_KEY` to send an `API-Key` header. Without it the script still
runs — the endpoint is public — but you get the anonymous quota.

In CI, `.github/workflows/urlscan-probe.yml` runs on manual dispatch only — the
daily scheduled probing lives in `urlscan-evidence.yml`, which commits its
results to the repo. Two daily jobs on the same seed would probe the exact
same 25 domains twice and waste urlscan.io quota, so the read-only probe
workflow was left manual, with a larger budget for one-off deep runs. It
sends `secrets.URLSCAN_API_KEY` when that secret exists, uploads
`probe-result.json` as an artifact (kept 14 days), and never commits.

### The evidence ledger (`urlscan-evidence.json`)

`probe.py` can merge its results into a ledger file that **is committed to this
repo**. That ledger, not a per-run artifact, is what `classify.py` reads as
evidence — so the classification job spends **zero** urlscan.io quota.

```bash
python probe.py --limit 25 --merge-out urlscan-evidence.json
```

| | |
|---|---|
| Format | `{"schema": 1, "generated_at": <unix>, "domains": [...]}` |
| Keyed by | domain |
| Per domain | `total`, `uuids[]`, `first_url`, `first_scanned`, `checked_at` |
| Merge rule | `uuids` are **unioned**, never overwritten — evidence found once stays |
| Cap | 50 uuid per domain |
| Ordering | domains sorted, so a repo diff appears only when the data really changed |

`domains_listed` in `stats.json` counts candidates; the ledger counts what was
actually **checked** against urlscan.io, including the ones with no scan at
all (`total: 0`). Those zero-hit entries are deliberate: they are the record
that a domain was probed and nothing was found.

The ledger holds URLs, page titles, and domains only. No DOM content, no
screenshots — the same restriction `classify.py` already enforces.

### Constraints for whoever builds the rest

- **A result is evidence, not a verdict.** A scan containing a candidate's domain
  does not prove it is a gambling service. Candidates shared with `pages.dev`,
  `workers.dev` or `vercel.app` must never be escalated into blocklist entries
  from a single scan.
- **Scan text is attacker-controlled.** The DOM and page titles returned by
  urlscan.io come from the suspected gambling site itself. They must never be
  treated as instructions (see the prompt-injection note above), and must never
  be the sole basis for an automated blocklist addition.
- **Keep it out of CI by default.** The current workflow runs every 6 hours with
  `contents: write` and `git push`. Adding a third-party API call there means an
  API key in CI secrets and one more external failure mode in a job that commits
  state. Probe on demand or as a separate, read-only workflow.
- **Respect the quota.** `domain.txt` currently holds ~97,000 candidates with no
  backpressure; querying all of them is not viable. Sample, cache, and respect
  rate-limit responses instead of retrying blindly.

---

## LLM classification (Gemini)

**Experimental, unverified output.** Two steps, deliberately separate:

```bash
python probe.py --limit 10 --size 10 --sleep 15 --json-out probe.json
python classify.py --evidence probe.json
```

`probe.py` collects evidence from urlscan.io; `classify.py` reads that file and
asks Gemini to score each domain 0–100 with Google Search grounding. **classify
never calls urlscan.io itself.**

That split exists because of a real failure: classify run #1 called urlscan on
its own and got `HTTP 429` on the first three domains, so zero domains were
scored. Probing now happens once, with the throttle `probe.py` already has, and
classify degrades gracefully — if the evidence file is empty or missing, it still
scores the candidates with no evidence (and says so in the output).

The score is bucketed:

| Score | File | Meaning |
|---|---|---|
| `>= 80` | `blocklist.txt` | strong signal — **still not a verified blocklist** |
| `<= 20` | `whitelist.txt` | most likely not gambling |
| `21–79` | `review.txt` | ambiguous, needs a human |

**The thresholds are 80/20, not 50/50.** A domain scoring 49 and one scoring 51
are indistinguishable, and the constraint above says an LLM must never on its own
add an entry to a public blocklist. The grey zone forces borderline cases into
`review.txt` where a human decides. Override with `--block-threshold` /
`--allow-threshold`.

All three files are **gitignored**. The `classify` workflow uploads them as a
14-day artifact and commits nothing — publishing unreviewed LLM output into a
public repository is the same as publishing an unreviewed blocklist.

### API surface

Verified 2026-11-10 against the official docs:

```
POST https://generativelanguage.googleapis.com/v1beta/interactions
headers: x-goog-api-key, Content-Type: application/json, Api-Revision: 2026-05-20
body:    model, input, system_instruction,
         tools: [{"type": "google_search"}],
         response_format: {type: "text", mime_type: "application/json", schema: ...}
response: steps[-1].content[0].text   (JSON matching the schema)
```

Default model is `gemini-3.8-flash`. Note this is the Interactions API — not
`generateContent`, which the current docs no longer use for this endpoint.

### Running it

```bash
export GEMINI_API_KEY=...                      # never commit this
python probe.py --limit 10 --json-out probe.json
python classify.py --evidence probe.json       # writes the three files
```

| Flag | Default | Purpose |
|---|---|---|
| `--domains` | `domain.txt` | Candidate source |
| `--evidence` | — | `probe.py` JSON output to use as evidence |
| `--limit` | `10` | Domains per run (used when there is no evidence) |
| `--seed` | `0` | Sample seed |
| `--links` | `10` | Evidence URLs sent per domain |
| `--model` | `gemini-3.8-flash` | Gemini model |
| `--block-threshold` | `80` | Minimum score for `blocklist.txt` |
| `--allow-threshold` | `20` | Maximum score for `whitelist.txt` |
| `--out-dir` | `.` | Where the three files go |
| `--sleep` | `2.0` | Delay between domains |
| `--json-out` | — | Full results including `reason` |

In CI, `.github/workflows/classify.yml` runs daily at `41 7 * * *` plus manual
dispatch, needs the `GEMINI_API_KEY` secret, and is `contents: read`. Evidence
comes from the committed ledger `urlscan-evidence.json` when it exists; the
workflow then classifies a `--limit` sample of the domains in that ledger,
using a day-of-year seed so consecutive days do not judge the same domains.
Manual dispatch can pin the seed for reproducibility.

### Safety properties worth keeping

- **Site text is data, never instruction.** The system instruction forbids
  following anything inside the evidence, and only URL/title/domain metadata from
  urlscan.io is sent — never DOM content.
- **Absence of evidence lowers confidence.** A domain with no urlscan.io scans is
  told explicitly that evidence is missing rather than scored as if clean.
- **Every decision carries a `reason`**, so a wrong bucket can be audited after
  the fact.
- **The model cannot promote a shared platform.** The instruction names
  `pages.dev`, `workers.dev`, `vercel.app`, `netlify.app` and `blogspot.com` as
  never-gambling-by-association.

---

## Disclaimer

Detection output is probabilistic. A domain listed here is **not** an assertion
that it operates an online gambling service. The project's goal is an
evidence-driven pipeline whose mistakes are auditable — not the largest possible
list.

---

## License

[MIT](LICENSE) © 2026 Aditiya Saputra.
