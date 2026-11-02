"""kb.py init | stats | search "query" [--after YYYY-MM-DD] [--source NAME] [--limit N] [--json]

Runs locally on the server, or remotely when KB_URL and KB_TOKEN are set (any computer)."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def remote(path, params):
    url = os.environ["KB_URL"].rstrip("/") + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["KB_TOKEN"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def show(res, as_json):
    if as_json:
        print(json.dumps(res, indent=2))
        return
    print(res["header"])
    for i, r in enumerate(res["results"], 1):
        print(f"\n{i}. {r['ts'][:10]}  [{r['source']}/{r['kind']}]  {r['title']}")
        print(f"   {(r['snippet'] or '').replace(chr(10), ' ')[:300]}")
        if r["pointer"]:
            print(f"   -> {r['pointer']}")
    if not res["results"]:
        print("\nNo matches. Try different, more distinctive words.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sp = sub.add_parser("stats")
    sp.add_argument("--json", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--after")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if os.environ.get("KB_URL") and a.cmd != "init":
        if a.cmd == "stats":
            print(json.dumps(remote("/stats", {}), indent=2))
        else:
            show(remote("/search", {"q": a.query, "after": a.after, "source": a.source, "limit": a.limit}), a.json)
        return

    import db
    con = db.connect()
    db.init(con)
    if a.cmd == "init":
        print(f"Initialized {db.DB_PATH}")
    elif a.cmd == "stats":
        st = db.stats(con)
        if a.json:
            print(json.dumps(st, indent=2))
            return
        print(db.header(con))
        for r in st["by_source"]:
            print(f"  {r['source']:12} {r['kind']:8} {r['n']:7}  {r['first'][:10]} .. {r['last'][:10]}")
        for e in st["recent_errors"]:
            print(f"  error {e['ts']} {e['source']}: {e['detail'][:200]}")
    else:
        res = {"header": db.header(con),
               "results": db.search(con, a.query, a.after, a.source, a.limit)}
        show(res, a.json)


if __name__ == "__main__":
    main()
