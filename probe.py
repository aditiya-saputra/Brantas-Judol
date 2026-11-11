#!/usr/bin/env python3
"""Probe kandidat di domain.txt lewat urlscan.io (read-only).

Tahap probing belum dibangun. Skrip ini adalah langkah pertama yang sengaja
dibatasi: hanya mencari apakah sebuah domain PERNAH discan urlscan.io, lalu
mencetak ringkasannya. Tidak ada klasifikasi, tidak ada blocklist, tidak ada
commit -- hasilnya cuma log Actions.

Kenapa terpisah dari grab.py dan tidak dipanggil CI terjadwal:
  * Kandidat di domain.txt ~97 ribu dan urlscan.io punya kuota; menyapu
    semuanya per run tidak realistis.
  * ct-grab.yml memegang `contents: write` + `git push`. Menambah panggilan
    API pihak ketiga di sana menambah mode gagal baru pada job yang mengubah
    state.

Endpoint terverifikasi 2026-11-10:
  GET https://urlscan.io/api/v1/search/?q=domain:<domain>&size=<n>
  -> 200 OK tanpa API key, kunci: results, total, has_more, took.
  Rate limit dan batas `size` TIDAK dipublikasikan, jadi keduanya dibuat
  konservatif dan bisa diatur lewat flag.

Hasil scan adalah BUKTI, bukan vonis. Domain yang punya scan tidak otomatis
situs judol.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from typing import Optional

import httpx

VERSION = "0.1"
USER_AGENT = f"brantas-judol-probe/{VERSION} (+mailto:aditiya.saputrax1x2@gmail.com)"

SEARCH_URL = "https://urlscan.io/api/v1/search/"

# Kuota urlscan tidak dipublikasikan. Default dibuat kecil dan jarak antar
# request lebar supaya run pertama tidak langsung kena limit.
DEFAULT_LIMIT = 25
DEFAULT_SIZE = 1
DEFAULT_SLEEP = 6.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ProbeError(RuntimeError):
    pass


class RateLimited(ProbeError):
    pass


def load_candidates(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def pick_sample(domains: list[str], limit: int, seed: int) -> list[str]:
    """Ambil sampel deterministik supaya run yang sama menghasilkan himpunan sama."""
    if limit >= len(domains):
        return list(domains)
    rng = random.Random(seed)
    return rng.sample(domains, limit)


def _fetch_results(
    client: httpx.Client,
    domain: str,
    size: int,
    retries: int = 3,
) -> dict:
    """Satu pencarian ke urlscan.io. Kembalikan payload mentah."""
    params = {"q": f"domain:{domain}", "size": size}
    delay = 10.0
    last: Optional[str] = None

    for attempt in range(retries):
        if attempt:
            time.sleep(delay)
            delay *= 2.0
        try:
            resp = client.get(SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue

        if resp.status_code in RETRYABLE_STATUS:
            last = f"HTTP {resp.status_code}"
            retry_after = resp.headers.get("retry-after", "").strip()
            if retry_after.isdigit():
                delay = max(1.0, float(retry_after))
            continue
        if resp.status_code >= 400:
            raise ProbeError(f"HTTP {resp.status_code} for {domain}")

        try:
            payload = resp.json()
        except ValueError:
            last = "respons bukan JSON"
            continue
        if not isinstance(payload, dict):
            last = "respons bukan objek JSON"
            continue
        return payload

    raise RateLimited(f"menyerah pada {domain}: {last}")


def search_scans(
    client: httpx.Client,
    domain: str,
    size: int,
    retries: int = 3,
) -> list[dict]:
    """Kembalikan daftar scan ringkas untuk satu domain (bisa kosong)."""
    payload = _fetch_results(client, domain, size, retries)
    out: list[dict] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "uuid": item.get("_id") or "",
                "url": (item.get("page") or {}).get("url") or "",
                "scanned": (item.get("task") or {}).get("time") or "",
            }
        )
    return [hit for hit in out if hit["uuid"]]


def probe(
    client: httpx.Client,
    domain: str,
    size: int,
    retries: int = 3,
) -> dict:
    """Cari scan untuk satu domain. Kembalikan dict ringkas."""
    payload = _fetch_results(client, domain, size, retries)
    total = int(payload.get("total") or 0)
    results = payload.get("results") or []
    top = results[0] if results else {}
    return {
        "domain": domain,
        "total": total,
        "has_more": bool(payload.get("has_more")),
        "first_uuid": top.get("_id") or "",
        "first_url": (top.get("page") or {}).get("url") or "",
        "first_scanned": (top.get("task") or {}).get("time") or "",
    }


def summarize(rows: list[dict]) -> str:
    hit = [r for r in rows if r["total"] > 0]
    lines = [
        f"diperiksa      : {len(rows)} domain",
        f"punya scan     : {len(hit)}",
        f"tanpa scan     : {len(rows) - len(hit)}",
        "",
    ]
    for r in hit:
        lines.append(f"[{r['total']:>4} scan] {r['domain']}")
        if r["first_uuid"]:
            lines.append(f"             https://urlscan.io/result/{r['first_uuid']}/")
        if r["first_url"]:
            lines.append(f"             {r['first_url']}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe",
        description="Cari kandidat domain.txt di urlscan.io (read-only)",
    )
    parser.add_argument("--domains", default="domain.txt", help="daftar kandidat")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"jumlah domain yang diperiksa (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        help=f"scan per domain yang diminta (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help=f"jeda antar request detik (default: {DEFAULT_SLEEP})",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed sampel")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="berhenti setelah N request gagal berturut-turut (default: 3)",
    )
    parser.add_argument("--json-out", help="simpan hasil sebagai JSON (opsional)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    args.limit = max(1, args.limit)
    args.size = max(1, args.size)

    try:
        domains = load_candidates(args.domains)
    except OSError as exc:
        print(f"[fatal] gagal membaca {args.domains}: {exc}", file=sys.stderr)
        return 1
    if not domains:
        print(f"[fatal] {args.domains} kosong", file=sys.stderr)
        return 1

    sample = pick_sample(domains, args.limit, args.seed)
    print(
        f"kandidat: {len(domains)} | diperiksa: {len(sample)} | "
        f"size: {args.size} | jeda: {args.sleep:.0f}s"
    )

    rows: list[dict] = []
    failures = 0
    headers = {"User-Agent": USER_AGENT}
    api_key = os.environ.get("PROBE_API_KEY", "").strip()
    if api_key:
        # Endpoint publik tetap jalan tanpa key, tapi kuota anonim kecil.
        headers["API-Key"] = api_key
    client = httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers=headers,
    )
    try:
        for index, domain in enumerate(sample, 1):
            try:
                # Satu request saja per domain. `probe()` dan `search_scans()`
                # memakai `_fetch_results()` yang sama, jadi memanggil keduanya
                # berarti dua request untuk kuota yang sama-sama terbatas.
                hits = search_scans(client, domain, args.size)
                row = {
                    "domain": domain,
                    "total": len(hits),
                    "has_more": False,
                    "first_uuid": hits[0]["uuid"] if hits else "",
                    "first_url": hits[0]["url"] if hits else "",
                    "first_scanned": hits[0]["scanned"] if hits else "",
                    "uuids": [h["uuid"] for h in hits],
                }
            except ProbeError as exc:
                failures += 1
                print(f"  [{index}/{len(sample)}] {domain}: {exc}", file=sys.stderr)
                if failures >= args.max_failures:
                    print(
                        f"[berhenti] {failures} kegagalan berturut-turut",
                        file=sys.stderr,
                    )
                    break
                continue
            failures = 0
            rows.append(row)
            status = f"{row['total']} scan" if row["total"] else "tidak ada scan"
            print(f"  [{index}/{len(sample)}] {domain}: {status}")
            if index < len(sample) and args.sleep > 0:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        client.close()

    print()
    print(summarize(rows))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, ensure_ascii=False)
        print(f"\nhasil JSON: {args.json_out}")

    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
