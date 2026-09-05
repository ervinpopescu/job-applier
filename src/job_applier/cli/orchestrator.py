from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from job_applier.pipeline import run_application_pipeline


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="End-to-End Automated Job Application Pipeline"
    )
    parser.add_argument(
        "--api_key",
        default=os.environ.get("GOOGLE_API_KEY"),
        help="Google Gemini API Key (or set GOOGLE_API_KEY env var)",
    )
    parser.add_argument(
        "--terms",
        default="Python Dev,Solutions Architect,Software Engineer,Cloud Architect,DevOps Engineer",
        help="Comma-separated search terms for job scraping",
    )
    parser.add_argument(
        "--locations",
        default="Bucharest",
        help="Comma-separated target locations",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum jobs to fetch per term",
    )
    parser.add_argument(
        "--sites",
        default="linkedin,indeed,ejobs,bestjobs,hipo,undelucram,jooble,google,greenhouse,lever,remote,glassdoor",
        help="Comma-separated sites: linkedin, indeed, ejobs, bestjobs, hipo, undelucram, jooble, google, greenhouse, lever, remote, glassdoor, zip_recruiter",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Search specifically for remote/telework positions across platforms",
    )
    parser.add_argument(
        "--region",
        default="EMEA",
        choices=[
            "emea",
            "europe",
            "romania",
            "usa",
            "global",
            "custom",
            "EMEA",
            "EUROPE",
            "ROMANIA",
            "USA",
            "GLOBAL",
            "CUSTOM",
        ],
        help="Target geographic region filter: EMEA, EUROPE, ROMANIA, USA, GLOBAL, or CUSTOM",
    )
    parser.add_argument(
        "--countries",
        default="",
        help="Comma-separated list of allowed countries or cities (e.g. 'Romania,United Kingdom,Germany')",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Automatically launch browser application immediately after generating each package",
    )
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="Run full autonomous submit without waiting for manual confirmation",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode during auto-apply",
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "chrome", "chromium", "firefox"],
        default=None,
        help="Browser engine to use during auto-apply (auto, chrome, chromium, firefox)",
    )

    args = parser.parse_args()

    if not args.api_key:
        print(
            "Error: GOOGLE_API_KEY is required. Pass via --api_key or set in .env file."
        )
        print("Get an API key here: https://aistudio.google.com/app/apikey")
        return

    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    locations = [loc.strip() for loc in args.locations.split(",") if loc.strip()]
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    countries = (
        [c.strip() for c in args.countries.split(",") if c.strip()]
        if args.countries
        else None
    )

    run_application_pipeline(
        api_key=args.api_key,
        search_terms=terms,
        locations=locations,
        results_wanted=args.limit,
        sites=sites,
        is_remote=args.remote,
        region=args.region.upper(),
        countries=countries,
        auto_apply=args.auto_apply,
        autonomous=args.autonomous,
        headless=args.headless,
        browser=args.browser,
    )


if __name__ == "__main__":
    main()
