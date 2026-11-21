# Flowchart

Diagrams of the pipeline **as it actually runs today**. Rendered with Mermaid.

- [1. Main run](#1-main-run)
- [2. Per-log: two-phase cursor](#2-per-log-two-phase-cursor)
- [3. `fetch_range`](#3-fetch_range)
- [4. Entry handler](#4-entry-handler)
- [5. Cursor state rules](#5-cursor-state-rules)
- [6. CI workflow (`ct-grab`)](#6-ci-workflow-ct-grab)
- [7. `probe.py` — search & request scan](#7-probepy--search--request-scan)
- [8. CI workflow (`urlscan-evidence`)](#8-ci-workflow-urlscan-evidence)
- [9. CI workflow (`classify`)](#9-ci-workflow-classify)

---

## 1. Main run

Orchestrates logs in LRU order (`updated_at` ascending), re-splitting budget and
time after every log so a fast log hands its share back to the next one.

```mermaid
flowchart TD
    Start(["python grab.py"]) --> Cfg["Parse CLI<br/>clamp batch ≤ 4096"]

    Cfg --> ListLogs{"--list-logs?"}
    ListLogs -- yes --> PrintLogs["Print usable logs<br/>exit"] --> EndOK

    ListLogs -- no --> Load{"--log-url?"}
    Load -- yes --> OneLog["Single custom log"]
    Load -- no --> Registry["load_logs<br/>Google + Apple registries<br/>state == usable, dedup by log_id"]

    OneLog --> Sort
    Registry --> NoLogs{"empty?"}
    NoLogs -- yes --> Fatal["FATAL — no usable logs<br/>exit 1"] --> EndFail
    NoLogs -- no --> Sort["Sort by updated_at<br/>oldest first (LRU)"]

    Sort --> OpenOut["Open domain.txt (append)<br/>init make_handler"]

    OpenOut --> Loop{"Next log"}

    Loop -- none left --> Summary
    Loop -- has next --> Guards{"budget ≤ 0<br/>or time up?"}
    Guards -- yes --> SkipAll["Report N logs skipped<br/>break"] --> Summary

    Guards -- no --> Slice["remaining = logs left<br/>time_slice = sisa waktu / remaining<br/>budget_slice = budget / remaining"]

    Slice --> MinGuard{"position > 1<br/>and time_slice < 3s?"}
    MinGuard -- yes --> TooShort["Sisa waktu terlalu kecil<br/>break<br/>(untouched logs stay first next run)"] --> Summary
    MinGuard -- no --> Grab["grab_log<br/>see diagram 2"]

    Grab --> Deduct["budget -= used<br/>stats.skipped += skipped<br/>stats.backlog += backlog"]
    Deduct --> PrintLog["print +N entri, tail, next<br/>(+ lewati if skipped)"] --> Loop

    Summary["Print summary"] --> Cover["print_coverage<br/>see diagram 1"]

    Cover --> Flow{"entries + skipped > 0?"}
    Flow -- yes --> FlowOut["cakupan aliran<br/>= entries / (entries + skipped)"]
    Flow -- no --> FlowNA["cakupan aliran : n/a (bootstrap)"]
    FlowOut --> Back
    FlowNA --> Back
    Back{"backlog > 0?"} -- yes --> BackOut["backlog : N entri<br/>belum pernah discan"] --> EndOK
    Back -- no --> EndOK

    EndOK(["exit 0"]) --> Finally
    EndFail(["exit 1"]) --> Finally
    Finally["finally: conn.close()<br/>client.close()"]

    classDef err fill:#fde2e2,stroke:#c0392b
    class Fatal,EndFail err
```

### `print_coverage`

Two numbers, always printed together. Reading only the first is misleading.

```mermaid
flowchart LR
    A["entries<br/>truly read"] --> P
    B["skipped by re-anchor"] --> P
    P["cakupan aliran<br/>= A / (A + B)"]
    C["backlog<br/>never scanned"] --> Q["backlog : N entri"]
    P --- Q

    X["tree_size − tail_index<br/>≈ 99.95%"] -.->|"JANGAN DIPAKAI"| P
    style X fill:#fde2e2,stroke:#c0392b,stroke-width:2px
```

`tree_size − tail_index` is excluded on purpose: re-anchor jumps `tail_index` to
the tip of the tree, so it reads ~100% while actual coverage measured **0.46%**.

---

## 2. Per-log: two-phase cursor

`grab_log`. Tail first (newest entries), backlog second with whatever budget is
left. The critical property: **the cursor is only written when there is real
progress**, so a failed fetch cannot open a hole.

```mermaid
flowchart TD
    Start(["grab_log"]) --> G1{"deadline passed<br/>or budget ≤ 0?"}
    G1 -- yes --> R0["return current cursor<br/>updated_at UNTOUCHED<br/>(log stays first next run)"]

    G1 -- no --> STH["GET /ct/v1/get-sth"]
    STH -- fail --> R0

    STH -- ok --> Tree["tree_size = STH.tree_size"]
    Tree --> ReadCur["read next_index, tail_index"]
    ReadCur --> Clamp{"cursor > tree_size?"}
    Clamp -- tail too big --> ResetTail["tail_index = 0"]
    Clamp -- next too big --> ClampNext["next_index = tree_size"]
    Clamp -- ok --> G2
    ResetTail --> ClampNext
    ClampNext --> G2{"deadline passed<br/>or budget ≤ 0?"}
    G2 -- yes --> R1["return<br/>updated_at UNTOUCHED"]

    G2 -- no --> Desired["desired = min(tail_window, budget)<br/>tail_start = max(tail_index, tree_size − desired)"]

    Desired --> Skipped["skipped = max(0, tail_start − tail_index)<br/>only if tail_index > 0<br/><i>bootstrap history is NOT 'skipped'</i>"]
    Skipped --> Warn{"skipped > 0?"}
    Warn -- yes --> Reanchor["stderr: re-anchor tail:<br/>lewati N entri"] --> HadWork
    Warn -- no --> HadWork["had_work = tail_start < tree_size<br/>or next_index < tail_start"]

    HadWork --> Phase1{"tail_start < tree_size?"}
    Phase1 -- yes --> TailFetch["fetch_range<br/>tail_start → tree_size − 1<br/>budget = full slice"]
    Phase1 -- no --> Phase2

    TailFetch --> TailAdv{"pos > tail_start?"}
    TailAdv -- yes --> TailOk["tail_index = pos<br/>used += n"]
    TailAdv -- no --> Phase2["gap_end = tail_start"]
    TailOk --> Phase2

    Phase2 --> TailOK2{"tail_index > 0<br/>or tail_start ≥ tree_size?"}
    TailOK2 -- no --> Save["bootstrap: backlog HELD BACK<br/>so the gap has exactly one owner"]

    TailOK2 -- yes --> NextCheck{"next_index ≥ gap_end?"}
    NextCheck -- yes --> FastFwd["next_index = max(next_index, tail_index)"]
    NextCheck -- no --> BudgetCheck{"used < budget<br/>and deadline ok?"}

    BudgetCheck -- no --> Save
    BudgetCheck -- yes --> BackFetch["fetch_range<br/>next_index → gap_end − 1<br/>budget = slice − used"]
    BackFetch --> BackAdv["next_index = pos<br/>used += n"]
    BackAdv --> BackCatch{"next_index ≥ gap_end?"}
    BackCatch -- yes --> FastFwd
    BackCatch -- no --> Save
    FastFwd --> Save

    Save --> Commit{"used > 0<br/>or not had_work?"}
    Commit -- yes --> Write["set_cursor<br/>(commits next_index, tail_index,<br/>url, description, updated_at)"]
    Commit -- no --> SkipWrite["do NOT write<br/>failed work → retried first next run"]

    Write --> Ret
    SkipWrite --> Ret["return LogResult<br/used, next_index, tail_index,<br/>skipped, backlog"]

    classDef warn fill:#fff4d6,stroke:#d68910
    classDef err fill:#fde2e2,stroke:#c0392b
    class Reanchor,Warn warn
    class R0,R1,SkipWrite err
```

### Why the backlog does not catch up

```mermaid
flowchart LR
    A["backlog must sweep<br/>~41.2 billion entries<br/>from next_index = 0"] --> B["budget ≈ 200,000 / run"]
    B --> C["≈ 2 million runs needed"]
    C --> D["so ALL budget goes to tail:<br/>newly issued certs are the only<br/>ones with value here"]
    style D fill:#e8f8f5,stroke:#1abc9c
```

Splitting budget to feed the backlog would cost real recall on fresh domains and
buy essentially nothing against 41 billion. See the coverage section in README.

---

## 3. `fetch_range`

Fetches `[start, stop]` inclusively. Two hard guards per iteration: the budget and
the deadline.

```mermaid
flowchart TD
    Start(["fetch_range start..stop"]) --> Loop{"pos ≤ stop<br/>and used < budget<br/>and now < deadline?"}
    Loop -- no --> Ret["return (pos, used)"]

    Loop -- yes --> Clamp["remaining = budget − used<br/>end = pos + min(batch, remaining) − 1<br/>end = min(end, stop)"]
    Clamp --> Get["GET /ct/v1/get-entries?start&end"]

    Get -- error --> Err["stderr: fetch error @pos<br/>break<br/><b>pos NOT advanced</b>"] --> Ret
    Get -- ok --> Empty{"entries empty?"}
    Empty -- yes --> EmptyStop["break — server done"] --> Ret

    Empty -- no --> Handle["for each entry: handle(raw)"]
    Handle --> Advance["pos += len(entries)<br/>used += len(entries)"]
    Advance --> Sleep{"sleep > 0?"}
    Sleep -- yes --> Zzz["time.sleep(sleep)"] --> Loop
    Sleep -- no --> Loop

    classDef err fill:#fde2e2,stroke:#c0392b
    class Err,EmptyStop err
```

> **Fixed in 0.2.0:** the batch was not clamped to `remaining`, so each call
> overshot the budget by up to `batch − 1` entries. `--max-entries` was not a hard
> cap.

Server returning fewer entries than requested is legal (RFC 6962) and is handled:
`pos` advances by the actual count.

---

## 4. Entry handler

```mermaid
flowchart TD
    Raw["entry (b64)"] --> Count["stats.entries += 1"]
    Count --> Parse["domains_from_entry<br/>parse_leaf → parse_extra → X.509 SAN + CN"]

    Parse -- raises --> Fail["stats.parse_fail += 1"]
    Fail --> Sample{"parse_fail ≤ 5?"}
    Sample -- yes --> Log["stderr: parse_fail #N<br/>TypeName: message"]
    Sample -- no --> Drop["count only"]
    Log --> Ret
    Drop --> Ret["return"]

    Parse -- ok --> Norm["normalize_domain<br/>lowercase · strip *. · strip trailing dot<br/>IDNA · validate · drop all-numeric"]
    Norm --> Dedup{"domain in seen?"}
    Dedup -- yes --> Ret
    Dedup -- no --> Add["seen.add(domain)"]
    Add --> Cand{"is_candidate?"}
    Cand -- no --> Ret
    Cand -- yes --> Write["write + flush<br/>stats.written += 1"] --> Ret

    classDef warn fill:#fff4d6,stroke:#d68910
    class Fail,Sample,Log warn
```

`seen` is per-run only, so cross-run uniqueness comes from `sort -u` in CI.

---

## 5. Cursor state rules

The single most important invariant: **cursor and output advance together, or not
at all.**

```mermaid
flowchart TD
    subgraph Written ["set_cursor IS called — updated_at bumped, moves to back of queue"]
        W1["used > 0 — real progress"]
        W2["not had_work — nothing to do, log is current"]
    end

    subgraph NotWritten ["set_cursor NOT called — stays first next run, retried immediately"]
        N1["get-sth failed"]
        N2["fetch failed before any progress"]
        N3["budget/deadline exhausted"]
    end

    W1 --> Effect["domain.txt and ct.db commit together"]
    W2 --> Effect
    N1 --> Retry["log reappears at front of next run"]
    N2 --> Retry
    N3 --> Retry

    Effect --> Invariant["<b>No hole possible:</b><br/>next_index never jumps past<br/>an uncovered region"]
    Retry --> Invariant

    classDef ok fill:#e8f8f5,stroke:#1abc9c
    classDef err fill:#fde2e2,stroke:#c0392b
    class W1,W2,Effect ok
    class N1,N2,N3,Retry err
```

### Bootstrap safety

```mermaid
flowchart LR
    A["tail_index = 0<br/>(never scanned)"] --> B{"tail made progress?"}
    B -- no --> C["backlog HELD BACK<br/>tail_ok = False"]
    B -- yes --> D["backlog may run<br/>gap has one owner"]
    C --> E["no gap left uncovered<br/>by anyone"]
    D --> E
```

---

## 6. CI workflow (`ct-grab`)

`.github/workflows/ct-grab.yml` — cron `23 */6 * * *`, 30-minute job timeout,
`concurrency` group with `cancel-in-progress: false` so two runs never race on
the cursors.

```mermaid
flowchart TD
    Cron["schedule: 23 */6 * * *<br/>or workflow_dispatch"] --> Co{"another run<br/>in progress?"}
    Co -- yes --> Queue["queue — do NOT cancel<br/>(cursors must not race)"]
    Queue --> Co

    Co -- no --> Checkout["actions/checkout@v4"]
    Checkout --> Py["setup-python 3.13<br/>pip cache"]
    Py --> Deps["pip install -r requirements.txt pytest"]
    Deps --> Test["pytest -q<br/>81 tests, no network"]

    Test -- fail --> Stop["job fails<br/>nothing committed<br/>cursor + output roll back together"]
    Test -- ok --> Grab["grab.py --max-entries 200000<br/>--max-seconds 1200"]

    Grab -- nonzero exit --> Stop
    Grab -- ok --> Dedupe["sort -u domain.txt<br/>wc -l"]
    Dedupe --> Diff{"git diff<br/>cached empty?"}
    Diff -- yes --> Noop["no changes<br/>skip commit"] --> Done
    Diff -- no --> Commit["git add domain.txt ct.db<br/>git commit -m 'update <timestamp>'"]
    Commit --> Push["git push"] --> Done(["done"])

    classDef err fill:#fde2e2,stroke:#c0392b
    class Stop err
    classDef ok fill:#e8f8f5,stroke:#1abc9c
    class Noop,Done ok
```

`ct.db` and `domain.txt` are staged in the same `git add`. That is what makes the
rollback consistent — the two must never be committed separately.

---

## 7. `probe.py` — search & request scan

`probe.py` does two things in a single pass over the sampled domains:

1. **Search** (`GET /api/v1/search/`) — find scans that already exist.
2. **Request scan** (`POST /api/v1/scan/`) — submit a URL for domains with no
   existing scan, then poll for the result.

```mermaid
flowchart TD
    Start(["python probe.py"]) --> CLI["Parse CLI<br/>--submit-scan, --max-submit,<br/>--visibility, --poll-seconds"]

    CLI --> Quota{"--check-quota?"}
    Quota -- yes --> PrintQ["GET /api/v1/quotas<br/>print + exit"] --> EndOK

    Quota -- no --> LoadDom["load_candidates<br/>domain.txt"]
    LoadDom --> Sample["pick_sample<br/>(limit, seed)"]
    Sample --> APIKey{"PROBE_API_KEY<br/>set?"}
    APIKey -- no --> DisableScan["submit_budget = 0<br/>search only"]
    APIKey -- yes --> KeyOK["headers['API-Key'] = key"]
    DisableScan --> ReadLedger
    KeyOK --> ReadLedger

    ReadLedger{"--merge-out?"} -- yes --> LoadEvid["load_evidence<br/>existing ledger"]
    ReadLedger -- no --> EmptyEvid["existing = {}"]
    LoadEvid --> Loop
    EmptyEvid --> Loop

    Loop{{"Next domain"}} -- none left --> Summary
    Loop -- has next --> Search["GET /api/v1/search/<br/>domain:__domain__"]

    Search -- error --> Fail{">= max_failures<br/>berturut-turut?"}
    Fail -- yes --> Stop["BERHENTI"] --> Summary
    Fail -- no --> Loop

    Search -- ok --> Row["row = {domain, total,<br/>uuids, first_url, ...}"]

    Row --> ShouldScan{"--submit-scan<br/>AND total == 0<br/>AND budget > 0?"}

    ShouldScan -- no --> Append
    ShouldScan -- yes --> Already{"ledger punya<br/>scan_uuid atau<br/>scan_pending_uuid?"}
    Already -- yes --> Append

    Already -- no --> Submit["POST /api/v1/scan/<br/>{url, visibility}<br/>budget -= 1"]
    Submit -- error --> SubmitFail["stderr: gagal<br/>lanjut"] --> Append
    Submit -- ok --> Poll["poll_result<br/>GET /api/v1/result/{uuid}/<br/>wait up to poll-seconds"]

    Poll -- done --> Summarize["summarize_scan<br/>title, page_url, http_status,<br/>contacted, verdict, screenshot"]
    Summarize --> SetDone["scan_uuid = uuid<br/>scan_status = done"]
    SetDone --> Sleep1{"submit_sleep<br/>& budget > 0?"}
    Sleep1 -- yes --> Zzz["time.sleep"] --> Append
    Sleep1 -- no --> Append

    Poll -- timeout --> Pending["scan_pending_uuid = uuid<br/>scan_status = pending"]
    Pending --> Sleep1

    Append["rows.append(row)<br/>print status"] --> SleepN{"index < len?"}
    SleepN -- yes --> SleepD["time.sleep(sleep)"] --> Loop
    SleepN -- no --> Loop

    Summary["summarize(rows)"] --> MergeQ{"--merge-out?"}
    MergeQ -- yes --> Merge["merge_evidence<br/>union uuids, max total,<br/>preserve done scan"]
    Merge --> Write["write_evidence<br/>urlscan-evidence.json"]
    Write --> JsonQ
    MergeQ -- no --> JsonQ{"--json-out?"}
    JsonQ -- yes --> JsonOut["write JSON"] --> EndOK
    JsonQ -- no --> EndOK(["exit 0"])

    classDef err fill:#fde2e2,stroke:#c0392b
    class Stop,SubmitFail err
    classDef ok fill:#e8f8f5,stroke:#1abc9c
    class EndOK ok
```

### Merge rules

```mermaid
flowchart LR
    subgraph Evidence ["Scan evidence fields"]
      A["scan_uuid<br/>scan_status<br/>scan_title<br/>scan_page_url<br/>scan_http_status<br/>scan_screenshot<br/>scan_contacted<br/>scan_malicious<br/>scan_score<br/>scan_scanned_at"]
    end
    subgraph Pending ["Pending fields"]
      B["scan_pending_uuid"]
    end

    New{"new status?"} -- done --> Replace["Replace ALL evidence<br/>from new row<br/>(clean slate)"]
    New -- not done --> Old{"old status?"}
    Old -- done --> Keep["Keep ALL old evidence<br/>(never downgrade)"]
    Old -- not done --> Empty["No evidence<br/>either side"]

    B --> PNew{"new pending<br/>uuid?"}
    PNew -- yes --> TakeNew["Take new"]
    PNew -- no --> TakeOld["Keep old"]
```

### `summarize_scan` — only metadata, never DOM

```mermaid
flowchart LR
    Payload["urlscan result<br/>payload"] --> Safe["EXTRACTED:<br/>page.title · page.url ·<br/>page.status · task.screenshotURL ·<br/>lists.domains · verdicts.overall"]
    Payload -.-x|"NEVER READ"| Danger["data.dom · data.cookies ·<br/>data.console · data.requests[].body"]
    style Danger fill:#fde2e2,stroke:#c0392b,stroke-width:2px
```

---

## 8. CI workflow (`urlscan-evidence`)

`.github/workflows/urlscan-evidence.yml` — 4x daily at 00:17, 06:17, 12:17,
18:17 UTC (every 6 hours), also manual. Commits `urlscan-evidence.json` to repo.
`contents: write`.

```mermaid
flowchart TD
    Trigger["schedule: 17 0,6,12,18 * * *<br/>(4x per hari)<br/>or workflow_dispatch"] --> Co{"another run<br/>in progress?"}
    Co -- yes --> Queue["queue — do NOT cancel<br/>(ledger must not race)"]
    Queue --> Co

    Co -- no --> Checkout["actions/checkout@v7"]
    Checkout --> Py["setup-python 3.13<br/>pip cache"]
    Py --> Deps["pip install requirements.txt pytest"]
    Deps --> Test["pytest -q"]

    Test -- fail --> Stop["job fails<br/>nothing committed"]
    Test -- ok --> Validate["Validate inputs<br/>limit, size, sleep,<br/>max_submit, visibility,<br/>poll_seconds"]

    Validate --> APICheck{"URLSCAN_API_KEY<br/>set?"}
    APICheck -- yes --> SetKey["export PROBE_API_KEY"]
    APICheck -- no --> NoKey["search only<br/>(submit-scan disabled)"]

    SetKey --> Probe
    NoKey --> Probe

    Probe["probe.py<br/>--submit-scan --max-submit N<br/>--merge-out urlscan-evidence.json"]

    Probe --> CommitStep["git add urlscan-evidence.json"]
    CommitStep --> Diff{"git diff<br/>cached empty?"}
    Diff -- yes --> Noop["no changes<br/>skip commit"]
    Diff -- no --> Commit["git commit<br/>'evidence: urlscan &lt;timestamp&gt;'"]
    Commit --> Rebase["git pull --rebase origin main"]
    Rebase --> Push["git push origin main"]

    Noop --> Upload
    Push --> Upload["upload-artifact<br/>urlscan-evidence.json<br/>probe-result.json"]
    Upload --> Done(["done"])

    classDef err fill:#fde2e2,stroke:#c0392b
    class Stop err
    classDef ok fill:#e8f8f5,stroke:#1abc9c
    class Noop,Done ok
```

Key properties:
- **Commit before rebase**: `git pull --rebase` refuses to run with uncommitted
  changes, so `git commit` happens first. The file we touch
  (`urlscan-evidence.json`) does not conflict with other workflows (`ct-grab`
  writes `domain.txt`, `ct.db`, `stats.json`).
- **`if: always()`** on the commit step: even if probe hits a quota wall, the
  partial ledger is committed so the next run can build on it.
- **Concurrency serialized** (`cancel-in-progress: false`): two runs never race
  on the same ledger.

---

## 9. CI workflow (`classify`)

`.github/workflows/classify.yml` — daily at 07:41 UTC, also manual.
Read-only: results go to artifacts, never committed (unreviewed LLM output).

```mermaid
flowchart TD
    Trigger["schedule: 41 7 * * *<br/>or workflow_dispatch"] --> Co{"another run<br/>in progress?"}
    Co -- yes --> Queue["queue"] --> Co

    Co -- no --> Checkout["actions/checkout@v7"]
    Checkout --> Py["setup-python 3.13"]
    Py --> Deps["pip install"]
    Deps --> Test["pytest -q"]

    Test -- fail --> Stop["job fails"]
    Test -- ok --> CheckKey{"GEMINI_API_KEY<br/>set?"}
    CheckKey -- no --> NoKey["::error::Secret belum diset<br/>exit 1"]
    CheckKey -- yes --> KeyOK

    KeyOK --> Seed["SEED = inputs.seed<br/>or day-of-year"]

    Seed --> Ledger{"urlscan-evidence.json<br/>exists in repo?"}

    Ledger -- yes --> UseLedger["EVIDENCE_ARG=<br/>--evidence urlscan-evidence.json<br/>(zero urlscan quota)"]
    Ledger -- no --> FallbackProbe["python probe.py<br/>--json-out hasil/probe.json"]

    FallbackProbe -- ok --> UseProbe["EVIDENCE_ARG=<br/>--evidence hasil/probe.json"]
    FallbackProbe -- fail --> NoEvidence["::warning::probe gagal<br/>classify tanpa bukti"]

    UseLedger --> Classify
    UseProbe --> Classify
    NoEvidence --> Classify

    Classify["python classify.py<br/>--domains domain.txt<br/>--limit N --seed SEED<br/>$EVIDENCE_ARG<br/>--out-dir hasil"]

    Classify --> Upload["upload-artifact<br/>blocklist.txt · whitelist.txt ·<br/>review.txt · classify-result.json"]
    Upload --> Done(["done"])

    classDef err fill:#fde2e2,stroke:#c0392b
    class Stop,NoKey err
    classDef ok fill:#e8f8f5,stroke:#1abc9c
    class Done ok
    classDef warn fill:#fff4d6,stroke:#d68910
    class NoEvidence warn
```

The critical design: `classify.yml` **prefers the committed ledger** over calling
urlscan.io itself. When `urlscan-evidence.json` exists (committed by the
`urlscan-evidence` workflow), the classify job uses it directly — zero urlscan
quota spent. The fallback probe runs only if the ledger doesn't exist yet
(first-ever run). Day-of-year seeding ensures different domains get classified
each day on scheduled runs.
