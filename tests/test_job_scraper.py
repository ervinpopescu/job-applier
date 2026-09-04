from job_applier.scrapers.ats_scraper import (  # type: ignore[import-not-found]
    scrape_greenhouse_jobs,
)
from job_applier.scrapers.job_scraper import (  # type: ignore[import-not-found]
    detect_country_for_indeed,
)
from job_applier.scrapers.regional_scraper import (  # type: ignore[import-not-found]
    scrape_bestjobs,
    scrape_ejobs,
    scrape_hipo,
    scrape_jooble,
    scrape_undelucram,
)


def test_detect_country_for_indeed():
    assert detect_country_for_indeed("Bucharest") == "romania"
    assert detect_country_for_indeed("Cluj-Napoca, Romania") == "romania"
    assert detect_country_for_indeed("London, UK") == "uk"
    assert detect_country_for_indeed("Berlin") == "germany"
    assert detect_country_for_indeed("New York") == "usa"


def test_greenhouse_scraper_structure():
    # Test that scraper function returns a dataframe with standard columns
    df = scrape_greenhouse_jobs(search_term="nonexistent_role_xyz_123", location="")
    assert "site" in df.columns or df.empty


def test_regional_scrapers_structure():
    # Verify regional scrapers return valid DataFrames
    df_ej = scrape_ejobs(search_term="Cloud", location="Bucuresti", limit=2)
    assert isinstance(df_ej.empty, bool)

    df_bj = scrape_bestjobs(search_term="Cloud", location="Bucuresti", limit=2)
    assert isinstance(df_bj.empty, bool)

    df_hipo = scrape_hipo(search_term="Cloud", location="Bucuresti", limit=2)
    assert isinstance(df_hipo.empty, bool)

    df_und = scrape_undelucram(search_term="Cloud", location="Bucuresti", limit=2)
    assert isinstance(df_und.empty, bool)

    df_jooble = scrape_jooble(search_term="Cloud", location="Bucuresti", limit=2)
    assert isinstance(df_jooble.empty, bool)
