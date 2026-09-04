import requests
import pandas as pd
import time

# List of companies in Romania (or global with RO presence) using Workday
# Format: (subdomain, tenant_id)
# Example URL: https://<subdomain>.myworkdayjobs.com/<tenant_id>
WORKDAY_COMPANIES = [
    ("cognizant.wd3", "CognizantCareers"),
    ("nvidia.wd5", "NVIDIA_External_Career_Site"),
    ("hp.wd5", "ExternalCareerSite"),
    ("crowdstrike.wd5", "crowdstrike_careers"),
    ("uipath.wd3", "UiPath"),
    ("salesforce.wd1", "External_Careers"),
    ("adobe.wd5", "external_experienced"),
]


def scrape_single_workday(subdomain, tenant, search_term, location_filter="Romania"):
    """
    Scrapes a single Workday site.
    Fetching full description requires a second request per job, but we'll start with the list.
    """
    url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{subdomain}/{tenant}/jobs"

    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": search_term}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print(f"  Checking {subdomain}...")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            return []

        data = response.json()
        raw_jobs = data.get("jobPostings", [])
        # print(f"    Raw API returned {len(raw_jobs)} jobs")

        jobs = []

        for job in raw_jobs:
            job_loc = job.get("locationsText", "")

            # Basic location filtering
            if location_filter and location_filter.lower() not in job_loc.lower():
                # print(f"    Skipping '{job.get('title')}' at '{job_loc}' (Location mismatch)")
                continue

            external_path = job.get("externalPath", "")
            job_url = (
                f"https://{subdomain}.myworkdayjobs.com/en-US/{tenant}{external_path}"
            )

            # Fetch Full Description
            desc_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{subdomain}/{tenant}{external_path}"
            try:
                desc_resp = requests.get(desc_url, headers=headers, timeout=5)
                if desc_resp.status_code == 200:
                    full_desc = (
                        desc_resp.json()
                        .get("jobPostingInfo", {})
                        .get("jobDescription", "")
                    )
                else:
                    full_desc = "Check link for details."
            except Exception:
                full_desc = "Check link for details."

            jobs.append(
                {
                    "company": subdomain.capitalize(),
                    "title": job.get("title"),
                    "location": job_loc,
                    "job_url": job_url,
                    "description": full_desc,
                    "source": "Workday",
                }
            )

        return jobs

    except Exception:
        # print(f"    Error scraping {subdomain}: {e}")
        return []


def scrape_all_workday(search_term="Cloud", location="Romania"):
    print(f"\n--- Scraping Workday Sites for '{search_term}' in '{location}' ---")
    all_jobs = []

    for subdomain, tenant in WORKDAY_COMPANIES:
        found = scrape_single_workday(subdomain, tenant, search_term, location)
        if found:
            print(f"    -> Found {len(found)} jobs at {subdomain.capitalize()}")
            all_jobs.extend(found)
        time.sleep(1)  # Be polite

    df = pd.DataFrame(all_jobs)
    print(f"--- Workday Scraping Complete. Total jobs: {len(df)} ---\n")
    return df


if __name__ == "__main__":
    # Test run
    df = scrape_all_workday("", "Romania")
    if not df.empty:
        print(df[["company", "title", "location"]].head())
