"""Deep citations: map each visualized datum back to supporting CT.gov records.

Traceability contract per datum:
- ``citations[].nct_id`` — ClinicalTrials.gov study id
- ``citations[].url`` — canonical study page
- ``citations[].excerpt`` — exact API field paths/values that justify membership
  in that bar / slice / point / edge (no “first unrelated item” fallback)
- ``contributing_count`` — full bucket size; citations are a sample of those IDs
"""

from __future__ import annotations

from typing import Any, Optional

from app.engine.study_fields import (
    countries_match,
    get_brief_title,
    get_nct_id,
    normalize_drug_name,
)
from app.schemas.output import Citation

CTGOV_STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"


# --- Small field readers from CT.gov protocol JSON ---

def study_url(nct_id: str) -> str:
    """Canonical ClinicalTrials.gov study page URL."""
    return CTGOV_STUDY_URL.format(nct_id=(nct_id or "").strip())


def _protocol(study: dict) -> dict:
    """Return protocolSection dict or empty."""
    return study.get("protocolSection", {}) or {}


def _brief_title(study: dict) -> str:
    """Study brief title from identification module."""
    return get_brief_title(study)


def _overall_status(study: dict) -> str:
    """Overall status string."""
    return (_protocol(study).get("statusModule", {}) or {}).get("overallStatus", "") or ""


def _phases(study: dict) -> list[str]:
    """List of phase strings from the study."""
    phases = (_protocol(study).get("designModule", {}) or {}).get("phases") or []
    return phases if isinstance(phases, list) else ([phases] if phases else [])


def _start_date(study: dict) -> str:
    """Start date string if present."""
    return (
        (_protocol(study).get("statusModule", {}) or {})
        .get("startDateStruct", {})
        or {}
    ).get("date", "") or ""


def _enrollment(study: dict) -> Optional[int]:
    """Enrollment count if present."""
    value = (
        (_protocol(study).get("designModule", {}) or {})
        .get("enrollmentInfo", {})
        or {}
    ).get("count")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sponsor(study: dict) -> str:
    """Lead sponsor name."""
    return (
        (_protocol(study).get("sponsorCollaboratorsModule", {}) or {})
        .get("leadSponsor", {})
        or {}
    ).get("name", "") or ""


def _conditions(study: dict) -> list[str]:
    """Condition names from the study."""
    return (_protocol(study).get("conditionsModule", {}) or {}).get("conditions") or []


def _drugs(study: dict) -> list[str]:
    """Drug intervention names."""
    interventions = (
        (_protocol(study).get("armsInterventionsModule", {}) or {}).get("interventions")
        or []
    )
    return [i.get("name", "") for i in interventions if i.get("name")]


def _countries(study: dict) -> list[str]:
    """Facility country names."""
    locations = (
        (_protocol(study).get("contactsLocationsModule", {}) or {}).get("locations")
        or []
    )
    out = []
    for loc in locations:
        country = (loc.get("country") or "").strip()
        if country and country not in out:
            out.append(country)
    return out


def _investigators(study: dict) -> list[str]:
    """Investigator names."""
    officials = (
        (_protocol(study).get("contactsLocationsModule", {}) or {}).get("overallOfficials")
        or []
    )
    return [p.get("name") for p in officials if p.get("name")]


def _format_phase_api(phases: list[str]) -> str:
    """Format phases for excerpt text."""
    if not phases:
        return "designModule.phases=[]"
    return f"designModule.phases={phases!r}"


# --- Matching helpers (never fall back to values[0]) ---

def _exact_match(values: list[str], target: str, *, normalize=None) -> Optional[str]:
    """Return a grounded match only — never fall back to values[0]."""
    needle = (target or "").strip()
    if not needle:
        return None
    needle_key = normalize(needle) if normalize else needle.casefold()
    for value in values:
        raw = (value or "").strip()
        if not raw:
            continue
        key = normalize(raw) if normalize else raw.casefold()
        if key == needle_key:
            return raw
    return None


def _match_country(values: list[str], target: str) -> Optional[str]:
    """Find country in values matching target aliases."""
    needle = (target or "").strip()
    if not needle:
        return None
    for value in values:
        if countries_match(value, needle):
            return value
    return None


def _match_drug(values: list[str], target: str) -> Optional[str]:
    """Find drug in values matching target."""
    return _exact_match(
        values,
        target,
        normalize=lambda s: normalize_drug_name(s).casefold(),
    )


def _entity_label(raw: str, prefix: str) -> str:
    """Strip typed prefix (Drug:/Sponsor:) from entity labels."""
    text = (raw or "").strip()
    if text.startswith(prefix):
        return text[len(prefix):].strip()
    return text


def _make_citation(nct_id: str, excerpt: str) -> Citation:
    """Build a Citation model with nct_id, url, and excerpt."""
    return Citation(nct_id=nct_id, url=study_url(nct_id), excerpt=excerpt)


# --- Build a field-path excerpt that justifies this study in the datum ---

def build_supporting_excerpt(
    study: dict,
    aggregation: str,
    datum: dict[str, Any],
) -> str:
    """
    Build an excerpt from exact API field values that justify this datum.

    Always includes ``identificationModule.nctId`` and ``briefTitle`` when
    present, plus the field(s) that caused the study to land in this
    bucket / edge / point.
    """
    nct = get_nct_id(study)
    parts: list[str] = []
    if nct:
        parts.append(f"identificationModule.nctId={nct}")
    title = _brief_title(study)
    if title:
        parts.append(f"identificationModule.briefTitle={title!r}")

    if aggregation == "by_year":
        date = _start_date(study)
        if date:
            parts.append(f"statusModule.startDateStruct.date={date}")
    elif aggregation == "by_phase":
        parts.append(_format_phase_api(_phases(study)))
        target = str(datum.get("phase") or datum.get("label") or "")
        if target:
            parts.append(f"bucket.phase={target!r}")
    elif aggregation == "by_phase_group":
        parts.append(_format_phase_api(_phases(study)))
        group = str(datum.get("phase_group") or datum.get("label") or "")
        if group:
            parts.append(f"bucket.phase_group={group!r}")
    elif aggregation == "by_status":
        status = _overall_status(study)
        if status:
            parts.append(f"statusModule.overallStatus={status}")
    elif aggregation == "by_sponsor":
        sponsor = _sponsor(study)
        if sponsor:
            parts.append(f"sponsorCollaboratorsModule.leadSponsor.name={sponsor!r}")
    elif aggregation == "by_condition":
        target = str(datum.get("condition") or datum.get("label") or "")
        match = _exact_match(_conditions(study), target)
        if match:
            parts.append(f"conditionsModule.conditions contains {match!r}")
    elif aggregation == "by_location":
        target = str(datum.get("country") or datum.get("label") or "")
        match = _match_country(_countries(study), target)
        if match:
            parts.append(f"contactsLocationsModule.locations.country={match!r}")
    elif aggregation == "by_drug":
        target = str(datum.get("drug") or datum.get("label") or "")
        match = _match_drug(_drugs(study), target)
        if match:
            parts.append(f"armsInterventionsModule.interventions.name={match!r}")
    elif aggregation == "phase_by_status":
        parts.append(_format_phase_api(_phases(study)))
        status = _overall_status(study)
        if status:
            parts.append(f"statusModule.overallStatus={status}")
    elif aggregation == "phase_by_drug":
        parts.append(_format_phase_api(_phases(study)))
        target = str(datum.get("drug") or datum.get("series") or "")
        match = _match_drug(_drugs(study), target)
        if match:
            parts.append(f"armsInterventionsModule.interventions.name={match!r}")
    elif aggregation == "enrollment_histogram":
        enrollment = _enrollment(study)
        if enrollment is not None:
            parts.append(f"designModule.enrollmentInfo.count={enrollment}")
        bin_label = str(datum.get("enrollment_bin") or datum.get("label") or "")
        if bin_label:
            parts.append(f"bucket.enrollment_bin={bin_label!r}")
    elif aggregation == "enrollment_by_phase_group":
        enrollment = _enrollment(study)
        parts.append(_format_phase_api(_phases(study)))
        if enrollment is not None:
            parts.append(f"designModule.enrollmentInfo.count={enrollment}")
        group = str(datum.get("phase_group") or datum.get("series") or "")
        if group:
            parts.append(f"bucket.phase_group={group!r}")
    elif aggregation == "year_enrollment_scatter":
        date = _start_date(study)
        enrollment = _enrollment(study)
        if date:
            parts.append(f"statusModule.startDateStruct.date={date}")
        if enrollment is not None:
            parts.append(f"designModule.enrollmentInfo.count={enrollment}")
    elif aggregation.endswith("_network"):
        source = str(datum.get("source") or "")
        target = str(datum.get("target") or "")
        for raw in (source, target):
            if raw.startswith("Drug:"):
                match = _match_drug(_drugs(study), _entity_label(raw, "Drug:"))
                if match:
                    parts.append(f"armsInterventionsModule.interventions.name={match!r}")
            elif raw.startswith("Sponsor:"):
                sponsor = _sponsor(study)
                if sponsor:
                    parts.append(
                        f"sponsorCollaboratorsModule.leadSponsor.name={sponsor!r}"
                    )
            elif raw.startswith("Condition:"):
                match = _exact_match(_conditions(study), _entity_label(raw, "Condition:"))
                if match:
                    parts.append(f"conditionsModule.conditions contains {match!r}")
            elif raw.startswith("Site:"):
                match = _match_country(_countries(study), _entity_label(raw, "Site:"))
                if match:
                    parts.append(
                        f"contactsLocationsModule.locations.country={match!r}"
                    )
            elif raw.startswith("Investigator:"):
                match = _exact_match(
                    _investigators(study),
                    _entity_label(raw, "Investigator:"),
                )
                if match:
                    parts.append(
                        f"contactsLocationsModule.overallOfficials.name={match!r}"
                    )
    else:
        status = _overall_status(study)
        if status:
            parts.append(f"statusModule.overallStatus={status}")
        phases = _phases(study)
        if phases:
            parts.append(_format_phase_api(phases))

    return " | ".join(parts) if parts else f"identificationModule.nctId={nct or 'unknown'}"


# --- Sample of NCT citations attached to one chart mark ---

def citations_for_datum(
    study_ids: list[str],
    study_map: dict[str, dict],
    aggregation: str,
    datum: dict[str, Any],
    max_citations: int = 8,
) -> Optional[list[Citation]]:
    """Attach a sample of NCT citations for one aggregated mark.

    ``datum.contributing_count`` (set by aggregators) remains the full bucket
    size; this returns at most ``max_citations`` supporting records.
    """
    if not study_ids:
        return None

    citations: list[Citation] = []
    for nct_id in study_ids[:max_citations]:
        if not nct_id:
            continue
        study = study_map.get(nct_id)
        if not study:
            citations.append(
                _make_citation(
                    nct_id,
                    f"identificationModule.nctId={nct_id} | "
                    "record unavailable in fetched page set",
                )
            )
            continue
        excerpt = build_supporting_excerpt(study, aggregation, datum)
        citations.append(_make_citation(nct_id, excerpt))

    return citations or None
