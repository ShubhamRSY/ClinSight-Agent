"""Post-fetch study filtering and small filter helpers.

CT.gov query.* / advanced filters are imperfect (fuzzy text match). After fetch we
re-check status, phase, sponsor, condition, country, and year bounds locally so
the chart matches what the user asked for.
"""

from __future__ import annotations

import re
from typing import Optional

from app.engine.aggregator import (
    extract_date_parts,
    extract_field,
    study_matches_condition_filter,
    study_matches_country_filter,
)
from app.schemas.input import PHASE_ALIASES, QueryRequest, normalize_phase_value, normalize_status_value

# Words that mean a modality/procedure, not a drug name (for meta.filters labels).
_MODALITY_KEYWORDS = (
    "radiation",
    "radiotherapy",
    "radiosurgery",
    "chemotherapy",
    "immunotherapy",
    "phototherapy",
    "hyperthermia",
    "surgery",
    "transplant",
    "transplantation",
    "dialysis",
    "exercise",
    "behavioral",
    "psychotherapy",
    "counseling",
    "education",
    "screening",
    "diagnostic",
    "imaging",
    "tomography",
    "ultrasound",
    "biopsy",
    "placebo",
)


def is_modality_intervention(name: str) -> bool:
    lower = (name or "").strip().lower()
    if not lower:
        return False
    return any(term in lower for term in _MODALITY_KEYWORDS)


def intervention_filter_key(name: str) -> str:
    return "intervention" if is_modality_intervention(name) else "drug_name"


def normalize_phase(value: str) -> str:
    """Normalize phase labels; prefer shared aliases, fall back to CT.gov-style enum."""
    key = value.strip().lower().replace("_", " ")
    if key in PHASE_ALIASES:
        return PHASE_ALIASES[key]
    try:
        return normalize_phase_value(value)
    except ValueError:
        return value.strip().upper().replace(" ", "")


def build_start_date_advanced_filter(
    start_year: Optional[int],
    start_month: Optional[int] = None,
    end_year: Optional[int] = None,
) -> Optional[str]:
    """CT.gov Essie filter for study start date range."""
    if start_year is None and end_year is None:
        return None
    start = f"{start_year:04d}-{(start_month or 1):02d}-01" if start_year is not None else "MIN"
    end = f"{end_year:04d}-12-31" if end_year is not None else "MAX"
    return f"AREA[StartDate]RANGE[{start},{end}]"


# --- Post-fetch safety net: keep only studies that match structured + NL filters ---

def apply_structured_filters(
    studies: list[dict],
    request: QueryRequest,
    start_year_override: Optional[int] = None,
    end_year_override: Optional[int] = None,
    start_month_override: Optional[int] = None,
    sponsor_override: Optional[str] = None,
    condition_override: Optional[str] = None,
    country_override: Optional[str] = None,
) -> list[dict]:
    """Apply structured fields that CT.gov query params may not fully cover.

    Year bounds may come from NL interpretation (e.g. "since 2015"). Explicit
    request.start_year / request.end_year always win when set.
    """
    filtered = studies

    # Condition (skip "Drug A vs Drug B" false positives).
    condition_filter = request.condition or condition_override
    if condition_filter and re.search(r"\b(?:vs\.?|versus)\b", condition_filter, re.I):
        condition_filter = None
    if condition_filter:
        filtered = [
            s for s in filtered
            if study_matches_condition_filter(s, condition_filter)
        ]

    # Country / site location.
    country_filter = request.country or country_override
    if country_filter:
        filtered = [
            s for s in filtered
            if study_matches_country_filter(s, country_filter)
        ]

    # Trial phase enum match.
    if request.trial_phase:
        target = normalize_phase(request.trial_phase)
        next_studies = []
        for s in filtered:
            phases = extract_field(s, "designModule", "phases")
            phase_list = phases if isinstance(phases, list) else ([phases] if phases else [])
            normalized = {normalize_phase(p) for p in phase_list if p}
            if target in normalized:
                next_studies.append(s)
        filtered = next_studies

    status_filter = (request.status or "").strip()
    # Overall status (comma-lists allowed after schema normalization).
    if status_filter:
        try:
            target_status = normalize_status_value(status_filter)
        except ValueError:
            target_status = status_filter.strip().upper().replace(" ", "_")
        next_studies = []
        for s in filtered:
            raw = extract_field(s, "statusModule", "overallStatus") or ""
            study_status = str(raw).strip().upper().replace(" ", "_")
            if study_status == target_status:
                next_studies.append(s)
        filtered = next_studies

    sponsor_needle = (request.sponsor or sponsor_override or "").strip().lower()
    # Lead sponsor substring match (request wins, else NL override).
    if sponsor_needle:
        filtered = [
            s for s in filtered
            if sponsor_needle in (
                extract_field(s, "sponsorCollaboratorsModule", "leadSponsor", "name") or ""
            ).lower()
        ]

    # Start/end year (+ optional month) from structured request or NL interpretation.
    start_yr = request.start_year if request.start_year is not None else start_year_override
    end_yr = request.end_year if request.end_year is not None else end_year_override
    if start_yr is not None or end_yr is not None or start_month_override is not None:
        next_studies = []
        for s in filtered:
            year, month = extract_date_parts(s, "statusModule", "startDateStruct", "date")
            if year is None:
                continue
            if start_yr is not None and year < start_yr:
                continue
            if (
                start_yr is not None
                and start_month_override is not None
                and year == start_yr
                and month is not None
                and month < start_month_override
            ):
                continue
            if end_yr is not None and year > end_yr:
                continue
            next_studies.append(s)
        filtered = next_studies

    return filtered
