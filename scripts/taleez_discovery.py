#!/usr/bin/env python3
"""One-off Taleez discovery pass — finds Toulouse-based companies using Taleez
that aren't yet tracked in companies.json, by scanning every posting in their
sitewide job sitemap. Meant to be run manually (workflow_dispatch), not on a
recurring schedule — see .github/workflows/taleez_discovery.yml.

Run from GitHub Actions runners rather than a local machine: an earlier local
attempt got the local IP blocked twice (once at 20 concurrent workers, again
at just 4), even though robots.txt has no Crawl-delay and doesn't disallow
/apply/. GitHub's runner IPs are unrelated to that block.
"""
import json
import re
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; JobHuntJarvis/1.0; +https://github.com/ralphmartynward/joblistings)"
HEADERS = {"User-Agent": UA}
MAX_WORKERS = 6
ERROR_WINDOW = 100
ERROR_RATE_ABORT_THRESHOLD = 0.5


def fetch_sitemap_urls():
    r = requests.get("https://taleez.com/sitemap-job.xml", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return re.findall(r"<loc>(.*?)</loc>", r.text)


def fetch_job(url):
    """Returns (result_or_None, ok) — ok=False marks a fetch failure for the
    error-rate monitor, distinct from a successful fetch that's just not Toulouse."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None, False
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", {"type": "application/ld+json"})
        if not script or not script.string:
            return None, True
        data = json.loads(script.string)
        locality = ((data.get("jobLocation") or {}).get("address") or {}).get("addressLocality", "") or ""
        org = (data.get("hiringOrganization") or {}).get("name") or (data.get("identifier") or {}).get("name") or ""
        title = data.get("title", "")
        if "toulouse" in locality.lower() and org:
            return {"company": org, "title": title, "url": url}, True
        return None, True
    except Exception:
        return None, False


def slugify_candidates(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"\b(SAS|SARL|SA|SAS U|Group|Groupe|Inc)\b", "", n, flags=re.IGNORECASE)
    n = n.strip()
    candidates = set()
    lower = n.lower()
    candidates.add(re.sub(r"[^a-z0-9]+", "", lower))
    candidates.add(re.sub(r"[^a-z0-9]+", "-", lower).strip("-"))
    candidates.add(re.sub(r"\s+", "", lower))
    return [c for c in candidates if c]


def resolve_subdomain(company_name):
    for slug in slugify_candidates(company_name):
        try:
            r = requests.get(f"https://{slug}.taleez.com/api/careez", headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                remote_name = (data.get("name") or "").lower()
                if remote_name and (remote_name in company_name.lower() or company_name.lower() in remote_name):
                    return f"{slug}.taleez.com"
        except Exception:
            continue
    return None


def main():
    print("Fetching sitemap...")
    urls = fetch_sitemap_urls()
    total_urls = len(urls)
    print(f"Total URLs: {total_urls}")

    toulouse_jobs = []
    done = 0
    aborted = False
    recent_outcomes = deque(maxlen=ERROR_WINDOW)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_job, u): u for u in urls}
        for fut in as_completed(futures):
            done += 1
            result, ok = fut.result()
            recent_outcomes.append(ok)
            if result:
                toulouse_jobs.append(result)
            if done % 500 == 0:
                print(f"  progress: {done}/{total_urls}")
            if len(recent_outcomes) == ERROR_WINDOW:
                error_rate = 1 - (sum(recent_outcomes) / ERROR_WINDOW)
                if error_rate > ERROR_RATE_ABORT_THRESHOLD:
                    print(f"ABORTING at {done}/{total_urls} — error rate {error_rate:.0%} over last {ERROR_WINDOW} requests")
                    aborted = True
                    for f in futures:
                        f.cancel()
                    break

    print(f"Scanned: {done}/{total_urls} (aborted early: {aborted})")
    print(f"Toulouse-located postings: {len(toulouse_jobs)}")

    by_company = {}
    for j in toulouse_jobs:
        by_company.setdefault(j["company"], []).append(j)

    print(f"Unique Toulouse companies: {len(by_company)}")

    companies = json.load(open("companies.json", encoding="utf-8"))
    existing_names = [c["name"].lower() for c in companies]

    def already_tracked(name):
        nl = name.lower()
        return any(nl in en or en in nl for en in existing_names)

    new_companies = {c: jobs for c, jobs in by_company.items() if not already_tracked(c)}
    print(f"New (not yet tracked) companies: {len(new_companies)}")

    print("Resolving subdomains for new companies...")
    results = []
    for company, jobs in new_companies.items():
        subdomain = resolve_subdomain(company)
        results.append({
            "company": company,
            "count": len(jobs),
            "example_title": jobs[0]["title"],
            "example_url": jobs[0]["url"],
            "subdomain": subdomain,
        })

    results.sort(key=lambda r: -r["count"])

    with open("_taleez_discovery_output.json", "w", encoding="utf-8") as f:
        json.dump({
            "scanned": done,
            "total_urls": total_urls,
            "aborted_early": aborted,
            "toulouse_postings": len(toulouse_jobs),
            "unique_toulouse_companies": len(by_company),
            "new_companies": results,
        }, f, ensure_ascii=False, indent=2)

    print("Done. Wrote _taleez_discovery_output.json")


if __name__ == "__main__":
    main()
