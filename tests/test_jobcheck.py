import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import jobcheck as jc  # noqa: E402

TODAY = dt.date(2026, 10, 21)
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def page(**jp):
    d = {"@context": "https://schema.org", "@type": "JobPosting", "title": "AI Engineer",
         "hiringOrganization": {"@type": "Organization", "name": "Damon Dynamics Ltd", "sameAs": "https://damondynamics.co.uk"},
         "datePosted": "2026-10-15", "description": "<p>Build RAG systems.</p>"}
    d.update(jp)
    return f'<html><script type="application/ld+json">{json.dumps(d)}</script><body>Apply now</body></html>'


def ch(hits, profile=None):
    """Fake Companies House: search then profile."""
    def fake(url, auth=None, timeout=20):
        if "/search/" in url:
            return {"items": hits}
        if "/company/" in url:
            return profile or hits[0]
        return None
    return fake


ACTIVE = {"title": "DAMON DYNAMICS LTD", "company_number": "16534001", "company_status": "active",
          "address": {"postal_code": "BD5 8AB"}, "date_of_creation": "2025-06-01"}


class Parse(unittest.TestCase):
    def test_jobposting_found_in_graph(self):
        p = '<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},{"@type":"JobPosting","title":"X"}]}</script>'
        self.assertEqual(jc.job_posting(p)["title"], "X")

    def test_age_from_text_when_no_schema(self):
        s = jc.posting_signals({}, "Posted 75 days ago", TODAY)
        self.assertEqual(s[0]["value"], 75)
        self.assertTrue(s[0]["bad"])

    def test_expired(self):
        s = jc.posting_signals({"datePosted": "2026-08-01", "validThrough": "2026-09-01"}, "", TODAY)
        self.assertTrue(any(x["signal"] == "expired" and x["bad"] for x in s))


class CompaniesHouse(unittest.TestCase):
    def test_no_key_skips(self):
        self.assertEqual(jc.companies_house("X", "", None)["value"], "skipped")

    def test_exact_active_match(self):
        with mock.patch.object(jc, "get_json", ch([ACTIVE])):
            s = jc.companies_house("Damon Dynamics Ltd", "", "k")
        self.assertEqual(s["value"], "active")
        self.assertFalse(s["bad"])
        self.assertIn("16534001", s["evidence"])

    def test_overdue_is_bad(self):
        prof = dict(ACTIVE, accounts={"overdue": True})
        with mock.patch.object(jc, "get_json", ch([ACTIVE], prof)):
            s = jc.companies_house("Damon Dynamics", "", "k")
        self.assertTrue(s["bad"])
        self.assertIn("overdue: accounts", s["note"])

    def test_ambiguous_narrowed_by_postcode(self):
        other = dict(ACTIVE, company_number="999", address={"postal_code": "SW1A 1AA"})
        with mock.patch.object(jc, "get_json", ch([ACTIVE, other])):
            self.assertEqual(jc.companies_house("Damon Dynamics", "", "k")["value"], "ambiguous")
            self.assertEqual(jc.companies_house("Damon Dynamics", "BD5 8AB", "k")["value"], "active")

    def test_not_found(self):
        with mock.patch.object(jc, "get_json", ch([])):
            s = jc.companies_house("Ghost Corp", "", "k")
        self.assertEqual(s["value"], "not found")
        self.assertTrue(s["bad"])


class Text(unittest.TestCase):
    def test_scam_wording_and_free_mail(self):
        s = jc.text_signals("Pay a registration fee. Contact on WhatsApp only. Reply to jobs@gmail.com", "")
        notes = {x["note"] for x in s}
        self.assertIn("asks for a fee", notes)
        self.assertIn("contact through a chat app only", notes)
        self.assertTrue(any(x["signal"] == "contact" for x in s))

    def test_clean_text_has_no_signals(self):
        self.assertEqual(jc.text_signals("We are hiring an engineer. Apply via careers@damondynamics.co.uk", ""), [])


class Verdicts(unittest.TestCase):
    def test_real(self):
        v, _ = jc.verdict([{"signal": "posting_age", "value": 6, "bad": False},
                           {"signal": "employer", "value": "active", "bad": False}])
        self.assertEqual(v, "REAL")

    def test_ghost_by_age(self):
        v, _ = jc.verdict([{"signal": "posting_age", "value": 90, "bad": True},
                           {"signal": "employer", "value": "active", "bad": False}])
        self.assertEqual(v, "GHOST")

    def test_scam_by_wording(self):
        v, c = jc.verdict([{"signal": "wording", "value": 3, "bad": True}])
        self.assertEqual((v, c), ("SCAM", "high"))

    def test_dissolved_is_ghost(self):
        self.assertEqual(jc.verdict([{"signal": "employer", "value": "dissolved", "bad": True}])[0], "GHOST")

    def test_unverified(self):
        self.assertEqual(jc.verdict([{"signal": "employer", "value": "skipped", "bad": False}])[0], "UNVERIFIED")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = os.path.join(self.tmp, "job.html")
        with open(self.f, "w") as fh:
            fh.write(page())
        self.db = os.path.join(self.tmp, "ledger.db")

    def test_file_real_and_repost_ledger(self):
        def net(url, auth=None, timeout=20):
            if "rdap.org" in url:
                return {"events": [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}]}
            if "dns.google" in url:
                return {"Answer": [{"type": 15}]}
            return ch([ACTIVE])(url, auth)
        with mock.patch.object(jc, "get_json", net):
            r = jc.check(self.f, key="k", today=TODAY, ledger_path=self.db)
            self.assertEqual(r["verdict"], "REAL")
            self.assertEqual(r["employer"], "Damon Dynamics Ltd")
            self.assertTrue(any(s["signal"] == "domain_age" and not s["bad"] for s in r["signals"]))
            for _ in range(2):
                r = jc.check(self.f, key="k", today=TODAY, ledger_path=self.db)
        rep = next(s for s in r["signals"] if s["signal"] == "repost")
        self.assertEqual(rep["value"], 3)
        self.assertEqual(r["verdict"], "GHOST")

    def test_card_renders(self):
        r = {"verdict": "GHOST", "title": "AI <Eng>", "employer": "X", "checked": "2026-10-21",
             "signals": [{"bad": True, "note": "open for 90 days"}]}
        out = jc.card(r)
        self.assertIn("GHOST", out)
        self.assertIn("AI &lt;Eng&gt;", out)
        self.assertIn("open for 90 days", out)

    def test_cli_json_no_network(self):
        with mock.patch.object(jc, "get_json", lambda *a, **k: None), \
             mock.patch.object(jc, "LEDGER", self.db), mock.patch("sys.stdout") as out:
            self.assertEqual(jc.main([self.f, "--json"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed)["employer"], "Damon Dynamics Ltd")


if __name__ == "__main__":
    unittest.main()
