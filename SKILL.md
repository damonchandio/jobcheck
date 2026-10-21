---
name: jobcheck
description: Check whether a job ad is real before applying. Use when the user asks "is this job real", "is this a ghost job", "is this a scam", "verify this employer", "should I apply to this", or pastes a job URL. Returns REAL, GHOST, SCAM, SUSPECT or UNVERIFIED with evidence from Companies House (UK company status and overdue filings), the posting date, the employer's domain age, free-mail reply addresses, scam wording and a local repost ledger. Python standard library only, no account needed.
license: MIT
compatibility: Python 3.10 or newer. Companies House step needs a free API key in COMPANIES_HOUSE_API_KEY (optional).
metadata:
  author: Damon Chandio
  version: 0.1.0
---

# jobcheck

Facts about a job ad, not a vibe score. Run it before the user applies, and before any job-application agent fills in a form.

## Run it

```bash
python3 scripts/jobcheck.py <url> --json
```

If the page needs a login (LinkedIn, Indeed and some ATS pages return 403 or 999), do not retry the URL. You already have the page open: pass its text instead, with the employer name.

```bash
python3 scripts/jobcheck.py - --employer "Employer Ltd" --json <<'EOF'
<paste the job ad text>
EOF
```

Options: `--postcode` narrows between same-name companies, `--domain` sets the employer website if the page doesn't, `--card out.html` writes a shareable verdict card, `--no-ledger` skips the repost record.

## Read the result

`verdict` is one of:

- **REAL**: employer is an active UK company with no overdue filings, the ad is under 30 days old, no scam wording.
- **GHOST**: open 60+ days, past its closing date, seen 3+ times, or the employer is dissolved.
- **SCAM**: fee, bank details before starting, chat-app-only contact, or crypto pay.
- **SUSPECT**: employer not on Companies House, or a free-mail reply address, or one piece of scam wording.
- **UNVERIFIED**: not enough data (usually no Companies House key and no posting date).

Each entry in `signals` has `note` (plain English) and `bad` (true when it counts against the ad). `evidence` holds the Companies House page to cite.

## Tell the user

Give the verdict, then the one or two signals that decided it, then the evidence link. Say "not found on Companies House" rather than "fake": agencies, trading names and non-UK employers are not on the register. If the verdict is UNVERIFIED and the user wants the registry check, tell them the key is free at developer.company-information.service.gov.uk and goes in `COMPANIES_HOUSE_API_KEY`.

## Limits

- Companies House covers UK companies only. Charities, sole traders, the NHS and universities are not on it.
- Posting age comes from the schema.org `datePosted` the board embeds, or "posted N days ago" text. Boards that show neither give no age.
- The repost ledger is local (`~/.jobcheck.db`), so it only knows ads this machine has checked.
- This is a checklist to inform a decision, not proof. A REAL verdict means the facts line up, not that the role will be filled.
