# Job Hunt Jarvis

Monitors career pages daily and emails you a digest of new job postings.

## How it works

1. Reads `companies.json` for the list of companies to watch
2. Scrapes each career page using a per-site parser
3. Compares results against `jobs_seen.json` (previously seen URLs)
4. If new jobs are found, sends an HTML email digest
5. Updates `jobs_seen.json` with the current listings

Runs automatically at **8:00 AM UTC** via GitHub Actions (free on public repos).

---

## Setup

### 1. Fork / push this repo to GitHub (keep it public for free Actions minutes)

### 2. Add repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `GMAIL_USER` | Your Gmail address (e.g. `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | A [Gmail App Password](https://myaccount.google.com/apppasswords) (not your real password) |
| `RECIPIENT_EMAIL` | Where to send the digest (can be the same address) |

### 3. Run locally

```bash
pip install -r requirements.txt

# Dry run — prints findings, no email, no file update
python scraper.py --test

# Full run — sends email if new jobs, updates jobs_seen.json
GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=xxxx RECIPIENT_EMAIL=you@gmail.com python scraper.py
```

---

## Adding more companies

Edit `companies.json`. Each entry needs:

```json
{
  "name": "Company Name",
  "url": "https://careers.company.com/jobs",
  "parser": "lever"
}
```

### Available parsers

| `parser` value | Works for |
|---|---|
| `smappen` | smappen.fr/jobs (WordPress-style static page) |
| `lever` | Any `jobs.lever.co/{slug}` board |
| `greenhouse` | Any `boards.greenhouse.io/{slug}` board (add `"config": {"slug": "..."}` if the slug differs from the URL) |
| `welcometothejungle` | welcometothejungle.com company pages |

For a site that doesn't match any existing parser, add a new function to `scraper.py` following the same signature:

```python
def parse_mysite(company: dict) -> list[dict]:
    # return [{"title": "...", "url": "..."}, ...]
```

Then register it in the `PARSERS` dict and use `"parser": "mysite"` in `companies.json`.

---

## Files

| File | Purpose |
|---|---|
| `scraper.py` | Main script |
| `companies.json` | List of companies to watch |
| `jobs_seen.json` | Persistent store of seen job URLs (auto-updated) |
| `requirements.txt` | Python dependencies |
| `.github/workflows/daily_scrape.yml` | GitHub Actions cron workflow |
