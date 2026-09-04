from __future__ import annotations

from pathlib import Path

import pandas as pd
from jobspy import scrape_jobs

from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.scrapers.ats_scraper import (  # type: ignore[import-not-found]
    scrape_greenhouse_jobs,
    scrape_lever_jobs,
)
from job_applier.scrapers.region_config import (  # type: ignore[import-not-found]
    RegionScope,
)
from job_applier.scrapers.regional_scraper import (  # type: ignore[import-not-found]
    scrape_bestjobs,
    scrape_ejobs,
    scrape_hipo,
    scrape_jooble,
    scrape_undelucram,
)
from job_applier.scrapers.remote_scraper import (  # type: ignore[import-not-found]
    scrape_remote_jobs,
)
from job_applier.utils import get_project_root, sanitize_name

DEFAULT_SITES: list[str] = get_platforms_config().get("default_sites", [])


def detect_country_for_indeed(location: str) -> str:
    """Infers appropriate country code for Indeed."""
    loc = location.lower()
    if any(
        w in loc
        for w in [
            "romania",
            "bucharest",
            "bucuresti",
            "cluj",
            "timisoara",
            "iasi",
            "constanta",
        ]
    ):
        return "romania"
    if any(w in loc for w in ["uk", "united kingdom", "london", "manchester"]):
        return "uk"
    if any(w in loc for w in ["germany", "berlin", "munich", "frankfurt"]):
        return "germany"
    if any(w in loc for w in ["france", "paris"]):
        return "france"
    if any(w in loc for w in ["netherlands", "amsterdam"]):
        return "netherlands"
    return "usa"


def fetch_jobs(
    search_term: str = "Cloud Architect",
    locations: list[str] | None = None,
    results_wanted: int = 15,
    sites: list[str] | None = None,
    is_remote: bool = False,
    region: str = "EMEA",
    countries: list[str] | None = None,
    emea_only: bool = True,
) -> Path | None:
    """
    Fetches jobs across LinkedIn, Indeed, Google Jobs, Greenhouse, Lever, and Remote tech boards.
    Each platform is scraped with isolated error boundaries and filtered according to regional scope.
    """
    scope = RegionScope(region=region, countries=countries, strict=emea_only)
    if locations is None:
        locations = ["Bucharest"]
    if isinstance(locations, str):
        locations = [locations]

    target_sites = [s.strip().lower() for s in (sites or DEFAULT_SITES)]
    all_jobs = pd.DataFrame()

    print(f"\n📡 Starting Multi-Platform Job Search for '{search_term}'...")
    print(f"   Target Platforms: {', '.join(target_sites).title()}")
    print(f"   Locations:        {', '.join(locations)}")

    for loc in locations:
        country_indeed = detect_country_for_indeed(loc)

        # 1. JobSpy Platforms (LinkedIn, Indeed, Google, Glassdoor, ZipRecruiter)
        jobspy_sites = [
            s
            for s in target_sites
            if s in ["linkedin", "indeed", "google", "glassdoor", "zip_recruiter"]
        ]

        for site in jobspy_sites:
            print(f"  Fetching from {site.upper()} in '{loc}'...")
            try:
                kwargs = {
                    "site_name": site,
                    "search_term": search_term,
                    "location": loc,
                    "results_wanted": results_wanted,
                    "hours_old": 72,
                    "is_remote": is_remote,
                }
                if site == "indeed":
                    country_code = scope.get_jobspy_country_code(loc)
                    kwargs["country_indeed"] = (
                        country_code if is_remote else country_indeed
                    )
                if site == "linkedin":
                    kwargs["linkedin_fetch_description"] = True
                if site == "glassdoor":
                    kwargs["country_indeed"] = scope.get_jobspy_country_code(loc)
                    jobs_df = scrape_jobs(**kwargs)
                else:
                    jobs_df = scrape_jobs(**kwargs)

                if jobs_df is not None and not jobs_df.empty:
                    print(f"    Found {len(jobs_df)} jobs on {site.upper()} in '{loc}'")
                    jobs_df["location_scraped"] = loc
                    jobs_df["source"] = site
                    all_jobs = pd.concat([all_jobs, jobs_df], ignore_index=True)
                else:
                    print(f"    No jobs returned from {site.upper()} in '{loc}'")
            except Exception as e:
                print(f"    Notice: {site.upper()} search skipped: {e}")

        # 2. Direct ATS: Greenhouse API (Datadog, GitLab, Cloudflare, Canonical, Stripe, etc.)
        if "greenhouse" in target_sites:
            print("  Fetching from GREENHOUSE direct ATS boards...")
            try:
                gh_jobs = scrape_greenhouse_jobs(
                    search_term=search_term,
                    location=loc,
                    region_scope=scope,
                    emea_only=emea_only,
                )
                if not gh_jobs.empty:
                    print(f"    Found {len(gh_jobs)} jobs on GREENHOUSE direct boards")
                    gh_jobs["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, gh_jobs], ignore_index=True)
            except Exception as e:
                print(f"    Notice: Greenhouse search skipped: {e}")

        # 3. Direct ATS: Lever API (Spotify, Palantir, etc.)
        if "lever" in target_sites:
            print("  Fetching from LEVER direct ATS boards...")
            try:
                lever_jobs = scrape_lever_jobs(
                    search_term=search_term,
                    location=loc,
                    region_scope=scope,
                    emea_only=emea_only,
                )
                if not lever_jobs.empty:
                    print(f"    Found {len(lever_jobs)} jobs on LEVER direct boards")
                    lever_jobs["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, lever_jobs], ignore_index=True)
            except Exception as e:
                print(f"    Notice: Lever search skipped: {e}")

        # 4. Regional Romanian Job Boards (eJobs, BestJobs, Hipo, Undelucram, Jooble)
        if "ejobs" in target_sites:
            print("  Fetching from EJOBS (ejobs.ro)...")
            try:
                ejobs_df = scrape_ejobs(
                    search_term=search_term, location=loc, limit=results_wanted
                )
                if not ejobs_df.empty:
                    print(f"    Found {len(ejobs_df)} jobs on EJOBS")
                    ejobs_df["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, ejobs_df], ignore_index=True)
            except Exception as e:
                print(f"    Notice: eJobs search skipped: {e}")

        if "bestjobs" in target_sites:
            print("  Fetching from BESTJOBS (bestjobs.eu)...")
            try:
                bj_df = scrape_bestjobs(
                    search_term=search_term, location=loc, limit=results_wanted
                )
                if not bj_df.empty:
                    print(f"    Found {len(bj_df)} jobs on BESTJOBS")
                    bj_df["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, bj_df], ignore_index=True)
            except Exception as e:
                print(f"    Notice: BestJobs search skipped: {e}")

        if "hipo" in target_sites:
            print("  Fetching from HIPO (hipo.ro)...")
            try:
                hipo_df = scrape_hipo(
                    search_term=search_term, location=loc, limit=results_wanted
                )
                if not hipo_df.empty:
                    print(f"    Found {len(hipo_df)} jobs on HIPO")
                    hipo_df["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, hipo_df], ignore_index=True)
            except Exception as e:
                print(f"    Notice: Hipo search skipped: {e}")

        if "undelucram" in target_sites:
            print("  Fetching from UNDELUCRAM (undelucram.ro)...")
            try:
                und_df = scrape_undelucram(
                    search_term=search_term, location=loc, limit=results_wanted
                )
                if not und_df.empty:
                    print(f"    Found {len(und_df)} jobs on UNDELUCRAM")
                    und_df["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, und_df], ignore_index=True)
            except Exception as e:
                print(f"    Notice: Undelucram search skipped: {e}")

        if "jooble" in target_sites:
            print("  Fetching from JOOBLE (ro.jooble.org)...")
            try:
                jooble_df = scrape_jooble(
                    search_term=search_term, location=loc, limit=results_wanted
                )
                if not jooble_df.empty:
                    print(f"    Found {len(jooble_df)} jobs on JOOBLE")
                    jooble_df["location_scraped"] = loc
                    all_jobs = pd.concat([all_jobs, jooble_df], ignore_index=True)
            except Exception as e:
                print(f"    Notice: Jooble search skipped: {e}")

    # 4. Remote Tech Job Boards (Jobicy / RemoteOK)
    if "remote" in target_sites:
        print("  Fetching from REMOTE tech boards...")
        try:
            remote_jobs = scrape_remote_jobs(
                search_term=search_term,
                count=results_wanted,
                region_scope=scope,
                emea_only=emea_only,
            )
            if not remote_jobs.empty:
                print(f"    Found {len(remote_jobs)} jobs on REMOTE tech boards")
                remote_jobs["location_scraped"] = "Remote"
                all_jobs = pd.concat([all_jobs, remote_jobs], ignore_index=True)
        except Exception as e:
            print(f"    Notice: Remote search skipped: {e}")

    print(f"\n📊 Total aggregated jobs found across all platforms: {len(all_jobs)}")

    if not all_jobs.empty:
        project_root = get_project_root()
        data_dir = project_root / "data" / "scraped_jobs"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Ensure required standard columns exist
        for col in ["company", "title", "location", "job_url", "description"]:
            if col not in all_jobs.columns:
                all_jobs[col] = ""

        # Remove duplicate jobs based on URL
        if "job_url" in all_jobs.columns:
            all_jobs = all_jobs.drop_duplicates(subset=["job_url"])

        # Filter strictly according to region scope
        if not all_jobs.empty and (scope.region != "GLOBAL" or scope.countries):
            initial_count = len(all_jobs)
            valid_rows = []
            for _, r in all_jobs.iterrows():
                loc_str = str(r.get("location", ""))
                desc_str = str(r.get("description", ""))
                is_ok, _ = scope.is_compatible(loc_str, desc_str)
                if is_ok:
                    valid_rows.append(r)
            all_jobs = pd.DataFrame(valid_rows) if valid_rows else pd.DataFrame()
            dropped = initial_count - len(all_jobs)
            if dropped > 0:
                print(
                    f"  Filtered out {dropped} postings outside target region ({scope.region})."
                )

        output_file = data_dir / f"jobs_{sanitize_name(search_term)}.csv"
        all_jobs.to_csv(output_file, index=False)
        print(f"Saved {len(all_jobs)} unique jobs to: {output_file.name}")
        return output_file
    else:
        print("No jobs found across selected sites.")
        return None


if __name__ == "__main__":
    fetch_jobs()
