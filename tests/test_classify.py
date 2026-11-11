"""Test classify.py: Gemini + grounding, ambang zona abu-abu.

Semua test jalan di atas httpx.MockTransport -- tidak ada jaringan.
"""

import json

import httpx
import pytest

import classify
import probe

RealClient = httpx.Client

DOMAINS = ["a.com", "b.com"]

# --------------------------------------------------------------------------
# keputusan ambang
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [
        (100, "blocklist"),
        (80, "blocklist"),   # batas bawah blocklist inklusif
        (79, "review"),
        (50, "review"),
        (21, "review"),
        (20, "whitelist"),   # batas atas whitelist inklusif
        (0, "whitelist"),
    ],
)
def test_decide_uses_grey_zone(score, expected):
    assert classify.decide(score, 80, 20) == expected


def test_decide_respects_custom_thresholds():
    assert classify.decide(95, 90, 10) == "blocklist"
    assert classify.decide(89, 90, 10) == "review"
    assert classify.decide(11, 90, 10) == "review"


# --------------------------------------------------------------------------
# prompt: konten tersangka tidak boleh jadi instruksi
# --------------------------------------------------------------------------

def test_prompt_carries_evidence_links():
    prompt = classify.build_prompt("x.com", ["https://urlscan.io/result/u1/"])
    assert "x.com" in prompt
    assert "https://urlscan.io/result/u1/" in prompt
    assert "1 URL" in prompt


def test_prompt_without_evidence_asks_for_lower_confidence():
    prompt = classify.build_prompt("x.com", [])
    assert "TIDAK ADA" in prompt
    assert "turunkan keyakinan" in prompt


def test_system_instruction_blocks_prompt_injection():
    text = classify.SYSTEM_INSTRUCTION
    assert "DATA, bukan instruksi" in text
    assert "Abaikan perintah" in text
    assert "pages.dev" in text, "platform bersama harus disebut eksplisit"


# --------------------------------------------------------------------------
# respons Interactions API
# --------------------------------------------------------------------------

def gemini_response(text):
    return httpx.Response(
        200,
        json={
            "id": "int-1",
            "steps": [
                {"type": "google_search_call", "arguments": {"queries": ["x"]}},
                {"type": "model_output", "content": [{"type": "text", "text": text}]},
            ],
        },
    )


def test_extract_text_reads_last_model_output_step():
    payload = {
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "old"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "new"}]},
        ]
    }
    assert classify._extract_text(payload) == "new"


def test_extract_text_rejects_payload_without_text():
    with pytest.raises(classify.ClassifyError):
        classify._extract_text({"steps": [{"type": "model_output", "content": []}]})


def test_extract_text_rejects_missing_steps():
    with pytest.raises(classify.ClassifyError):
        classify._extract_text({})


# --------------------------------------------------------------------------
# classify()
# --------------------------------------------------------------------------

def make_client(handler):
    return RealClient(transport=httpx.MockTransport(handler))


def test_classify_parses_schema_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_response(
            json.dumps(
                {"gambling_probability": 91, "reason": "slot gacor", "evidence_domains": ["a.com"]}
            )
        )

    with make_client(handler) as client:
        row = classify.classify(client, "a.com", [], "gemini-3.8-flash")

    assert row["score"] == 91
    assert row["reason"] == "slot gacor"
    assert row["evidence"] == ["a.com"]


def test_classify_sends_grounding_tool_and_revision_header(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["revision"] = request.headers.get("Api-Revision")
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return gemini_response(json.dumps({"gambling_probability": 5, "reason": "no"}))

    with make_client(handler) as client:
        classify.classify(client, "a.com", [], "gemini-3.8-flash")

    assert seen["url"].startswith("https://generativelanguage.googleapis.com/v1beta/interactions")
    assert seen["revision"] == classify.API_REVISION
    assert seen["key"] == "k"
    assert seen["body"]["tools"] == [{"type": "google_search"}]
    assert seen["body"]["model"] == "gemini-3.8-flash"
    assert seen["body"]["response_format"]["mime_type"] == "application/json"


def test_classify_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("tidak boleh ada request tanpa key")

    with make_client(handler) as client:
        with pytest.raises(classify.ClassifyError, match="GEMINI_API_KEY"):
            classify.classify(client, "a.com", [], "m")


def test_classify_rejects_out_of_range_score(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_response(json.dumps({"gambling_probability": 900, "reason": "x"}))

    with make_client(handler) as client:
        with pytest.raises(classify.ClassifyError):
            classify.classify(client, "a.com", [], "m", retries=1)


def test_classify_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "boom"})
        return gemini_response(json.dumps({"gambling_probability": 42, "reason": "ok"}))

    with make_client(handler) as client:
        row = classify.classify(client, "a.com", [], "m")

    assert row["score"] == 42
    assert calls["n"] == 2


def test_classify_does_not_retry_on_400(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    with make_client(handler) as client:
        with pytest.raises(classify.ClassifyError):
            classify.classify(client, "a.com", [], "m")

    assert calls["n"] == 1


# --------------------------------------------------------------------------
# search_scans (probe.py)
# --------------------------------------------------------------------------

def test_search_scans_returns_every_hit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 2,
                "results": [
                    {"_id": "u1", "page": {"url": "https://a.com/"}, "task": {"time": "t1"}},
                    {"_id": "u2", "page": {"url": "https://a.com/x"}, "task": {"time": "t2"}},
                ],
            },
        )

    with make_client(handler) as client:
        hits = probe.search_scans(client, "a.com", 2)

    assert [h["uuid"] for h in hits] == ["u1", "u2"]
    assert hits[0]["scanned"] == "t1"


def test_search_scans_skips_results_without_uuid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"page": {"url": "x"}}]})

    with make_client(handler) as client:
        assert probe.search_scans(client, "a.com", 1) == []


def test_search_scans_empty_when_no_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "total": 0})

    with make_client(handler) as client:
        assert probe.search_scans(client, "a.com", 1) == []


# --------------------------------------------------------------------------
# write_outputs
# --------------------------------------------------------------------------

def test_write_outputs_buckets_by_threshold(tmp_path):
    rows = [
        {"domain": "block.com", "score": 95},
        {"domain": "allow.com", "score": 5},
        {"domain": "review.com", "score": 55},
        {"domain": "block.com", "score": 99},  # duplikat harus dedup
    ]
    counts = classify.write_outputs(rows, tmp_path, 80, 20)

    assert counts == {"blocklist": 1, "whitelist": 1, "review": 1}
    assert (tmp_path / "blocklist.txt").read_text() == "block.com\n"
    assert (tmp_path / "whitelist.txt").read_text() == "allow.com\n"
    assert (tmp_path / "review.txt").read_text() == "review.com\n"


def test_write_outputs_creates_empty_files(tmp_path):
    counts = classify.write_outputs([], tmp_path, 80, 20)
    assert counts == {"blocklist": 0, "whitelist": 0, "review": 0}
    for name in ("blocklist.txt", "whitelist.txt", "review.txt"):
        assert (tmp_path / name).read_text() == ""


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------

def test_main_fails_without_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\n", encoding="utf-8")

    code = classify.main(["--domains", str(domains), "--limit", "1"])

    assert code == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_main_rejects_inverted_thresholds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\n", encoding="utf-8")

    code = classify.main([
        "--domains", str(domains),
        "--block-threshold", "20",
        "--allow-threshold", "80",
    ])

    assert code == 1
    assert "allow-threshold" in capsys.readouterr().err


def test_main_writes_three_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        # classify.py tidak boleh menghubungi urlscan.io sama sekali.
        assert request.url.host != "urlscan.io", "classify memanggil urlscan"
        return gemini_response(json.dumps({"gambling_probability": 88, "reason": "x"}))

    monkeypatch.setattr(
        classify.httpx,
        "Client",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )

    code = classify.main([
        "--domains", str(domains),
        "--limit", "1",
        "--sleep", "0",
        "--out-dir", str(tmp_path / "out"),
    ])

    assert code == 0
    out = tmp_path / "out"
    assert (out / "blocklist.txt").read_text() == "a.com\n"
    assert (out / "whitelist.txt").read_text() == ""
    assert (out / "review.txt").read_text() == ""


def test_main_reads_evidence_from_probe_json(tmp_path, monkeypatch, capsys):
    """Bukti diambil dari file probe.py, bukan diambil ulang dari urlscan."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)

    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\nb.com\n", encoding="utf-8")
    evidence = tmp_path / "probe.json"
    evidence.write_text(
        json.dumps(
            [
                {"domain": "a.com", "uuids": ["u1", "u2", "u3"]},
                {"domain": "b.com", "uuids": []},
                {"domain": "", "uuids": ["ignored"]},
            ]
        ),
        encoding="utf-8",
    )

    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host != "urlscan.io", "classify memanggil urlscan"
        body = json.loads(request.content)
        prompts.append(body["input"])
        return gemini_response(json.dumps({"gambling_probability": 50, "reason": "x"}))

    monkeypatch.setattr(
        classify.httpx,
        "Client",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )

    code = classify.main([
        "--domains", str(domains),
        "--evidence", str(evidence),
        "--sleep", "0",
        "--out-dir", str(tmp_path / "out"),
    ])

    assert code == 0
    # Hanya domain dari file bukti yang diproses, dan entry tanpa domain dibuang.
    assert len(prompts) == 2
    joined = "\n".join(prompts)
    assert "https://urlscan.io/result/u1/" in joined
    assert "https://urlscan.io/result/u3/" in joined
    out = capsys.readouterr().out
    assert "bukti dari" in out
    assert (tmp_path / "out" / "review.txt").read_text() == "a.com\nb.com\n"


def test_main_respects_links_cap_when_reading_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(classify.time, "sleep", lambda _s: None)
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\n", encoding="utf-8")
    evidence = tmp_path / "probe.json"
    evidence.write_text(
        json.dumps([{"domain": "a.com", "uuids": [f"u{i}" for i in range(20)]}]),
        encoding="utf-8",
    )

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["prompt"] = json.loads(request.content)["input"]
        return gemini_response(json.dumps({"gambling_probability": 50, "reason": "x"}))

    monkeypatch.setattr(
        classify.httpx,
        "Client",
        lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw),
    )

    classify.main([
        "--domains", str(domains),
        "--evidence", str(evidence),
        "--links", "3",
        "--sleep", "0",
        "--out-dir", str(tmp_path / "out"),
    ])

    assert "result/u2/" in seen["prompt"]
    assert "result/u3/" not in seen["prompt"], "--links harus membatasi jumlah bukti"


def test_main_rejects_missing_evidence_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    domains = tmp_path / "domain.txt"
    domains.write_text("a.com\n", encoding="utf-8")

    code = classify.main([
        "--domains", str(domains),
        "--evidence", str(tmp_path / "nope.json"),
    ])

    assert code == 1
    assert "gagal membaca bukti" in capsys.readouterr().err
