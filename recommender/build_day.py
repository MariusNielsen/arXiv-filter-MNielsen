#!/usr/bin/env python3
"""Assemble the digest page's record for one day.

    python recommender/build_day.py DATA_JSON RECS_JSON OUT_JSON

DATA_JSON  docs/data/<day>.json written by the daily GitHub run (all classified papers)
RECS_JSON  {"recommended": [{"pid": "...", "score": 0.0-1.0, "reason": "..."}], "note": "optional"}
OUT_JSON   document body for days/<day> in the digest page's store

The page shows tiers 1-5 plus every recommended paper; everything else is left out.
The body is kept under the store's 256 KiB document limit by trimming abstracts.
"""
import datetime as dt
import json
import sys

LIMIT = 240_000
KEEP = ("pid", "title", "abstract", "authors", "categories", "link", "pdf", "source",
        "announce", "ai_mode", "other_links", "tier", "reasons", "watched_authors")


def main(data_path, recs_path, out_path):
    data = json.load(open(data_path))
    recs_in = json.load(open(recs_path))
    by_id = {p["pid"]: p for p in data["papers"]}

    recs, seen = [], set()
    for r in recs_in.get("recommended", []):
        pid = r.get("pid")
        if pid in by_id and pid not in seen:
            seen.add(pid)
            recs.append({"pid": pid, "score": round(float(r.get("score", 0)), 2),
                         "reason": str(r.get("reason", "")).strip()[:300]})
    unknown = [r.get("pid") for r in recs_in.get("recommended", []) if r.get("pid") not in by_id]

    shown = [p for p in data["papers"] if p.get("tier", 9) <= 5 or p["pid"] in seen]
    papers = [{k: p.get(k) for k in KEEP} for p in shown]
    body = {
        "day": data["day"],
        "source_updated": data.get("updated"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "scanned": len(data["papers"]),
        "papers": papers,
        "recommended": recs,
    }
    if recs_in.get("note"):
        body["note"] = str(recs_in["note"])[:500]

    # stay under the document size limit: trim abstracts, lowest tiers first
    for cap in (2500, 1200, 600, 0):
        if len(json.dumps(body, ensure_ascii=False).encode()) <= LIMIT:
            break
        for p in sorted(papers, key=lambda p: -(p.get("tier") or 9)):
            if p["pid"] not in seen and p.get("abstract") and len(p["abstract"]) > cap:
                p["abstract"] = p["abstract"][:cap].rstrip() + ("…" if cap else "")

    json.dump(body, open(out_path, "w"), ensure_ascii=False)
    size = len(json.dumps(body, ensure_ascii=False).encode())
    print(f"{data['day']}: {len(papers)} papers shown of {len(data['papers'])} scanned, "
          f"{len(recs)} recommended, {size} bytes" + (f"; unknown pids ignored: {unknown}" if unknown else ""))


if __name__ == "__main__":
    main(*sys.argv[1:4])
