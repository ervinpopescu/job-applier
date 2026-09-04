from __future__ import annotations

from job_applier.scrapers.region_config import RegionScope, get_default_region_scope


def test_default_region_is_emea() -> None:
    scope = get_default_region_scope()
    assert scope.region == "EMEA"
    assert scope.strict is True


def test_emea_compatibilities() -> None:
    scope = RegionScope(region="EMEA", strict=True)

    # Allowed EMEA locations
    ok, _ = scope.is_compatible("Bucharest, Romania")
    assert ok is True

    ok, _ = scope.is_compatible("London, United Kingdom")
    assert ok is True

    ok, _ = scope.is_compatible("Berlin, Germany")
    assert ok is True

    ok, _ = scope.is_compatible("Amsterdam, Netherlands")
    assert ok is True

    ok, _ = scope.is_compatible("Remote - EMEA")
    assert ok is True

    ok, _ = scope.is_compatible("Worldwide Remote")
    assert ok is True

    # Non-EMEA rejections
    ok, reason = scope.is_compatible("San Francisco, CA")
    assert ok is False
    assert "United States" in reason or "outside" in reason

    ok, reason = scope.is_compatible("Austin, TX")
    assert ok is False

    ok, reason = scope.is_compatible(
        "Remote", "Candidates must be located in the US and hold US citizenship."
    )
    assert ok is False
    assert "restriction" in reason.lower()


def test_romania_only_scope() -> None:
    scope = RegionScope(region="ROMANIA")
    ok, _ = scope.is_compatible("Cluj-Napoca, Romania")
    assert ok is True

    ok, _ = scope.is_compatible("Timisoara")
    assert ok is True

    ok, _ = scope.is_compatible("London, UK")
    assert ok is False


def test_europe_only_scope() -> None:
    scope = RegionScope(region="EUROPE")
    ok, _ = scope.is_compatible("Zurich, Switzerland")
    assert ok is True

    ok, _ = scope.is_compatible("Paris, France")
    assert ok is True

    ok, _ = scope.is_compatible("New York, NY")
    assert ok is False


def test_custom_countries_scope() -> None:
    scope = RegionScope(region="CUSTOM", countries=["Germany", "Switzerland"])
    ok, _ = scope.is_compatible("Munich, Germany")
    assert ok is True

    ok, _ = scope.is_compatible("Zurich")
    assert ok is True

    ok, _ = scope.is_compatible("Madrid, Spain")
    assert ok is False


def test_global_scope() -> None:
    scope = RegionScope(region="GLOBAL")
    ok, _ = scope.is_compatible("Tokyo, Japan")
    assert ok is True

    ok, _ = scope.is_compatible("Austin, TX")
    assert ok is True
