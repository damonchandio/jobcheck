"""How old are UK AI and data job ads when a job hunter sees them? Run over career-ops scan history.

    python3 evals/uk_ai_ads.py path/to/scan-history.tsv [--json out.json]

Age = first_seen (the day the ad was found live) minus posted_at (the board's own first-published date).
Repost = the same employer and title live under 2+ different URLs.
"""
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date

UK = re.compile(r"\b(united kingdom|uk|england|scotland|wales|northern ireland|london|manchester|leeds|bradford|"
                r"birmingham|bristol|edinburgh|glasgow|cambridge|oxford|cardiff|belfast|sheffield|newcastle|"
                r"nottingham|liverpool|reading|york|brighton|remote \(uk\)|uk remote)\b", re.I)
ROLE = re.compile(r"\b(ai|a\.i\.|ml|machine learning|data scien\w*|data engineer\w*|llm|nlp|computer vision|"
                  r"deep learning|mlops|applied scientist|research scientist|research engineer|analytics engineer|"
                  r"data analyst|genai|generative)\b", re.I)


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def analyse(rows):
    ads = {}
    for r in rows:
        if not (r.get("posted_at") and r.get("first_seen") and r.get("url")):
            continue
        if not UK.search(r.get("location", "")) or not ROLE.search(r.get("title", "")):
            continue
        try:
            age = (date.fromisoformat(r["first_seen"][:10]) - date.fromisoformat(r["posted_at"][:10])).days
        except ValueError:
            continue
        if age < 0 or age > 3650:
            continue
        ads.setdefault(r["url"], {"age": age, "company": r.get("company", ""), "title": r.get("title", ""),
                                  "portal": r.get("portal", ""), "key": (r.get("normalized_company") or r.get("company", "")).lower()
                                  + "|" + re.sub(r"\W+", " ", r.get("title", "").lower()).strip()})
    ages = sorted(a["age"] for a in ads.values())
    if not ages:
        return {"ads": 0}
    by_key = defaultdict(set)
    for u, a in ads.items():
        by_key[a["key"]].add(u)
    reposted = {k: v for k, v in by_key.items() if len(v) > 1}
    n = len(ages)
    pct = lambda d: round(100 * sum(1 for x in ages if x >= d) / n, 1)
    oldest = sorted(ads.values(), key=lambda a: -a["age"])[:10]
    return {
        "ads": n, "employers": len({a["company"] for a in ads.values()}),
        "median_age_days": statistics.median(ages),
        "pct_30_plus": pct(30), "pct_60_plus": pct(60), "pct_90_plus": pct(90), "pct_180_plus": pct(180),
        "reposted_roles": len(reposted), "ads_in_reposted_roles": sum(len(v) for v in reposted.values()),
        "oldest": [{"age": a["age"], "company": a["company"], "title": a["title"]} for a in oldest],
        "portals": dict(sorted(((p, sum(1 for a in ads.values() if a["portal"] == p))
                                for p in {a["portal"] for a in ads.values()}), key=lambda x: -x[1])),
        "seen_between": [min(r["first_seen"][:10] for r in rows if r.get("first_seen")),
                         max(r["first_seen"][:10] for r in rows if r.get("first_seen"))],
    }


if __name__ == "__main__":
    res = analyse(load(sys.argv[1]))
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump(res, f, indent=2)
    print(json.dumps(res, indent=2))
