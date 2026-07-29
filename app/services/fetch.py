"""ClinicalTrials.gov fetch strategies (pagination, year buckets, multi-drug)."""

from __future__ import annotations

import asyncio
from typing import Optional

from app.api.clinical_trials import ClinicalTrialsClient
from app.config import get_reference_date
from app.engine.aggregator import get_nct_id, normalize_country_name
from app.schemas.input import QueryRequest
from app.services.filters import build_start_date_advanced_filter, normalize_phase

# Aggregations that bucket by start year need full historical coverage, not newest-first pages.
TREND_AGGREGATIONS = frozenset({"by_year", "year_enrollment_scatter"})
TREND_FETCH_CAP = 5000
TREND_PER_YEAR_CAP = 5000
YEAR_BUCKET_PAUSE_SECONDS = 0.25
MULTI_DRUG_PAUSE_SECONDS = 0.75

STUDY_FIELDS = (
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.designModule.phases",
    "protocolSection.designModule.enrollmentInfo.count",
    "protocolSection.statusModule.startDateStruct.date",
    "protocolSection.conditionsModule.conditions",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
    "protocolSection.armsInterventionsModule.interventions.name",
    "protocolSection.armsInterventionsModule.interventions.type",
    "protocolSection.contactsLocationsModule.locations.country",
    "protocolSection.contactsLocationsModule.overallOfficials.name",
)


def resolve_study_fetch_sort(aggregation: str, advanced_filter: Optional[str]) -> Optional[str]:
    """Trend charts must not use StartDate:desc — that drops older years under pagination caps."""
    if aggregation in TREND_AGGREGATIONS:
        return None
    return "StartDate:desc" if advanced_filter else None


def resolve_study_fetch_limit(aggregation: str, request_max: Optional[int]) -> Optional[int]:
    if request_max is not None:
        return request_max
    if aggregation in TREND_AGGREGATIONS:
        return TREND_FETCH_CAP
    return None


def trend_end_year(effective_end: Optional[int]) -> int:
    if effective_end is not None:
        return effective_end
    return get_reference_date().year + 1


async def fetch_studies_year_bucketed(
    ct_client: ClinicalTrialsClient,
    *,
    common: dict,
    intr: Optional[str],
    start_year: int,
    end_year: int,
    per_year_cap: int,
) -> dict:
    """Fetch one CT.gov page set per calendar year so trends are not biased by global sort."""
    merged: list[dict] = []
    seen: set[str] = set()
    total_available = 0
    any_truncated = False

    for i, year in enumerate(range(start_year, end_year + 1)):
        if i > 0:
            await asyncio.sleep(YEAR_BUCKET_PAUSE_SECONDS)
        year_filter = build_start_date_advanced_filter(year, 1, year)
        page = await ct_client.search_studies_paginated(
            **common,
            intr=intr,
            advanced_filter=year_filter,
            sort=None,
            max_studies=per_year_cap,
        )
        total_available += int(page.get("totalCount") or 0)
        any_truncated = any_truncated or bool(page.get("truncated"))
        for study in page.get("studies") or []:
            nct = get_nct_id(study)
            key = nct or str(id(study))
            if key in seen:
                continue
            seen.add(key)
            merged.append(study)

    return {
        "studies": merged,
        "totalCount": total_available,
        "fetchedCount": len(merged),
        "truncated": any_truncated,
    }


async def fetch_studies_for_query(
    ct_client: ClinicalTrialsClient,
    *,
    search_params: dict,
    request: QueryRequest,
    focus_drugs: list[str],
    fields: str,
    aggregation: str,
    effective_start_year: Optional[int] = None,
    effective_end_year: Optional[int] = None,
    effective_sponsor: Optional[str] = None,
    advanced_filter: Optional[str] = None,
    sort: Optional[str] = None,
    max_studies: Optional[int] = None,
) -> dict:
    """Fetch CT.gov studies; for Drug A vs Drug B, page each drug separately then merge."""
    common = {
        "term": search_params.get("term"),
        "cond": search_params.get("cond") or request.condition,
        "locn": search_params.get("locn") or request.country,
        "status": search_params.get("status") or request.status,
        "fields": fields,
        # Push lead sponsor to CT.gov so pagination isn't wasted on non-matching studies.
        "lead": (effective_sponsor or request.sponsor or "").strip() or None,
        # Push phase when explicitly requested so post-filter discard is minimized.
        "phase": request.trial_phase,
    }
    # Prefer canonical country names for CT.gov location search (usa → United States).
    if common.get("locn"):
        common["locn"] = normalize_country_name(common["locn"]) or common["locn"]
    if common.get("phase"):
        common["phase"] = normalize_phase(common["phase"])
    intr = search_params.get("intr") or request.drug_name
    fetch_limit = max_studies if max_studies is not None else request.max_studies

    if (
        aggregation in TREND_AGGREGATIONS
        and effective_start_year is not None
        and len(focus_drugs) < 2
    ):
        end_year = trend_end_year(effective_end_year)
        if effective_start_year <= end_year:
            per_year_cap = min(
                TREND_PER_YEAR_CAP,
                fetch_limit if fetch_limit is not None else TREND_PER_YEAR_CAP,
            )
            return await fetch_studies_year_bucketed(
                ct_client,
                common=common,
                intr=intr,
                start_year=effective_start_year,
                end_year=end_year,
                per_year_cap=per_year_cap,
            )

    if len(focus_drugs) >= 2:
        # Split the page budget so the rarer drug is not crowded out.
        limit = fetch_limit if fetch_limit is not None else request.max_studies
        per_drug = max((limit or 1000) // len(focus_drugs), 200)
        merged: list[dict] = []
        seen: set[str] = set()
        total_available = 0
        any_truncated = False
        drug_common = dict(common)
        if not request.condition:
            drug_common.pop("cond", None)
        for i, drug in enumerate(focus_drugs):
            if i > 0:
                # Pause between drug fetches to avoid CT.gov 429 rate limits.
                await asyncio.sleep(MULTI_DRUG_PAUSE_SECONDS)
            page = await ct_client.search_studies_paginated(
                **drug_common,
                intr=drug,
                max_studies=per_drug,
                advanced_filter=advanced_filter,
                sort=sort,
            )
            total_available += int(page.get("totalCount") or 0)
            any_truncated = any_truncated or bool(page.get("truncated"))
            for study in page.get("studies") or []:
                nct = get_nct_id(study)
                key = nct or str(id(study))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(study)
        return {
            "studies": merged,
            "totalCount": total_available,
            "fetchedCount": len(merged),
            "truncated": any_truncated,
        }

    return await ct_client.search_studies_paginated(
        **common,
        intr=intr,
        max_studies=fetch_limit,
        advanced_filter=advanced_filter,
        sort=sort,
    )
