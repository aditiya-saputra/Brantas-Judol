"""Test probe.py: urlscan.io read-only search.

Semua test jalan di atas httpx.MockTransport -- tidak ada jaringan.
"""

import json
import time

import httpx
import pytest

import probe

DOMAINS = ["rtp-a.com", "rtp-b.com", "rtp-c.com", "rtp-d.com"]

# Beberapa test mengganti probe.httpx.Client. Karena probe.httpx IS modul
# httpx, patch itu juga terlihat dari sini -- simpan referensi aslinya.
RealClient = httpx.Client


class FakeUrlscan:
    """urlscan.io tiruan.

    hits      : domain yang dianggap punya scan
    fail_after: request ke-(fail_after) dst gagal
    """

    def __init__(self, hits=(), fail_after=None, status=503):
        self.hits = set(hits)
        self.fail_after = fail_after
        self.status = status
        self.queries: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        domain = request.url.params["q"].split("domain:", 1)[-1]
        self.queries.append(domain)

        if self.fail_after is not None and len(self.queries) >= self.fail_after:
            return httpx.Response(self.status, json={"message": "boom"})

        if domain in self.hits:
            uuid = f"uuid-{domain}"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "_id": uuid,
                            "page": {"url": f"https://{domain}/"},
                            "task": {"time": "2026-11-10T00:00:00.000Z"},
                        }
                    ],
                    "total": 1,
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={"results": [], "total": 0, "has_more": False})

    def client(self) -> httpx.Client:
        # RealClient disimpan sebelum patch supaya helper ini tidak menangkap
        # kembali Client yang sudah di-patch oleh test.
        return RealClient(transport=httpx.MockTransport(self.handler))


# --------------------------------------------------------------------------
# pemilihan sampel
# --------------------------------------------------------------------------

def test_sample_is_deterministic_for_a_seed():
    first = probe.pick_sample(DOMAINS, limit=2, seed=7)
    second = probe.pick_sample(DOMAINS, limit=2, seed=7)
    assert first == second
    assert len(first) == 2


def test_sample_returns_everything_when_limit_exceeds_input():
    assert probe.pick_sample(DOMAINS, limit=99, seed=0) == DOMAINS


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------

def test_probe_reports_hit_with_result_url(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    fake = FakeUrlscan(hits={"rtp-a.com"})
    with fake.client() as client:
        row = probe.probe(client, "rtp-a.com", size=1)

    assert row["total"] == 1
    assert row["first_uuid"] == "uuid-rtp-a.com"
    assert row["first_url"] == "https://rtp-a.com/"


def test_probe_reports_miss(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    fake = FakeUrlscan()
    with fake.client() as client:
        row = probe.probe(client, "rtp-b.com", size=1)

    assert row["total"] == 0
    assert row["first_uuid"] == ""


def test_probe_retries_then_raises_on_persistent_503(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    fake = FakeUrlscan(fail_after=1)
    with fake.client() as client:
        with pytest.raises(probe.RateLimited):
            probe.probe(client, "rtp-c.com", size=1, retries=3)

    assert len(fake.queries) == 3, "harus dicoba sebanyak `retries`"


def test_probe_recovers_after_transient_failure(monkeypatch):
    """Request pertama gagal, kedua berhasil -> tidak boleh menyerah."""
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"message": "boom"})
        return httpx.Response(
            200, json={"results": [], "total": 0, "has_more": False}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        row = probe.probe(client, "rtp-d.com", size=1, retries=3)

    assert row["total"] == 0
    assert calls["n"] == 2


def test_probe_does_not_retry_on_client_error(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"message": "bad request"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(probe.ProbeError):
            probe.probe(client, "rtp-a.com", size=1, retries=3)

    assert calls["n"] == 1, "4xx bukan kondisi yang layak diulang"


def test_probe_sends_domain_query_and_size(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["q"] = request.url.params["q"]
        seen["size"] = request.url.params["size"]
        return httpx.Response(
            200, json={"results": [], "total": 0, "has_more": False}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        probe.probe(client, "rtp-a.com", size=5)

    assert seen["path"] == "/api/v1/search/"
    assert seen["q"] == "domain:rtp-a.com"
    assert seen["size"] == "5"


# --------------------------------------------------------------------------
# ringkasan
# --------------------------------------------------------------------------

def test_summarize_lists_only_hits_with_result_page():
    rows = [
        {"domain": "hit.com", "total": 3, "has_more": True,
         "first_uuid": "abc", "first_url": "https://hit.com/", "first_scanned": ""},
        {"domain": "miss.com", "total": 0, "has_more": False,
         "first_uuid": "", "first_url": "", "first_scanned": ""},
    ]
    text = probe.summarize(rows)

    assert "diperiksa      : 2 domain" in text
    assert "punya scan     : 1" in text
    assert "hit.com" in text
    assert "https://urlscan.io/result/abc/" in text
    assert "miss.com" not in text


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------

def test_main_writes_json_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    domains = tmp_path / "domain.txt"
    domains.write_text("rtp-a.com\nrtp-b.com\n", encoding="utf-8")
    out = tmp_path / "out.json"

    fake = FakeUrlscan(hits={"rtp-a.com"})
    monkeypatch.setattr(
        probe.httpx, "Client", lambda **kw: fake.client()
    )

    code = probe.main([
        "--domains", str(domains),
        "--limit", "2",
        "--sleep", "0",
        "--json-out", str(out),
    ])

    assert code == 0
    payload = json.loads(out.read_text())
    assert {row["domain"] for row in payload} == {"rtp-a.com", "rtp-b.com"}
    assert "diperiksa" in capsys.readouterr().out


def test_main_stops_after_max_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\nb.com\nc.com\nd.com\n", encoding="utf-8")

    fake = FakeUrlscan(fail_after=1)
    monkeypatch.setattr(probe.httpx, "Client", lambda **kw: fake.client())

    code = probe.main([
        "--domains", str(domains),
        "--limit", "4",
        "--sleep", "0",
        "--max-failures", "2",
        "--seed", "0",
    ])

    # Tidak ada baris sukses -> exit 1. Setiap request diulang `retries` kali,
    # jadi hentikan-setelah-2-kegagalan = dua domain uji, bukan empat.
    assert code == 1
    assert len(set(fake.queries)) == 2, f"harus berhenti di 2 domain: {fake.queries}"
    assert len(fake.queries) == 6, "2 domain x 3 percobaan"


def test_main_rejects_missing_domains_file(tmp_path, capsys):
    code = probe.main(["--domains", str(tmp_path / "nope.txt"), "--limit", "1"])
    assert code == 1
    assert "fatal" in capsys.readouterr().err


def test_main_rejects_empty_domains_file(tmp_path, capsys):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    code = probe.main(["--domains", str(empty), "--limit", "1"])
    assert code == 1
    assert "kosong" in capsys.readouterr().err


def test_main_sends_api_key_header_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    monkeypatch.setenv("PROBE_API_KEY", "secret-123")
    domains = tmp_path / "domain.txt"
    domains.write_text("rtp-a.com\n", encoding="utf-8")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api-key"] = request.headers.get("API-Key")
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(
            200, json={"results": [], "total": 0, "has_more": False}
        )

    monkeypatch.setattr(
        probe.httpx,
        "Client",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )

    probe.main(["--domains", str(domains), "--limit", "1", "--sleep", "0"])

    assert seen["api-key"] == "secret-123"
    assert "brantas-judol-probe" in seen["ua"]


def test_main_omits_api_key_header_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _s: None)
    monkeypatch.delenv("PROBE_API_KEY", raising=False)
    domains = tmp_path / "domain.txt"
    domains.write_text("rtp-a.com\n", encoding="utf-8")

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api-key"] = request.headers.get("API-Key")
        return httpx.Response(
            200, json={"results": [], "total": 0, "has_more": False}
        )

    monkeypatch.setattr(
        probe.httpx,
        "Client",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )

    probe.main(["--domains", str(domains), "--limit", "1", "--sleep", "0"])

    assert seen["api-key"] is None
