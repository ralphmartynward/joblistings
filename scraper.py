#!/usr/bin/env python3
"""Job Hunt Jarvis — daily career-page scraper with email digest."""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JOBS_SEEN_PATH = "jobs_seen.json"
COMPANIES_PATH = "companies.json"
REQUEST_DELAY = 2  # seconds between company requests
USER_AGENT = "Mozilla/5.0 (compatible; JobHuntJarvis/1.0; +https://github.com/ralphward/job-hunt-jarvis)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Per-company parsers — add new ones here, then reference by name in companies.json
# ---------------------------------------------------------------------------

def parse_smappen(company: dict) -> list[dict]:
    """Static WordPress-style jobs page. Each listing is <a href="/job/..."><h3>Title</h3>...</a>."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "lxml")
    jobs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/job/" not in href:
            continue
        h3 = a.find("h3")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        url = urljoin("https://www.smappen.fr", href)
        if title and url not in [j["url"] for j in jobs]:
            jobs.append({"title": title, "url": url})
    return jobs


def parse_lever(company: dict) -> list[dict]:
    """Lever job board — uses the public JSON API instead of scraping JS-rendered HTML."""
    slug = company["url"].rstrip("/").split("/")[-1]
    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = _get(api_url)
    return [
        {"title": p["text"], "url": p["hostedUrl"]}
        for p in r.json()
        if p.get("text") and p.get("hostedUrl")
    ]


def parse_greenhouse(company: dict) -> list[dict]:
    """Greenhouse job board — JSON API at boards-api.greenhouse.io."""
    slug = company.get("config", {}).get("slug") or company["url"].rstrip("/").split("/")[-1]
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = _get(api_url)
    return [
        {"title": j["title"], "url": j["absolute_url"]}
        for j in r.json().get("jobs", [])
        if j.get("title") and j.get("absolute_url")
    ]


def parse_welcometothejungle(company: dict) -> list[dict]:
    """Welcome to the Jungle — static HTML, jobs in <div data-testid='job-list-item'>."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "lxml")
    jobs = []
    for item in soup.find_all(attrs={"data-testid": "job-list-item"}):
        a = item.find("a", href=True)
        if not a:
            continue
        title_el = a.find(["h3", "h4", "span"])
        title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
        url = urljoin("https://www.welcometothejungle.com", a["href"])
        if title:
            jobs.append({"title": title, "url": url})
    return jobs


PARSERS: dict = {
    "smappen": parse_smappen,
    "lever": parse_lever,
    "greenhouse": parse_greenhouse,
    "welcometothejungle": parse_welcometothejungle,
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scrape_all(companies: list[dict]) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}
    for i, company in enumerate(companies):
        name = company["name"]
        parser_name = company.get("parser", "")
        parser = PARSERS.get(parser_name)
        if not parser:
            log.warning("No parser '%s' for %s — skipping", parser_name, name)
            results[name] = []
            continue
        if i > 0:
            time.sleep(REQUEST_DELAY)
        try:
            log.info("Scraping %s ...", name)
            jobs = parser(company)
            results[name] = jobs
            log.info("  → %d listing(s) found", len(jobs))
        except Exception as exc:
            log.error("Error scraping %s: %s", name, exc)
            results[name] = []
    return results


def find_new_jobs(results: dict, jobs_seen: dict) -> dict[str, list[dict]]:
    seen_urls = {company: set(urls) for company, urls in jobs_seen.items()}
    new: dict[str, list[dict]] = {}
    for company_name, jobs in results.items():
        fresh = [j for j in jobs if j["url"] not in seen_urls.get(company_name, set())]
        if fresh:
            new[company_name] = fresh
    return new


def update_seen(jobs_seen: dict, results: dict) -> dict:
    for company_name, jobs in results.items():
        jobs_seen[company_name] = [j["url"] for j in jobs]
    return jobs_seen


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_html(new_jobs: dict) -> str:
    today = date.today().strftime("%d %B %Y")
    rows = []
    for company_name, jobs in new_jobs.items():
        rows.append(f"<h2 style='color:#2c3e50'>{company_name}</h2><ul>")
        for job in jobs:
            rows.append(f'  <li><a href="{job["url"]}" style="color:#2980b9">{job["title"]}</a></li>')
        rows.append("</ul>")
    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px">
<h1 style="color:#1a1a2e">🆕 New job listings — {today}</h1>
{body}
<hr style="margin-top:32px">
<p style="color:#888;font-size:12px">Sent by Job Hunt Jarvis</p>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    log.info("Email sent → %s", recipient)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Job Hunt Jarvis — career page monitor")
    parser.add_argument("--test", action="store_true",
                        help="Dry run: print findings without sending email or updating jobs_seen.json")
    args = parser.parse_args()

    companies = load_json(COMPANIES_PATH, [])
    if not companies:
        log.error("No companies found in %s", COMPANIES_PATH)
        return 1

    jobs_seen = load_json(JOBS_SEEN_PATH, {})
    results = scrape_all(companies)
    new_jobs = find_new_jobs(results, jobs_seen)

    if new_jobs:
        total = sum(len(v) for v in new_jobs.values())
        log.info("New jobs found: %d across %d company/companies", total, len(new_jobs))
        for company_name, jobs in new_jobs.items():
            for job in jobs:
                print(f"[NEW]  {company_name:20s}  {job['title']}")
                print(f"       {job['url']}")
    else:
        log.info("No new jobs today.")

    if args.test:
        log.info("[TEST MODE] Skipping email and jobs_seen.json update.")
        return 0

    if new_jobs:
        today_str = date.today().strftime("%Y-%m-%d")
        send_email(f"🆕 New job listings — {today_str}", build_html(new_jobs))

    jobs_seen = update_seen(jobs_seen, results)
    save_json(JOBS_SEEN_PATH, jobs_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
