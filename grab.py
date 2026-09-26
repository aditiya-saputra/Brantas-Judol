#!/usr/bin/env python3
"""CT Grabber: Certificate Transparency -> domain.txt.

Alur: registry log list (Google/Apple) -> get-sth -> get-entries ->
parse TLS wire format RFC 6962 -> ekstrak domain (SAN + CN) ->
filter keyword -> tulis domain.txt.

Cursor hybrid disimpan di SQLite (ct.db):
  * tail_index  -> posisi entri terbaru yang sudah diproses (sinyal utama)
  * next_index  -> posisi backlog (scan dari awal log, sisanya budget run)
"""

from __future__ import annotations

import argparse
import base64
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

VERSION = "0.1"
USER_AGENT = f"ct-grabber/{VERSION} (+mailto:you@example.com)"

LOG_LIST_URLS = (
    "https://www.gstatic.com/ct/log_list/v3/log_list.json",
    "https://valid.apple.com/ct/log_list/current_log_list.json",
)

STRONG_KEYWORDS = (
    "slot",
    "togel",
    "gacor",
    "maxwin",
    "judi",
    "casino",
    "jackpot",
)
KEYWORD_PATTERN = re.compile("|".join(map(re.escape, STRONG_KEYWORDS)), re.IGNORECASE)

# "rtp" cuma 3 huruf dan gampang menempel di tengah kata ("digicertp",
# "mazortp" -> ratusan FP dari host Salesforce/AWS), jadi dia hanya dihitung
# kalau tidak diawali huruf. Kata panjang di atas tetap substring murni
# supaya "superslot" / "gacorslot" tetap ketangkep.
RTP_PATTERN = re.compile(r"rtp", re.IGNORECASE)

LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"{LABEL}(?:\.{LABEL})+")

DEFAULT_BATCH = 256
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_SECONDS = 1200.0
DEFAULT_TAIL_WINDOW = 100_000
DEFAULT_SLEEP = 0.25

# Jatah waktu minimum per log. Di bawah ini get-sth saja sudah habis jatahnya
# tanpa sempat 1 batch, jadi lebih baik berhenti: log yang belum tersentuh
# diprioritaskan run berikutnya.
MIN_SLICE_SECONDS = 3.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# TLS presentation language (RFC 5246 section 4)
# --------------------------------------------------------------------------


class TLSReader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError(
                f"truncated TLS vector: want {n} bytes at offset {self.pos}, "
                f"have {len(self.data) - self.pos}"
            )
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self._take(2), "big")

    def u24(self) -> int:
        return int.from_bytes(self._take(3), "big")

    def u64(self) -> int:
        return int.from_bytes(self._take(8), "big")

    def fixed(self, n: int) -> bytes:
        return self._take(n)

    def var16(self) -> bytes:
        return self._take(self.u16())

    def var24(self) -> bytes:
        return self._take(self.u24())


# --------------------------------------------------------------------------
# Leaf / extra_data (RFC 6962 section 3.1-3.2)
# --------------------------------------------------------------------------


@dataclass
class Leaf:
    version: int
    leaf_type: int
    timestamp: int
    entry_type: int
    cert_der: bytes = b""
    tbs_der: bytes = b""
    extensions: bytes = b""


def parse_leaf(leaf_input: bytes) -> Leaf:
    r = TLSReader(leaf_input)
    version = r.u8()
    leaf_type = r.u8()
    timestamp = r.u64()
    entry_type = r.u16()

    if entry_type == 0:
        cert_der = r.var24()
        tbs_der = b""
    elif entry_type == 1:
        r.fixed(32)
        cert_der = b""
        tbs_der = r.var24()
    else:
        raise ValueError(f"unknown entry_type: {entry_type}")

    extensions = r.var16()
    return Leaf(version, leaf_type, timestamp, entry_type, cert_der, tbs_der, extensions)


def parse_extra(extra_data: bytes) -> tuple[bytes, list[bytes]]:
    """PrecertChainEntry: pre_certificate (DER penuh) + vector<ASN.1Cert>."""
    r = TLSReader(extra_data)
    pre_certificate = r.var24()
    chain_blob = r.var24()

    chain: list[bytes] = []
    cr = TLSReader(chain_blob)
    while cr.pos < len(chain_blob):
        chain.append(cr.var24())
    return pre_certificate, chain


# --------------------------------------------------------------------------
# Domain extraction & keyword filter
# --------------------------------------------------------------------------


def normalize_domain(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    d = raw.strip().lower().rstrip(".")
    while d.startswith("*."):
        d = d[2:]
    if not d or len(d) > 253:
        return None
    if any(ch.isspace() for ch in d):
        return None
    if not d.isascii():
        try:
            d = d.encode("idna").decode("ascii")
        except (UnicodeError, LookupError):
            return None
    if not DOMAIN_RE.fullmatch(d):
        return None
    if all(label.isdigit() for label in d.split(".")):
        return None
    return d


def domains_from_cert(der: bytes) -> set[str]:
    if not der:
        return set()
    cert = x509.load_der_x509_certificate(der)
    out: set[str] = set()

    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        san = None
    if san is not None:
        for name in san.get_values_for_type(x509.DNSName):
            d = normalize_domain(name)
            if d:
                out.add(d)

    for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        d = normalize_domain(str(attr.value))
        if d:
            out.add(d)
    return out


def domains_from_entry(entry: dict) -> set[str]:
    leaf = parse_leaf(base64.b64decode(entry["leaf_input"]))
    if leaf.entry_type == 0:
        return domains_from_cert(leaf.cert_der)
    if leaf.entry_type == 1:
        extra = base64.b64decode(entry.get("extra_data") or "")
        pre_certificate, _chain = parse_extra(extra)
        return domains_from_cert(pre_certificate)
    return set()


def is_candidate(domain: str) -> bool:
    if KEYWORD_PATTERN.search(domain):
        return True
    for match in RTP_PATTERN.finditer(domain):
        start = match.start()
        if start == 0 or not domain[start - 1].isalpha():
            return True
    return False


# --------------------------------------------------------------------------
# Log list (registry resmi)
# --------------------------------------------------------------------------


def _get_json(
    client: httpx.Client,
    url: str,
    params: Optional[dict] = None,
    retries: int = 3,
) -> dict:
    delay = 1.0
    last: Optional[BaseException] = None

    for attempt in range(retries):
        if attempt:
            time.sleep(min(60.0, delay))
            delay *= 4.0
        try:
            resp = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
            continue

        if resp.status_code in RETRYABLE_STATUS:
            last = FetchError(f"HTTP {resp.status_code}")
            retry_after = resp.headers.get("retry-after", "")
            if retry_after.strip().isdigit():
                delay = max(1.0, float(retry_after))
            continue
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except ValueError as exc:
            last = exc
            continue

    raise FetchError(f"giving up on {url}: {last}")


def load_logs(
    client: httpx.Client,
    log_list_urls: tuple[str, ...] = LOG_LIST_URLS,
) -> list[dict]:
    """Ambil log dengan state 'usable', dedup berdasarkan log_id."""
    logs: dict[str, dict] = {}
    for url in log_list_urls:
        try:
            data = _get_json(client, url)
        except (FetchError, httpx.HTTPError) as exc:
            print(f"[log-list] skip {url}: {exc}", file=sys.stderr)
            continue
        for operator in data.get("operators", []):
            for log in operator.get("logs", []):
                if "usable" not in (log.get("state") or {}):
                    continue
                log_id = log.get("log_id") or ""
                log_url = (log.get("url") or "").rstrip("/")
                if not log_id or not log_url or log_id in logs:
                    continue
                logs[log_id] = {
                    "log_id": log_id,
                    "url": log_url,
                    "description": log.get("description", ""),
                }
    return list(logs.values())


# --------------------------------------------------------------------------
# Cursor state (SQLite)
# --------------------------------------------------------------------------


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cursors (
            log_id     TEXT PRIMARY KEY,
            url        TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            next_index INTEGER NOT NULL DEFAULT 0,
            tail_index INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cursors)")}
    for name, ddl in (
        ("url", "TEXT NOT NULL DEFAULT ''"),
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("next_index", "INTEGER NOT NULL DEFAULT 0"),
        ("tail_index", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE cursors ADD COLUMN {name} {ddl}")
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection, log_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT next_index, tail_index FROM cursors WHERE log_id=?", (log_id,)
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def set_cursor(
    conn: sqlite3.Connection,
    log: dict,
    next_index: int,
    tail_index: int,
) -> None:
    conn.execute(
        """
        INSERT INTO cursors (log_id, url, description, next_index, tail_index, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(log_id) DO UPDATE SET
            url=excluded.url,
            description=excluded.description,
            next_index=excluded.next_index,
            tail_index=excluded.tail_index,
            updated_at=excluded.updated_at
        """,
        (
            log["log_id"],
            log["url"],
            log["description"],
            next_index,
            tail_index,
            int(time.time()),
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Fetch loop
# --------------------------------------------------------------------------


def fetch_range(
    client: httpx.Client,
    log_url: str,
    start: int,
    stop: int,
    cfg: argparse.Namespace,
    budget: int,
    deadline: float,
    handle: Callable[[dict], None],
) -> tuple[int, int]:
    """Fetch entri [start, stop] (inklusif). Kembalikan (posisi, jumlah)."""
    pos = start
    used = 0
    while pos <= stop and used < budget and time.monotonic() < deadline:
        end = min(pos + cfg.batch - 1, stop)
        try:
            payload = _get_json(
                client, f"{log_url}/ct/v1/get-entries", {"start": pos, "end": end}
            )
        except (FetchError, httpx.HTTPError) as exc:
            print(f"      fetch error @{pos}: {exc}", file=sys.stderr)
            break

        entries = payload.get("entries") or []
        if not entries:
            break

        for raw in entries:
            handle(raw)
        pos += len(entries)
        used += len(entries)

        if cfg.sleep > 0:
            time.sleep(cfg.sleep)
    return pos, used


def grab_log(
    client: httpx.Client,
    conn: sqlite3.Connection,
    log: dict,
    cfg: argparse.Namespace,
    budget: int,
    deadline: float,
    handle: Callable[[dict], None],
) -> tuple[int, int, int]:
    """Satu log: phase tail dulu (entri terbaru), lalu backlog dengan sisa budget."""
    log_url = log["url"]

    # Jatah log ini sudah habis: jangan buang get-sth, dan jangan sentuh
    # updated_at supaya log ini tetap di urutan pertama run berikutnya.
    if time.monotonic() >= deadline or budget <= 0:
        return 0, *get_cursor(conn, log["log_id"])

    try:
        sth = _get_json(client, f"{log_url}/ct/v1/get-sth")
    except (FetchError, httpx.HTTPError) as exc:
        print(f"      sth error: {exc}", file=sys.stderr)
        return 0, *get_cursor(conn, log["log_id"])

    tree_size = int(sth.get("tree_size") or 0)
    next_index, tail_index = get_cursor(conn, log["log_id"])
    if tail_index > tree_size:
        tail_index = 0
    if next_index > tree_size:
        next_index = tree_size

    if time.monotonic() >= deadline or budget <= 0:
        # get-sth tadi saja sudah menghabiskan jatah: skip fetch, jangan
        # sentuh updated_at (log ini tetap di urutan pertama run berikutnya)
        return 0, next_index, tail_index

    used = 0

    # --- phase 1: tail -----------------------------------------------------
    # Jatah segar = yang paling baru dulu. desired dibatasi window DAN budget
    # log ini, supaya kalau budget < window kita mulai dari UJUNG tree
    # (entri terbaru), bukan dari awal window yang sudah basi.
    desired = min(cfg.tail_window, budget)
    tail_start = max(tail_index, tree_size - desired)
    tail_start = max(0, min(tail_start, tree_size))
    if tail_index > 0 and tail_start > tail_index:
        print(
            f"      re-anchor tail: lewati {tail_start - tail_index} entri "
            f"(lag > jatah {desired})",
            file=sys.stderr,
        )

    gap_end = tail_start
    had_work = tail_start < tree_size or next_index < gap_end

    if tail_start < tree_size:
        pos, n = fetch_range(
            client, log_url, tail_start, tree_size - 1, cfg, budget, deadline, handle
        )
        if pos > tail_start:
            # hanya majukan cursor kalau benar-benar ada progres, supaya fetch
            # gagal di tengah tidak membuat lompatan celah
            tail_index = pos
            used += n

    # --- phase 2: backlog (celah antara next_index dan tail) ---------------
    # Backlog hanya jalan kalau tail sudah punya progres (tail_index > 0),
    # supaya tidak ada celah yang tidak dicover siapa pun saat bootstrap.
    tail_ok = tail_index > 0 or tail_start >= tree_size
    if next_index >= gap_end:
        next_index = max(next_index, tail_index)
    elif tail_ok and used < budget and time.monotonic() < deadline:
        pos, n = fetch_range(
            client, log_url, next_index, gap_end - 1, cfg, budget - used, deadline, handle
        )
        next_index = pos
        used += n
        if next_index >= gap_end:
            next_index = max(next_index, tail_index)

    # Cursor (dan updated_at untuk urutan rotasi) hanya disimpan kalau ada
    # progres atau memang tidak ada kerjaan; kalau ada kerjaan tapi fetch
    # gagal, log ini tetap di urutan pertama run berikutnya.
    if used > 0 or not had_work:
        set_cursor(conn, log, next_index, tail_index)

    return used, next_index, tail_index


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------


def make_handler(fout, stats: dict) -> Callable[[dict], None]:
    seen: set[str] = set()

    def handle(raw: dict) -> None:
        stats["entries"] += 1
        try:
            domains = domains_from_entry(raw)
        except Exception:
            stats["parse_fail"] += 1
            return
        for domain in domains:
            if domain in seen:
                continue
            seen.add(domain)
            if is_candidate(domain):
                fout.write(domain + "\n")
                fout.flush()
                stats["written"] += 1

    return handle


def run(cfg: argparse.Namespace) -> int:
    started = time.monotonic()
    deadline = started + cfg.max_seconds
    stats = {"entries": 0, "written": 0, "parse_fail": 0}

    client = httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    conn = init_db(cfg.db)

    try:
        if cfg.log_url:
            logs = [
                {
                    "log_id": f"custom:{cfg.log_url.rstrip('/')}",
                    "url": cfg.log_url.rstrip("/"),
                    "description": "custom --log-url",
                }
            ]
        else:
            logs = load_logs(client)
            if not logs:
                print("[fatal] tidak ada log usable dari registry", file=sys.stderr)
                return 1

        stamps = {
            row[0]: int(row[1] or 0)
            for row in conn.execute("SELECT log_id, updated_at FROM cursors")
        }
        logs.sort(key=lambda item: stamps.get(item["log_id"], 0))

        print(
            f"logs: {len(logs)} | budget: {cfg.max_entries} entri | "
            f"limit: {cfg.max_seconds:.0f}s | tail-window: {cfg.tail_window}"
        )

        budget = cfg.max_entries
        total_logs = len(logs)
        with open(cfg.out, "a", encoding="utf-8") as fout:
            handler = make_handler(fout, stats)
            for position, log in enumerate(logs, 1):
                now = time.monotonic()
                if budget <= 0 or now >= deadline:
                    print(f"  budget/habis waktu, {total_logs - position + 1} log dilewati")
                    break

                # Fair-share dinamis: jatah = sisa / sisa-log. Log yang cepat
                # (sudah ketemu / sedikit entri baru) mengembalikan jatahnya
                # ke log berikutnya lewat perhitungan ulang ini.
                remaining_logs = total_logs - position + 1
                time_slice = (deadline - now) / remaining_logs
                budget_slice = max(1, budget // remaining_logs)
                if position > 1 and time_slice < MIN_SLICE_SECONDS:
                    # Sisa waktu tak cukup untuk get-sth + 1 batch. Berhenti:
                    # log yang belum tersentuh tetap di urutan depan run
                    # berikutnya (updated_at tidak berubah).
                    print(
                        f"  sisa waktu {deadline - now:.0f}s terlalu kecil "
                        f"untuk {remaining_logs} log tersisa, berhenti"
                    )
                    break
                log_deadline = min(deadline, now + time_slice)

                label = log["description"] or log["url"]
                used, next_index, tail_index = grab_log(
                    client, conn, log, cfg, budget_slice, log_deadline, handler
                )
                budget -= used
                print(
                    f"[{position}/{total_logs}] {label}: +{used} entri, "
                    f"tail={tail_index}, backlog={next_index}"
                )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        conn.close()
        client.close()

    elapsed = time.monotonic() - started
    print(
        f"selesai: entri={stats['entries']} kandidat={stats['written']} "
        f"gagal_parse={stats['parse_fail']} waktu={elapsed:.1f}s"
    )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grab", description="CT grabber: log list -> domain.txt"
    )
    parser.add_argument("--db", default="ct.db", help="path SQLite cursor (default: ct.db)")
    parser.add_argument("--out", default="domain.txt", help="output (default: domain.txt)")
    parser.add_argument(
        "--max-entries", type=int, default=DEFAULT_MAX_ENTRIES,
        help="budget total entri per run (default: 100000)",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
        help="batas waktu run dalam detik (default: 1200)",
    )
    parser.add_argument(
        "--tail-window", type=int, default=DEFAULT_TAIL_WINDOW,
        help="entri terakhir yang diambil saat bootstrap cursor (default: 100000)",
    )
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="entri per request")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="jeda antar batch")
    parser.add_argument("--log-url", help="ambil satu log saja (debug)")
    parser.add_argument("--list-logs", action="store_true", help="tampilkan log usable lalu keluar")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    cfg = build_arg_parser().parse_args(argv)
    cfg.batch = max(1, min(cfg.batch, 4096))
    cfg.max_entries = max(1, cfg.max_entries)
    cfg.tail_window = max(1, cfg.tail_window)

    if cfg.list_logs:
        client = httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            for log in load_logs(client):
                print(f"{log['log_id']}\t{log['description']}\t{log['url']}")
        finally:
            client.close()
        return 0

    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
