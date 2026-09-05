from __future__ import annotations

import re

from job_applier.config import get_regions_config


RegionData = tuple[dict[str, set[str]], dict[str, set[str]], list[str], list[str]]


def _load_region_data() -> RegionData:
    cfg = get_regions_config()
    regions = {k: set(v) for k, v in cfg.get("regions", {}).items()}
    aliases = {k: set(v) for k, v in cfg.get("country_aliases", {}).items()}
    restrictions = cfg.get("non_emea_restrictions", [])
    us_indicators = cfg.get("us_city_state_indicators", [])
    return regions, aliases, restrictions, us_indicators


REGION_KEYWORDS, COUNTRY_ALIASES, NON_EMEA_RESTRICTIONS, US_CITY_STATE_INDICATORS = (
    _load_region_data()
)


class RegionScope:
    """Configures target region, allowed countries, and exclusion rules for job scraping and matching."""

    def __init__(
        self,
        region: str = "EMEA",
        countries: list[str] | None = None,
        strict: bool = True,
    ):
        self.region = region.strip().upper() if region else "EMEA"
        if self.region not in REGION_KEYWORDS and self.region != "CUSTOM":
            self.region = "EMEA"

        # Explicit custom countries / cities
        self.countries = [c.strip().lower() for c in (countries or []) if c.strip()]
        self.strict = strict

        # Build active allowed keywords
        self.allowed_keywords: set[str] = set()
        if self.region in REGION_KEYWORDS:
            self.allowed_keywords.update(REGION_KEYWORDS[self.region])

        for c in self.countries:
            c_low = c.lower()
            self.allowed_keywords.add(c_low)
            if c_low in COUNTRY_ALIASES:
                self.allowed_keywords.update(COUNTRY_ALIASES[c_low])

        # Precompile search regex for performance
        self._allowed_regex: re.Pattern[str] | None = None
        if self.allowed_keywords:
            sorted_kws = sorted(self.allowed_keywords, key=len, reverse=True)
            self._allowed_regex = re.compile(
                r"\b(" + "|".join(re.escape(k) for k in sorted_kws) + r")\b",
                re.IGNORECASE,
            )

    def is_compatible(self, location: str, description: str = "") -> tuple[bool, str]:
        """Validates whether a job position is compatible with the selected region scope."""
        loc_clean = location.strip().lower()
        desc_clean = description.strip().lower()[:2000]

        # 1. If region is GLOBAL and no custom countries specified, accept everything
        if self.region == "GLOBAL" and not self.countries:
            return True, "Global region accepts all locations"

        # 2. If filtering for EMEA, Europe, or Romania, enforce non-EMEA residency restrictions
        if self.strict and self.region in ["EMEA", "EUROPE", "ROMANIA"]:
            combined_text = f"{loc_clean} {desc_clean}"
            for restriction in NON_EMEA_RESTRICTIONS:
                if restriction in combined_text:
                    return False, f"Job has non-EMEA restriction: '{restriction}'"

        # 3. Match against allowed regional keywords
        if self._allowed_regex and self._allowed_regex.search(loc_clean):
            return True, f"Location matches target region ({self.region})"

        # 4. If USA region selected, allow US state/city indicators
        if self.region == "USA":
            if any(us_ind in loc_clean for us_ind in US_CITY_STATE_INDICATORS):
                return True, "Matches US location indicator"
            return False, f"Location '{location}' does not match US criteria"

        # 5. For non-US regions, reject US city/state indicators
        if self.region in [
            "EMEA",
            "EUROPE",
            "ROMANIA",
        ] and any(us_ind in loc_clean for us_ind in US_CITY_STATE_INDICATORS):
            return False, f"Location is in the United States ({loc_clean})"

        # 6. Handle generic 'Remote'
        if "remote" in loc_clean or loc_clean in [
            "",
            "any",
            "telework",
            "work from home",
        ]:
            # If EMEA/Europe, verify description doesn't contradict
            if self.region in ["EMEA", "EUROPE"]:
                if re.search(
                    r"\b(emea|europe|worldwide|global|romania)\b", desc_clean, re.I
                ):
                    return True, "Remote position open to target region"
                return True, "Generic remote without regional restrictions"
            elif self.region == "ROMANIA":
                if re.search(
                    r"\b(romania|românia|bucharest|bucuresti)\b", desc_clean, re.I
                ):
                    return True, "Remote position in Romania"
                return False, "Remote position is not verified for Romania"

            return True, "Remote position accepted"

        # 7. Fallback check: does description explicitly name allowed hubs?
        if self._allowed_regex and self._allowed_regex.search(desc_clean):
            return True, "Description confirms target region eligibility"

        return (
            False,
            f"Location '{location}' is outside the selected region ({self.region})",
        )

    def get_jobspy_country_code(self, default_loc: str = "") -> str:
        """Returns the best country domain code for Indeed and Glassdoor."""
        if self.region == "ROMANIA":
            return "romania"
        if self.region == "USA":
            return "usa"
        if self.region in ["EMEA", "EUROPE"]:
            return "uk"
        return "usa"

    def get_jobicy_geo_param(self) -> str:
        """Returns the URL parameter for the Jobicy remote API."""
        if self.region == "EMEA":
            return "&geo=emea"
        if self.region == "EUROPE":
            return "&geo=europe"
        if self.region == "USA":
            return "&geo=usa"
        return ""


def get_default_region_scope() -> RegionScope:
    """Returns the default region scope (EMEA)."""
    return RegionScope(region="EMEA", strict=True)
