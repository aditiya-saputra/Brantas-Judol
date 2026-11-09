"""Generate stats.json from ct.db and domain.txt."""
import argparse, json, sqlite3, os

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="ct.db")
    p.add_argument("--domains", default="domain.txt")
    p.add_argument("--out", default="stats.json")
    a = p.parse_args()

    con = sqlite3.connect(a.db)
    ct_entries, ct_logs, last_updated = con.execute(
        "SELECT COALESCE(SUM(tail_index),0), COUNT(*), COALESCE(MAX(updated_at),0) FROM cursors"
    ).fetchone()
    con.close()

    with open(a.domains) as f:
        domains_listed = sum(1 for _ in f)

    with open(a.out, "w") as f:
        json.dump({
            "ct_entries_scanned": ct_entries,
            "domains_listed": domains_listed,
            "ct_logs_monitored": ct_logs,
            "last_updated": last_updated,
        }, f, indent=2)
    print(f"stats.json: {ct_entries} entries, {domains_listed} domains, {ct_logs} logs")

if __name__ == "__main__":
    main()
