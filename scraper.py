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
    """Static WordPress-style page. HTML5 parsers restructure <a><h3> into <h3><a>,
    so we find all <a href='/job/'> and use their text content directly."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if "/job/" not in a["href"]:
            continue
        title = a.get_text(strip=True)
        if not title:  # skip image-only links (no text)
            continue
        url = urljoin("https://www.smappen.fr", a["href"])
        if url not in seen:
            seen.add(url)
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
    """WTTJ — two-step public API (HTML page is JS-rendered/bot-blocked).
    1. Resolve org slug → org reference via api.welcometothejungle.com
    2. Fetch jobs via welcomekit.co embed API.
    Only jobs with 'wttj_fr' in cms_sites_references are live on WTTJ;
    URL comes from websites_urls to avoid 404s from reference-based paths."""
    import re as _re
    slug = _re.search(r"/companies(?:-v1)?/([^/]+)", company["url"]).group(1)
    ref_r = _get(f"https://api.welcometothejungle.com/api/v1/organizations/{slug}")
    org_ref = ref_r.json()["organization"]["reference"]
    jobs_r = _get(f"https://www.welcomekit.co/api/v1/embed?organization_reference={org_ref}")
    jobs = []
    for j in jobs_r.json().get("jobs", []):
        if not j.get("name") or "wttj_fr" not in j.get("cms_sites_references", []):
            continue
        wttj_url = next(
            (w["url"] for w in j.get("websites_urls", []) if w.get("website_reference") == "wttj_fr"),
            None,
        )
        if wttj_url:
            jobs.append({"title": j["name"], "url": wttj_url})
    return jobs


def parse_workable(company: dict) -> list[dict]:
    """Workable job board — uses the widget JSON API (no scraping needed)."""
    slug = company["url"].rstrip("/").split("/")[-1]
    api_url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    r = requests.post(api_url, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    return [
        {"title": j["title"], "url": f"https://apply.workable.com/{slug}/j/{j['shortcode']}"}
        for j in r.json().get("results", [])
        if j.get("title") and j.get("shortcode")
    ]


def parse_aniti(company: dict) -> list[dict]:
    """ANITI jobs page — each listing is <h5><a href='pdf-or-post'>Title</a></h5>."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for h5 in soup.find_all("h5"):
        a = h5.find("a", href=True)
        if not a:
            continue
        title = h5.get_text(strip=True)
        url = a["href"]
        if title and url and url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_hautegaronnenumerique(company: dict) -> list[dict]:
    """HGN recruitment page — no job container template; returns empty while no-jobs text is present.
    When jobs appear, looks for <a> links in page content (structure unknown until first posting)."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    if "pas d" in soup.get_text() and "offres de recrutement" in soup.get_text():
        return []
    base_url = company["url"].rstrip("/")
    jobs = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        url = urljoin(company["url"], href)
        if url.rstrip("/") == base_url:
            continue
        if any(kw in href.lower() for kw in ("recrutement/", "offre", "emploi", "poste", ".pdf")):
            if url not in seen:
                seen.add(url)
                jobs.append({"title": title, "url": url})
    return jobs


def parse_enercoop(company: dict) -> list[dict]:
    """Enercoop — listing page is JS-rendered; scrape sitemap for job URLs then fetch each title from h1."""
    r = _get("https://recrutement.enercoop.fr/sitemap.xml")
    soup = BeautifulSoup(r.text, "xml")
    job_urls = [loc.text for loc in soup.find_all("loc") if "/offres/" in loc.text]
    jobs = []
    for url in job_urls:
        try:
            time.sleep(REQUEST_DELAY)
            detail = BeautifulSoup(_get(url).text, "html.parser")
            h1 = detail.find("h1")
            if h1:
                title = h1.get_text(strip=True)
                if title:
                    jobs.append({"title": title, "url": url})
        except Exception as exc:
            log.warning("Enercoop job page error %s: %s", url, exc)
    return jobs


def parse_citiz(company: dict) -> list[dict]:
    """Citiz Occitanie — WordPress /recrutement/ page; job listings are h3 > a links."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if "/recrutement/" not in href or href.rstrip("/").endswith("/recrutement"):
            continue
        title = a.get_text(strip=True)
        if title and href not in seen:
            seen.add(href)
            jobs.append({"title": title, "url": href})
    return jobs


def parse_taleez(company: dict) -> list[dict]:
    """Taleez job board — unauthenticated JSON API at {slug}.taleez.com/api/careez.
    Public apply links live at taleez.com/apply/{slug}, not {slug}.taleez.com/jobs/{slug}
    (the latter 404s for applicants)."""
    slug = company["url"].rstrip("/").split("//")[-1].split(".taleez.com")[0]
    r = _get(f"https://{slug}.taleez.com/api/careez")
    return [
        {"title": j["label"], "url": f"https://taleez.com/apply/{j['slug']}"}
        for j in r.json().get("jobs", [])
        if j.get("label") and j.get("slug")
    ]


def parse_makesense(company: dict) -> list[dict]:
    """Makesense job board — server-rendered. Jobs are <a href='/fr/jobs/...'> with title in inner <h3>."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/fr/jobs/"):
            continue
        h3 = a.find("h3")
        if not h3:
            continue
        title = h3.get_text(strip=True)
        if not title:
            continue
        url = "https://jobs.makesense.org" + href.split("?")[0]
        if url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_ekitia(company: dict) -> list[dict]:
    """Ekitia jobs page — job posts appear as links with 'offre' or 'recru' in the URL."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not any(kw in href.lower() for kw in ("offre", "emploi", "recru", "poste")):
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        url = urljoin("https://www.ekitia.fr", href)
        if url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_cls(company: dict) -> list[dict]:
    """CLS careers page — Softy ATS, server-rendered. Listings are
    <div class="offre_emploi"><h3><span class="title"><a>Title</a>."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for div in soup.find_all("div", class_="offre_emploi"):
        h3 = div.find("h3")
        a = h3.find("a", href=True) if h3 else None
        if not a:
            continue
        title = a.get_text(strip=True)
        url = urljoin("https://www.cls.fr", a["href"])
        if title and url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_imajing(company: dict) -> list[dict]:
    """Imajing careers page — Elementor icon-box list, no per-job links.
    Titles are <h3 class="elementor-icon-box-title">; synthesize a stable
    per-title URL fragment on the career page for dedup purposes."""
    import re as _re
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    base_url = company["url"].rstrip("/")
    for h3 in soup.find_all("h3", class_="elementor-icon-box-title"):
        title = h3.get_text(" ", strip=True)
        if not title:
            continue
        slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        url = f"{base_url}#{slug}"
        if url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_sensingai(company: dict) -> list[dict]:
    """Sensing (Absolut Sensing) careers page — Elementor loop grid.
    Each posting is <div class="e-loop-item"><h4>Title</h4>...<a href>.
    Skip the evergreen 'Spontaneous application' entry."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for item in soup.find_all("div", class_="e-loop-item"):
        h4 = item.find("h4")
        a = item.find("a", href=True)
        if not h4 or not a:
            continue
        title = h4.get_text(strip=True)
        if not title or "spontaneous application" in title.lower():
            continue
        url = a["href"]
        if url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


def parse_welcomekit(company: dict) -> list[dict]:
    """Standalone Welcomekit-hosted career site (WTTJ white-label, e.g. *.welcomekit.co).
    Server-rendered; job links are <a class="jobs-list-item-link" href="/jobs/{slug}">."""
    r = _get(company["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen: set[str] = set()
    for a in soup.find_all("a", class_="jobs-list-item-link", href=True):
        title = a.get_text(" ", strip=True)
        url = urljoin(company["url"], a["href"])
        if title and url not in seen:
            seen.add(url)
            jobs.append({"title": title, "url": url})
    return jobs


PARSERS: dict = {
    "smappen": parse_smappen,
    "lever": parse_lever,
    "greenhouse": parse_greenhouse,
    "welcometothejungle": parse_welcometothejungle,
    "workable": parse_workable,
    "aniti": parse_aniti,
    "ekitia": parse_ekitia,
    "makesense": parse_makesense,
    "taleez": parse_taleez,
    "hautegaronnenumerique": parse_hautegaronnenumerique,
    "enercoop": parse_enercoop,
    "citiz": parse_citiz,
    "cls": parse_cls,
    "imajing": parse_imajing,
    "sensingai": parse_sensingai,
    "welcomekit": parse_welcomekit,
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
