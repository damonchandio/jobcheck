#!/usr/bin/env python3
"""jobcheck: facts about a job ad before you apply. Python standard library only.

    jobcheck.py <url | file.html | ->  [--employer NAME] [--postcode PC] [--domain D] [--json] [--card out.html]

Signals, most deterministic first:
  posting   datePosted / validThrough from the schema.org JobPosting most boards embed
  employer  Companies House: active? overdue filings? how old?  (free key in COMPANIES_HOUSE_API_KEY)
  domain    registration date (RDAP) and whether the domain can receive mail (MX)
  contact   reply addresses on free webmail
  wording   fee, bank details, chat-app-only contact, "always hiring"
  repost    how many times this exact ad has been seen before (local ledger)
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) jobcheck/0.1 (+https://github.com/damonchandio/jobcheck)"
CH = "https://api.company-information.service.gov.uk"
LEDGER = os.path.expanduser("~/.jobcheck.db")
FREE_MAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "yahoo.com",
             "yahoo.co.uk", "icloud.com", "aol.com", "protonmail.com", "proton.me", "live.com", "live.co.uk"}
SCAM = [  # (pattern, weight, label)
    (r"\b(registration|training|admin|processing|application)\s+fee\b", 3, "asks for a fee"),
    (r"\b(bank|card|account)\s+(details|number)\b.{0,60}\b(before|prior|to start|to apply)", 3, "wants bank details before you start"),
    (r"\b(whatsapp|telegram|signal)\b.{0,40}\b(only|contact|apply|message)", 2, "contact through a chat app only"),
    (r"\b(crypto|bitcoin|usdt)\b.{0,40}\b(pay|paid|payment|salary)", 2, "paid in crypto"),
    (r"\b(no (interview|experience) (needed|required|necessary))\b", 1, "no interview or experience needed"),
    (r"\b(always|continuously|constantly)\s+(hiring|recruiting|accepting)\b", 1, "says it is always hiring"),
    (r"\b(talent (pool|pipeline|community)|future (opportunities|openings|roles))\b", 1, "talent-pool wording"),
    (r"\b(earn|make)\s+(up to\s+)?[£$€]\s?\d{3,}.{0,12}\b(per|a)\s+(day|week)\b", 1, "unrealistic pay claim"),
]
AGE_RE = re.compile(r"\b(?:re)?posted\s+(\d{1,3})\+?\s+days?\s+ago\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", re.I)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def get_json(url: str, auth: str | None = None, timeout: int = 20):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{auth}:".encode()).decode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


# ---------- parsing ----------
def job_posting(page: str) -> dict:
    """The schema.org JobPosting object embedded in the page, or {}."""
    for m in re.finditer(r'<script[^>]+ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(html.unescape(m.group(1)).strip())
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return d
            stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            if "@graph" in d:
                stack.extend(d["@graph"])
    return {}


def page_text(page: str) -> str:
    page = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def plain_name(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"\b(the|limited|ltd|llp|plc|uk|co|company|group|holdings)\b", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def parse_date(s) -> dt.date | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


# ---------- signals ----------
def posting_signals(jp: dict, text: str, today: dt.date) -> list[dict]:
    out = []
    posted = parse_date(jp.get("datePosted"))
    m = AGE_RE.search(text)
    age = (today - posted).days if posted else (int(m.group(1)) if m else None)
    if age is not None:
        src = "schema.org datePosted" if posted else "'posted N days ago' on the page"
        out.append({"signal": "posting_age", "value": age, "bad": age >= 60,
                    "note": f"open for {age} days ({src})"})
    until = parse_date(jp.get("validThrough"))
    if until:
        out.append({"signal": "expired", "value": until.isoformat(), "bad": until < today,
                    "note": f"closing date {until.isoformat()}" + (" has passed" if until < today else "")})
    if not posted and not m:
        out.append({"signal": "posting_age", "value": None, "bad": False, "note": "no posting date found"})
    return out


def companies_house(name: str, postcode: str, key: str | None) -> dict:
    """One exact active match on Companies House, or why not."""
    if not key:
        return {"signal": "employer", "value": "skipped", "bad": False,
                "note": "Companies House check skipped (set COMPANIES_HOUSE_API_KEY, free)"}
    if not name:
        return {"signal": "employer", "value": "unknown", "bad": False, "note": "no employer name found"}
    q = urllib.parse.urlencode({"q": name, "items_per_page": 20})
    res = get_json(f"{CH}/search/companies?{q}", auth=key) or {}
    want = plain_name(name)
    hits = [i for i in res.get("items", []) if plain_name(i.get("title", "")) == want]
    pc = (postcode or "").upper().replace(" ", "")
    if pc and len(hits) > 1:
        hits = [i for i in hits if ((i.get("address") or {}).get("postal_code") or "").upper().replace(" ", "")[:len(pc) - 3] == pc[:-3]]
    if not hits:
        return {"signal": "employer", "value": "not found", "bad": True,
                "note": f"no company called '{name}' on Companies House (trading name, agency, or not a UK company?)"}
    active = [h for h in hits if h.get("company_status") == "active"]
    if len(active) > 1:
        return {"signal": "employer", "value": "ambiguous", "bad": False,
                "note": f"{len(active)} active companies called '{name}'; pass --postcode to narrow"}
    hit = active[0] if active else hits[0]
    p = get_json(f"{CH}/company/{hit['company_number']}", auth=key) or hit
    status = p.get("company_status", "unknown")
    since = parse_date(p.get("date_of_creation"))
    overdue = [k for k in ("accounts", "confirmation_statement") if (p.get(k) or {}).get("overdue")]
    bad = status != "active" or bool(overdue)
    note = f"{p.get('company_name', hit.get('title'))} ({hit['company_number']}) is {status}"
    if since:
        note += f", incorporated {since.isoformat()}"
    if overdue:
        note += ", overdue: " + ", ".join(k.replace("_", " ") for k in overdue)
    return {"signal": "employer", "value": status, "bad": bad, "note": note,
            "evidence": f"https://find-and-update.company-information.service.gov.uk/company/{hit['company_number']}"}


def domain_signals(domain: str, today: dt.date) -> list[dict]:
    if not domain:
        return []
    out = []
    rd = get_json(f"https://rdap.org/domain/{domain}") or {}
    reg = next((parse_date(e.get("eventDate")) for e in rd.get("events", []) if e.get("eventAction") == "registration"), None)
    if reg:
        age = (today - reg).days
        out.append({"signal": "domain_age", "value": age, "bad": age < 180,
                    "note": f"{domain} registered {reg.isoformat()} ({age} days ago)"})
    else:
        out.append({"signal": "domain_age", "value": None, "bad": False, "note": f"no registration date for {domain}"})
    mx = get_json(f"https://dns.google/resolve?{urllib.parse.urlencode({'name': domain, 'type': 'MX'})}") or {}
    has_mx = any(a.get("type") == 15 for a in mx.get("Answer", []))
    out.append({"signal": "mail", "value": has_mx, "bad": not has_mx and bool(mx),
                "note": f"{domain} {'can' if has_mx else 'cannot'} receive email"})
    return out


def text_signals(text: str, employer_domain: str) -> list[dict]:
    out = []
    free = sorted({d.lower() for d in EMAIL_RE.findall(text) if d.lower() in FREE_MAIL})
    if free:
        out.append({"signal": "contact", "value": free, "bad": True, "note": "reply address on " + ", ".join(free)})
    for pat, w, label in SCAM:
        if re.search(pat, text, re.I):
            out.append({"signal": "wording", "value": w, "bad": True, "note": label})
    return out


def ledger(key: str, url: str, today: dt.date, path: str = LEDGER) -> dict:
    db = sqlite3.connect(path)
    db.execute("create table if not exists seen (k text primary key, first text, last text, n int, url text)")
    row = db.execute("select first, n from seen where k=?", (key,)).fetchone()
    if row:
        db.execute("update seen set last=?, n=n+1 where k=?", (today.isoformat(), key))
        first, n = row[0], row[1] + 1
    else:
        db.execute("insert into seen values (?,?,?,?,?)", (key, today.isoformat(), today.isoformat(), 1, url))
        first, n = today.isoformat(), 1
    db.commit()
    db.close()
    return {"signal": "repost", "value": n, "bad": n >= 3,
            "note": f"seen {n} time{'s' if n != 1 else ''} since {first}" if n > 1 else "first time this ad has been seen here"}


# ---------- verdict ----------
def verdict(signals: list[dict]) -> tuple[str, str]:
    s = {x["signal"]: x for x in signals}
    scam = sum(x["value"] for x in signals if x["signal"] == "wording")
    if scam >= 3 or (s.get("employer", {}).get("value") in ("dissolved", "liquidation") and scam):
        return "SCAM", "high" if scam >= 3 else "medium"
    if s.get("employer", {}).get("value") in ("dissolved", "liquidation"):
        return "GHOST", "high"
    age = s.get("posting_age", {}).get("value")
    if s.get("expired", {}).get("bad") or (age is not None and age >= 60) or s.get("repost", {}).get("bad"):
        return "GHOST", "high" if s.get("employer", {}).get("value") != "active" else "medium"
    emp = s.get("employer", {}).get("value")
    if emp == "active" and not s["employer"]["bad"] and scam == 0 and (age is None or age < 30):
        return "REAL", "high" if age is not None else "medium"
    if emp == "not found" or s.get("contact", {}).get("bad") or scam:
        return "SUSPECT", "medium"
    return "UNVERIFIED", "low"


# ---------- card ----------
CARD = """<!doctype html><meta charset="utf-8"><title>jobcheck: {verdict}</title>
<style>body{{margin:0;background:#0a1420;font-family:Inter,system-ui,sans-serif;color:#fff}}
.c{{max-width:640px;margin:40px auto;padding:40px;border-left:10px solid {col};background:#0f1c2b}}
h1{{font-size:64px;margin:0;color:{col}}}h2{{font-weight:500;color:#b7c6d6;margin:8px 0 28px}}
ul{{padding:0;list-style:none}}li{{margin:10px 0;line-height:1.4}}.bad{{color:#ff6b6b}}.ok{{color:#5fd0ff}}
small{{color:#7e93a8;display:block;margin-top:28px;font-family:monospace}}</style>
<div class="c"><h1>{verdict}</h1><h2>{title} at {employer}</h2><ul>{items}</ul>
<small>jobcheck · {date} · github.com/damonchandio/jobcheck</small></div>"""


def card(result: dict) -> str:
    col = {"REAL": "#2ecc71", "GHOST": "#f1c40f", "SCAM": "#e74c3c"}.get(result["verdict"], "#95a5a6")
    items = "".join(f'<li class="{"bad" if s["bad"] else "ok"}">{"✗" if s["bad"] else "✓"} {html.escape(s["note"])}</li>'
                    for s in result["signals"])
    return CARD.format(verdict=result["verdict"], col=col, title=html.escape(result["title"] or "this role"),
                       employer=html.escape(result["employer"] or "unknown employer"), items=items, date=result["checked"])


# ---------- main ----------
def check(source: str, employer: str = "", postcode: str = "", domain: str = "", key: str | None = None,
          today: dt.date | None = None, ledger_path: str | None = LEDGER) -> dict:
    today = today or dt.date.today()
    if source == "-":
        page, url = sys.stdin.read(), ""
    elif re.match(r"https?://", source):
        page, url = fetch(source), source
    else:
        with open(source, encoding="utf-8", errors="replace") as f:
            page, url = f.read(), ""
    jp = job_posting(page)
    text = page_text(page) if "<" in page else page
    org = jp.get("hiringOrganization") or {}
    employer = employer or (org.get("name") if isinstance(org, dict) else str(org)) or ""
    loc = (jp.get("jobLocation") or {})
    loc = loc[0] if isinstance(loc, list) and loc else loc
    postcode = postcode or ((loc.get("address") or {}).get("postalCode") if isinstance(loc, dict) else "") or ""
    if not domain:
        site = org.get("sameAs") or org.get("url") if isinstance(org, dict) else ""
        if isinstance(site, list):
            site = site[0] if site else ""
        domain = urllib.parse.urlparse(site).netloc.removeprefix("www.") if site else ""
    signals = posting_signals(jp, text, today)
    signals.append(companies_house(employer, postcode, key if key is not None else os.environ.get("COMPANIES_HOUSE_API_KEY")))
    signals += domain_signals(domain, today)
    signals += text_signals(text, domain)
    if ledger_path:
        desc = page_text(str(jp.get("description", ""))) or text
        k = hashlib.sha1((plain_name(employer) + re.sub(r"\W+", "", desc.lower())[:2000]).encode()).hexdigest()
        signals.append(ledger(k, url, today, ledger_path))
    v, conf = verdict(signals)
    return {"verdict": v, "confidence": conf, "title": jp.get("title", ""), "employer": employer, "url": url,
            "checked": today.isoformat(), "signals": signals,
            "evidence": [s["evidence"] for s in signals if s.get("evidence")]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Is this job ad real? Facts, not a vibe score.")
    ap.add_argument("source", help="job URL, a saved .html file, or - for text on stdin")
    ap.add_argument("--employer", default="", help="employer name if the page doesn't say")
    ap.add_argument("--postcode", default="", help="employer postcode, to pick between same-name companies")
    ap.add_argument("--domain", default="", help="employer website domain")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--card", metavar="FILE", help="write a shareable HTML verdict card")
    ap.add_argument("--no-ledger", action="store_true", help="don't record this ad in ~/.jobcheck.db")
    a = ap.parse_args(argv)
    try:
        r = check(a.source, a.employer, a.postcode, a.domain, ledger_path=None if a.no_ledger else LEDGER)
    except urllib.error.HTTPError as e:
        print(f"Could not fetch the page (HTTP {e.code}). If it needs a login, save or paste the page and run:\n"
              f"  jobcheck.py - < page.txt --employer 'Name'", file=sys.stderr)
        return 2
    if a.card:
        with open(a.card, "w", encoding="utf-8") as f:
            f.write(card(r))
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"{r['verdict']} ({r['confidence']} confidence): {r['title'] or 'this role'} at {r['employer'] or 'unknown employer'}")
        for s in r["signals"]:
            print(f"  {'✗' if s['bad'] else '✓'} {s['note']}")
        for e in r["evidence"]:
            print(f"  → {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
