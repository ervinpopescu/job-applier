from __future__ import annotations

from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from job_applier.scrapers.activity_checker import (
    check_bestjobs_activity,
    check_linkedin_activity,
    is_job_active,
)


def test_bestjobs_inactive_next_data() -> None:
    mock_html = """
    <html>
        <head>
            <script id="__NEXT_DATA__" type="application/json">
            {
                "props": {
                    "pageProps": {
                        "job": {
                            "title": "Senior Software Engineer",
                            "active": false,
                            "state": "passive"
                        }
                    }
                }
            }
            </script>
        </head>
        <body>
            <h1>Senior Software Engineer</h1>
        </body>
    </html>
    """
    soup = BeautifulSoup(mock_html, "html.parser")
    is_active, reason = check_bestjobs_activity(
        soup, "https://www.bestjobs.eu/loc-de-munca/senior-software-engineer-343"
    )
    assert is_active is False
    assert "inactive" in reason or "passive" in reason

    # Test through is_job_active with html parameter
    ok, r_msg = is_job_active(
        "https://www.bestjobs.eu/loc-de-munca/senior-software-engineer-343",
        html=mock_html,
    )
    assert ok is False
    assert "inactive" in r_msg or "passive" in r_msg


def test_bestjobs_active_next_data() -> None:
    mock_html = """
    <html>
        <head>
            <script id="__NEXT_DATA__" type="application/json">
            {
                "props": {
                    "pageProps": {
                        "job": {
                            "title": "Python Developer",
                            "active": true,
                            "state": "active"
                        }
                    }
                }
            }
            </script>
        </head>
        <body>
            <button>Aplică rapid</button>
        </body>
    </html>
    """
    ok, reason = is_job_active(
        "https://www.bestjobs.eu/loc-de-munca/python-dev-10", html=mock_html
    )
    assert ok is True
    assert reason == "Active"


def test_linkedin_closed_detection() -> None:
    mock_html = """
    <html>
        <body>
            <div class="jobs-details-top-card__closed-indicator">
                No longer accepting applications
            </div>
        </body>
    </html>
    """
    soup = BeautifulSoup(mock_html, "html.parser")
    ok, reason = check_linkedin_activity(soup, "no longer accepting applications")
    assert ok is False
    assert "closed" in reason.lower() or "no longer" in reason.lower()

    ok2, _ = is_job_active("https://www.linkedin.com/jobs/view/99999", html=mock_html)
    assert ok2 is False


def test_indeed_expired_detection() -> None:
    mock_html = """
    <html>
        <body>
            <div class="jobsearch-JobComponent-description">
                This job has expired on Indeed.
            </div>
        </body>
    </html>
    """
    ok, reason = is_job_active("https://ro.indeed.com/viewjob?jk=123", html=mock_html)
    assert ok is False
    assert "expired" in reason.lower()


def test_romanian_portal_expired_phrases() -> None:
    mock_html = """
    <html>
        <body>
            <div class="alert alert-warning">
                Acest anunț a expirat. Nu mai acceptă aplicații.
            </div>
        </body>
    </html>
    """
    ok, reason = is_job_active(
        "https://www.ejobs.ro/user/locuri-de-munca/devops/123", html=mock_html
    )
    assert ok is False
    assert "anunț a expirat" in reason.lower() or "closure notice" in reason.lower()


def test_http_404_detection() -> None:
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        ok, reason = is_job_active("https://jobs.lever.co/company/old-job-123")
        assert ok is False
        assert "404" in reason
