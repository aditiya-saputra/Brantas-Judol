import sqlite3

import grab


def test_cursor_roundtrip(tmp_path):
    db = tmp_path / "ct.db"
    conn = grab.init_db(str(db))

    assert grab.get_cursor(conn, "missing") == (0, 0)

    log = {"log_id": "abc", "url": "https://log.example/", "description": "test"}
    grab.set_cursor(conn, log, next_index=100, tail_index=5000)
    assert grab.get_cursor(conn, "abc") == (100, 5000)

    grab.set_cursor(conn, log, next_index=200, tail_index=6000)
    assert grab.get_cursor(conn, "abc") == (200, 6000)

    row = conn.execute("SELECT url, description FROM cursors").fetchone()
    assert row == ("https://log.example/", "test")
    conn.close()


def test_init_db_is_idempotent(tmp_path):
    db = str(tmp_path / "ct.db")
    first = grab.init_db(db)
    first.close()
    second = grab.init_db(db)
    second.close()


def test_init_db_migrates_minimal_schema(tmp_path):
    db = tmp_path / "ct.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE cursors (log_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO cursors (log_id) VALUES ('old')")
    conn.commit()
    conn.close()

    conn = grab.init_db(str(db))
    assert grab.get_cursor(conn, "old") == (0, 0)
    grab.set_cursor(
        conn,
        {"log_id": "old", "url": "u", "description": "d"},
        next_index=7,
        tail_index=9,
    )
    assert grab.get_cursor(conn, "old") == (7, 9)
    conn.close()


def test_is_candidate_false_on_empty_and_noise():
    assert grab.is_candidate("") is False
    assert grab.is_candidate("example.com") is False
