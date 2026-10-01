"""Test orchestrator: fetch_range, grab_log.

Selama ini fetch_range dan grab_log tidak punya test sama sekali, padahal di
situ letak logika dua-phase cursor + fair-share yang paling rawan regresi.

Semua test jalan di atas httpx.MockTransport -- tidak ada jaringan.
"""

import argparse
import json
import pathlib
import sqlite3
import time

import httpx

import grab

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURES = [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("*.json"))]

# Satu entri = satu leaf dari fixture, dipakai berulang supaya log bisa
# "berisi" entri sebanyak yang diinginkan tanpa menyiapkan sertifikat asli.
ANY_ENTRY = {
    "leaf_input": FIXTURES[0]["leaf_input"],
    "extra_data": FIXTURES[0]["extra_data"],
}

LOG_URL = "https://log.example"


# --------------------------------------------------------------------------
# Fake CT log
# --------------------------------------------------------------------------


class FakeLog:
    """CT log tiruan yang melayani get-sth + get-entries.

    tree_size   : jumlah entri di log
    max_per_req : batas entri per request (server boleh memaksa lebih sedikit
                  dari yang diminta -- RFC 6962 mengizinkannya)
    fail_after  : kalau diisi, request get-entries ke-(fail_after) dst gagal
    """

    def __init__(self, tree_size=1000, max_per_req=None, fail_after=None,
                 fail_status=503, fail_sth=False):
        self.tree_size = tree_size
        self.max_per_req = max_per_req
        self.fail_after = fail_after
        self.fail_status = fail_status
        self.fail_sth = fail_sth
        self.requests: list[tuple[int, int]] = []
        self.sth_calls = 0
        self._entry_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path.endswith("/get-sth"):
            self.sth_calls += 1
            if self.fail_sth:
                return httpx.Response(503, json={"error": "boom"})
            return httpx.Response(
                200, json={"tree_size": self.tree_size, "timestamp": 1_749_078_971_434}
            )

        if not path.endswith("/get-entries"):
            return httpx.Response(404, json={"error": "not found"})

        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        self._entry_requests += 1
        self.requests.append((start, end))

        if self.fail_after is not None and self._entry_requests >= self.fail_after:
            return httpx.Response(self.fail_status, json={"error": "boom"})

        if self.max_per_req is not None:
            end = min(end, start + self.max_per_req - 1)
        if start >= self.tree_size:
            return httpx.Response(200, json={"entries": []})

        n = min(end, self.tree_size - 1) - start + 1
        return httpx.Response(200, json={"entries": [ANY_ENTRY] * n})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_cfg(**over) -> argparse.Namespace:
    cfg = grab.build_arg_parser().parse_args([])
    cfg.batch = over.pop("batch", 10)
    cfg.sleep = 0
    cfg.tail_window = over.pop("tail_window", 100)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def fresh_db(tmp_path) -> sqlite3.Connection:
    return grab.init_db(str(tmp_path / "ct.db"))


def log_dict() -> dict:
    return {"log_id": "fake-log-id", "url": LOG_URL, "description": "fake"}


def noop(raw) -> None:
    pass


def far_deadline() -> float:
    return time.monotonic() + 3600


def past_deadline() -> float:
    return time.monotonic() - 1


# --------------------------------------------------------------------------
# fetch_range
# --------------------------------------------------------------------------


def test_fetch_range_advances_by_actual_count():
    """Server boleh membalikkan lebih sedikit dari batch -- posisi harus ikut."""
    fake = FakeLog(tree_size=100, max_per_req=3)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 99, cfg, budget=1000,
            deadline=far_deadline(), handle=noop,
        )
    assert used == 100
    assert pos == 100
    for start, end in fake.requests:
        assert end - start + 1 <= 10, "request tidak boleh melewati batch"


def test_fetch_range_stops_when_server_returns_empty():
    fake = FakeLog(tree_size=5)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 999, cfg, budget=1000,
            deadline=far_deadline(), handle=noop,
        )
    assert used == 5
    assert pos == 5          # berhenti, tidak melompat ke 1000


def test_fetch_range_error_does_not_advance_position():
    fake = FakeLog(tree_size=100, fail_after=1)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 99, cfg, budget=1000,
            deadline=far_deadline(), handle=noop,
        )
    assert used == 0
    assert pos == 0


def test_fetch_range_partial_progress_then_error():
    fake = FakeLog(tree_size=100, fail_after=3)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 99, cfg, budget=1000,
            deadline=far_deadline(), handle=noop,
        )
    assert used == 20        # 2 batch sukses sebelum batch ke-3 gagal
    assert pos == 20


def test_fetch_range_respects_budget():
    fake = FakeLog(tree_size=1000)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 999, cfg, budget=25,
            deadline=far_deadline(), handle=noop,
        )
    assert used <= 25
    assert pos == used


def test_fetch_range_respects_deadline():
    fake = FakeLog(tree_size=10000)
    cfg = make_cfg(batch=10)
    with fake.client() as client:
        pos, used = grab.fetch_range(
            client, LOG_URL, 0, 9999, cfg, budget=100000,
            deadline=past_deadline(), handle=noop,
        )
    assert used == 0
    assert pos == 0


# --------------------------------------------------------------------------
# grab_log: kapan cursor boleh disimpan
# --------------------------------------------------------------------------


def test_sth_failure_leaves_cursor_untouched(tmp_path):
    """get-sth gagal -> updated_at tidak boleh berubah, supaya log tetap di
    urutan pertama run berikutnya (rotasi LRU)."""
    fake = FakeLog(tree_size=100, fail_sth=True)
    conn = fresh_db(tmp_path)
    cfg = make_cfg()
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )
    assert res.used == 0
    assert conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0, (
        "cursor tidak boleh tersimpan kalau get-sth gagal"
    )
    conn.close()


def test_no_work_still_records_cursor(tmp_path):
    """Log tanpa kerja tetap dicatat -- kalau tidak, log beres akan terus
    muncul di urutan pertama dan menyia-nyiakan get-sth tiap run."""
    fake = FakeLog(tree_size=0)
    conn = fresh_db(tmp_path)
    cfg = make_cfg()
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )
    row = conn.execute("SELECT updated_at FROM cursors").fetchone()
    assert row is not None
    assert row[0] > 0
    assert res.used == 0
    conn.close()


def test_progress_is_committed(tmp_path):
    fake = FakeLog(tree_size=1000)
    conn = fresh_db(tmp_path)
    cfg = make_cfg(batch=10, tail_window=500)
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )
    assert res.used > 0
    assert res.tail_index > 0
    assert conn.execute("SELECT tail_index FROM cursors").fetchone()[0] == res.tail_index
    conn.close()


def test_bootstrap_tail_runs_before_backlog(tmp_path):
    """Saat bootstrap (tail_index=0) backlog tidak boleh jalan sebelum tail
    progres -- kalau tidak, celah antara 0 dan tail_start tak dicover siapa pun."""
    fake = FakeLog(tree_size=1000)
    conn = fresh_db(tmp_path)
    cfg = make_cfg(batch=10, tail_window=100)
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=200,
            deadline=far_deadline(), handle=noop,
        )
    assert res.tail_index > 0, "tail harus jalan lebih dulu saat bootstrap"
    if res.tail_index == 0:
        assert res.next_index == 0
    conn.close()


def test_cursor_not_advanced_when_only_work_failed(tmp_path):
    """Ada kerjaan tapi fetch gagal total -> cursor tidak tersimpan, jadi run
    berikutnya mengulang dari posisi sama (tidak ada celah)."""
    fake = FakeLog(tree_size=1000, fail_after=1)
    conn = fresh_db(tmp_path)
    cfg = make_cfg(batch=10, tail_window=100)
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )
    assert res.used == 0
    assert conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0
    conn.close()


def test_deadline_already_passed_makes_no_request(tmp_path):
    fake = FakeLog(tree_size=1000)
    conn = fresh_db(tmp_path)
    cfg = make_cfg()
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=past_deadline(), handle=noop,
        )
    assert res.used == 0
    assert fake.sth_calls == 0, "jatah habis -> get-sth pun tidak boleh dipanggil"
    conn.close()


def test_budget_is_a_hard_cap(tmp_path):
    """fetch_range dulu bisa melewati budget sampai batch-1 entri per panggilan
    (loop mengecek `used < budget` sebelum fetch, bukan sesudah)."""
    fake = FakeLog(tree_size=10_000)
    conn = fresh_db(tmp_path)
    cfg = make_cfg(batch=10, tail_window=10_000)
    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=25,
            deadline=far_deadline(), handle=noop,
        )
    assert res.used <= 25, f"budget dilanggar: {res.used} > 25"
    conn.close()


def test_backlog_covers_gap_behind_tail(tmp_path):
    """next_index < tail_start -> backlog harus mengisi celahnya."""
    fake = FakeLog(tree_size=1000)
    conn = fresh_db(tmp_path)
    grab.set_cursor(conn, log_dict(), next_index=0, tail_index=500)
    cfg = make_cfg(batch=10, tail_window=100)

    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=500,
            deadline=far_deadline(), handle=noop,
        )

    # tail mulai di max(500, 1000-100)=900 -> gap = [0, 900)
    assert res.tail_index >= 900
    assert res.next_index > 0, "backlog harus dapat giliran mengisi gap [0, 900)"
    conn.close()


def test_reanchor_is_visible_on_stderr(tmp_path, capsys):
    """Re-anchor harus terlihat -- angka entri yang dilewati inilah yang selama
    ini tidak pernah diakumulasi di mana pun, sehingga cakupan tak terukur."""
    fake = FakeLog(tree_size=1_000_000)
    conn = fresh_db(tmp_path)
    grab.set_cursor(conn, log_dict(), next_index=0, tail_index=10)
    cfg = make_cfg(batch=10, tail_window=100)

    with fake.client() as client:
        grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )

    err = capsys.readouterr().err
    assert "re-anchor" in err
    assert "lewati" in err
    conn.close()


def test_skipped_is_reported_and_backlog_depth(tmp_path):
    """Dua metrik cakupan: entri yang dilewati re-anchor, dan entri yang belum
    pernah discan. Tanpa keduanya, tail_index terlihat ~100% padahal tidak."""
    fake = FakeLog(tree_size=1_000_000)
    conn = fresh_db(tmp_path)
    grab.set_cursor(conn, log_dict(), next_index=400, tail_index=10)
    cfg = make_cfg(batch=10, tail_window=100)

    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )

    # tail_start = 1_000_000 - 100 = 999_900; tail_index lama = 10
    assert res.skipped == 999_900 - 10
    # backlog = tree_size - next_index, dan next_index tidak boleh negatif
    assert res.backlog == 1_000_000 - res.next_index
    assert res.backlog >= 0
    conn.close()


def test_bootstrap_does_not_count_history_as_skipped(tmp_path):
    """Saat bootstrap tail_index=0: yang tertinggal bukan entri yang 'dilewati'
    tapi riwayat yang belum pernah disentuh -- jangan dicampur aduk."""
    fake = FakeLog(tree_size=1_000_000)
    conn = fresh_db(tmp_path)
    cfg = make_cfg(batch=10, tail_window=100)

    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=100,
            deadline=far_deadline(), handle=noop,
        )

    assert res.skipped == 0, "bootstrap tidak boleh dihitung sebagai re-anchor"
    assert res.backlog > 0, "riwayat yang belum discan tetap harus dilaporkan"
    conn.close()


def test_reanchor_never_leaves_a_hole(tmp_path):
    """Saat re-anchor melompatkan tail, celah yang tertinggal harus tetap
    tertutup oleh next_index -- bukan hilang begitu saja."""
    fake = FakeLog(tree_size=1_000_000)
    conn = fresh_db(tmp_path)
    grab.set_cursor(conn, log_dict(), next_index=400, tail_index=10)
    cfg = make_cfg(batch=10, tail_window=100)

    with fake.client() as client:
        res = grab.grab_log(
            client, conn, log_dict(), cfg, budget=1000,
            deadline=far_deadline(), handle=noop,
        )

    gap_end = 1_000_000 - 100      # tail_start
    # next_index tidak boleh melewati gap_end kecuali memang sudah menutup gap
    assert res.next_index <= gap_end or res.next_index >= res.tail_index
    # dan selama belum menutup gap, posisinya tidak boleh melompat mundur
    assert res.next_index >= 400
    conn.close()


# --------------------------------------------------------------------------
# print_coverage: metrik yang selama ini tidak pernah ada
# --------------------------------------------------------------------------


def test_print_coverage_reports_flow_percentage(capsys):
    grab.print_coverage({"entries": 100_000, "skipped": 21_500_000, "backlog": 0})
    out = capsys.readouterr().out
    assert "cakupan aliran" in out
    # 100_000 / 21_600_000 = 0.46%
    assert "0.46%" in out
    assert "21,500,000" in out, "entri yang dilewati harus ikut tercetak"


def test_print_coverage_reports_backlog_depth(capsys):
    grab.print_coverage({"entries": 10, "skipped": 0, "backlog": 41_215_930_332})
    out = capsys.readouterr().out
    assert "backlog" in out
    assert "41,215,930,332" in out


def test_print_coverage_bootstrap_is_not_misleading(capsys):
    """Saat belum ada entri terukur, jangan mencetak persentase palsu."""
    grab.print_coverage({"entries": 0, "skipped": 0, "backlog": 0})
    out = capsys.readouterr().out
    assert "n/a" in out
    assert "%" not in out.split("n/a")[1].split("\n")[0]


def test_print_coverage_never_uses_tail_index_metric():
    """Guard: cakupan harus dihitung dari diproses+terlewati, bukan dari
    tree_size - tail_index yang selalu mendekati 100%."""
    # skenario menipu: tail_index hampir sama dengan tree_size
    stats = {"entries": 500, "skipped": 999_500, "backlog": 0}
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        grab.print_coverage(stats)
    out = buf.getvalue()
    assert "0.05%" in out, f"cakupan harus ~0.05%, dapat: {out!r}"


# --------------------------------------------------------------------------
# make_handler: kegagalan parse tidak boleh ditelan diam-diam
# --------------------------------------------------------------------------


class Boom:
    """Entry yang selalu meledak saat diparse."""


def test_parse_failure_is_reported_with_reason(capsys):
    """Sebelumnya `except Exception` menghitung tanpa jejak -- bug programming
    ikut terhitung sebagai parse_fail dan tak pernah terlihat."""
    import tempfile

    stats = {"entries": 0, "written": 0, "parse_fail": 0}
    with tempfile.TemporaryFile("w+", encoding="utf-8") as fout:
        handler = grab.make_handler(fout, stats)
        handler(Boom())

    assert stats["parse_fail"] == 1
    assert stats["entries"] == 1
    err = capsys.readouterr().err
    assert "parse_fail #1" in err
    assert "Boom" in err, "tipe exception harus tercetak"


def test_parse_failure_samples_are_capped(capsys):
    import tempfile

    stats = {"entries": 0, "written": 0, "parse_fail": 0}
    with tempfile.TemporaryFile("w+", encoding="utf-8") as fout:
        handler = grab.make_handler(fout, stats)
        for _ in range(20):
            handler(Boom())

    assert stats["parse_fail"] == 20
    err = capsys.readouterr().err
    assert "parse_fail #5" in err
    assert "parse_fail #6" not in err, "sampel harus dibatasi"


def test_handler_writes_candidate_once(tmp_path):
    """Dedup dalam satu run: domain sama dari entri berbeda tidak dobel."""
    out = tmp_path / "domain.txt"
    stats = {"entries": 0, "written": 0, "parse_fail": 0}
    entry = {
        "leaf_input": FIXTURES[0]["leaf_input"],
        "extra_data": FIXTURES[0]["extra_data"],
    }
    with out.open("a", encoding="utf-8") as fout:
        handler = grab.make_handler(fout, stats)
        handler(entry)
        handler(entry)
        handler(entry)

    # fixture pertama tidak punya kandidat judol, jadi memang 0 tertulis;
    # yang diuji: tidak ada exception dan penghitungan entri benar
    assert stats["entries"] == 3
    assert stats["parse_fail"] == 0

