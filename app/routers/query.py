"""
HTTP orchestration for clinical-trial visualization queries.

Pipeline (each step is deterministic except the two LLM calls):
  1. Validate request + optional cache hit
  2. interpret_query (LLM) → search params + aggregation intent
  3. Fetch studies from ClinicalTrials.gov (paginated / year-bucketed / multi-drug)
  4. Local post-filters (status, phase, country, years, …)
  5. Deterministic aggregate_studies → chart rows + study IDs  (counts NEVER from LLM)
  6. classify_visualization (LLM) → title/notes only
  7. build_response + deep citations + cache store
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from app.api.clinical_trials import ClinicalTrialsAPIError, ClinicalTrialsClient
from app.config import OPENAI_API_KEY, QUERY_CACHE_MAX_ENTRIES, QUERY_CACHE_TTL_SECONDS
from app.engine.aggregator import (
    VALID_AGGREGATIONS,
    aggregate_studies,
    get_field_labels,
    get_field_types,
    get_series_field,
    normalize_country_name,
    resolve_viz_type,
)
from app.engine.builder import build_response
from app.engine.classifier import classify_visualization
from app.engine.interpreter import LLMServiceError, QueryInterpretationError, interpret_query
from app.engine.labels import build_deterministic_encoding
from app.schemas.input import QueryRequest
from app.schemas.output import VisualizationResponse
from app.services.cache import QueryResponseCache
from app.services.fetch import (
    STUDY_FIELDS,
    fetch_studies_for_query,
    resolve_study_fetch_limit,
    resolve_study_fetch_sort,
)
from app.services.filters import (
    apply_structured_filters,
    build_start_date_advanced_filter,
    intervention_filter_key,
)

router = APIRouter(prefix="/api/v1", tags=["query"])

# Process-local only — not shared across workers (documented tradeoff in README).
query_response_cache = QueryResponseCache(
    ttl_seconds=QUERY_CACHE_TTL_SECONDS,
    max_entries=QUERY_CACHE_MAX_ENTRIES,
)


@router.post(
    "/query",
    response_model=VisualizationResponse,
    response_model_exclude_none=True,
    summary="Interpret a clinical-trial question and return a chart-ready visualization",
    response_description="Vega-lite-style visualization spec plus metadata",
)
async def query_clinical_trials(request: QueryRequest):
    # --- Guardrails -----------------------------------------------------------
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY not configured. Set the OPENAI_API_KEY environment variable.",
        )
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=422, detail="query must be a non-empty string.")

    cached = query_response_cache.get(request)
    if cached is not None:
        return cached

    # --- 1) NL → structured interpretation (LLM + heuristics) -----------------
    try:
        interpretation = await interpret_query(request)
    except QueryInterpretationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except LLMServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    search_params = interpretation.get("search_params", {})
    aggregation = interpretation.get("aggregation", "by_status")
    interpreted_start_year = interpretation.get("start_year")
    interpreted_end_year = interpretation.get("end_year")
    interpreted_start_month = interpretation.get("start_month")
    interpreted_sponsor = interpretation.get("sponsor")
    focus_drugs = interpretation.get("focus_drugs") or []
    if not isinstance(focus_drugs, list):
        focus_drugs = []
    # Unknown aggregation from the model → safe default (status overview).
    if aggregation not in VALID_AGGREGATIONS:
        aggregation = "by_status"

    # Structured request fields win over NL/LLM when both are present
    # (e.g. query says "melanoma" but condition="Diabetes" → Diabetes).
    effective_start = request.start_year if request.start_year is not None else interpreted_start_year
    effective_end = request.end_year if request.end_year is not None else interpreted_end_year
    effective_condition = request.condition or search_params.get("cond")
    # "Drug A vs Drug B" must not be treated as a disease condition.
    if effective_condition and re.search(r"\b(?:vs\.?|versus)\b", effective_condition, re.I):
        effective_condition = None
    if len(focus_drugs) >= 2 and not request.condition:
        effective_condition = None
    effective_country = request.country or search_params.get("locn")
    if effective_country:
        effective_country = normalize_country_name(effective_country) or effective_country
        search_params = {**search_params, "locn": effective_country}

    # CT.gov AREA[StartDate] filter + sort/limit tuned per aggregation.
    advanced_filter = build_start_date_advanced_filter(
        effective_start,
        interpreted_start_month if request.start_year is None else None,
        effective_end,
    )
    sort = resolve_study_fetch_sort(aggregation, advanced_filter)
    fetch_limit = resolve_study_fetch_limit(aggregation, request.max_studies)

    # --- 2) Fetch live studies ------------------------------------------------
    ct_client = ClinicalTrialsClient()
    try:
        search_result = await fetch_studies_for_query(
            ct_client,
            search_params=search_params,
            request=request,
            focus_drugs=focus_drugs,
            fields=",".join(STUDY_FIELDS),
            aggregation=aggregation,
            effective_start_year=effective_start,
            effective_end_year=effective_end,
            effective_sponsor=request.sponsor or interpreted_sponsor,
            advanced_filter=advanced_filter,
            sort=sort,
            max_studies=fetch_limit,
        )
    except ClinicalTrialsAPIError as exc:
        status = exc.status_code or 502
        if status == 429:
            raise HTTPException(
                status_code=429,
                detail="ClinicalTrials.gov rate limit hit. Wait a few seconds and try again.",
            ) from exc
        if status == 504:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await ct_client.aclose()

    total_available = search_result.get("totalCount", 0)
    truncated = bool(search_result.get("truncated"))
    fetched_studies = search_result.get("studies", []) or []

    # --- 3) Local safety-net filters (CT.gov text search is fuzzy) ------------
    studies = apply_structured_filters(
        fetched_studies,
        request,
        start_year_override=interpreted_start_year,
        end_year_override=interpreted_end_year,
        start_month_override=interpreted_start_month,
        sponsor_override=interpreted_sponsor,
        condition_override=effective_condition,
        country_override=effective_country,
    )
    total_count = len(studies)

    if not studies:
        # Fetched something but filters wiped it → clarify (422), not a bare 404.
        if fetched_studies:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No trials remained after applying filters "
                    f"(fetched {len(fetched_studies)} studies from ClinicalTrials.gov). "
                    "Try relaxing status, phase, sponsor, country, or year bounds."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="No clinical trials found matching the query.",
        )

    # --- 4) Deterministic aggregation (bar heights / edge weights) ------------
    aggregated_data = aggregate_studies(
        studies,
        aggregation,
        focus_drugs=focus_drugs or None,
        country_filter=effective_country,
    )
    if not aggregated_data:
        raise HTTPException(
            status_code=422,
            detail=(
                "Trials were found, but none could be visualized with the selected "
                f"aggregation ({aggregation}). Try a different grouping or broader filters."
            ),
        )

    viz_type = resolve_viz_type(aggregation, interpretation.get("viz_type"))
    x_field, y_field = get_field_labels(aggregation)
    x_type, y_type = get_field_types(aggregation)
    series_field = get_series_field(aggregation)

    # Meta.filters mirrors what actually shaped the chart (for UI + grounding).
    filters_applied: dict = {}
    if focus_drugs:
        filters_applied["drugs"] = focus_drugs
    elif request.drug_name:
        filters_applied[intervention_filter_key(request.drug_name)] = request.drug_name
    elif search_params.get("intr"):
        intr_value = search_params["intr"]
        filters_applied[intervention_filter_key(intr_value)] = intr_value
    if request.condition:
        filters_applied["condition"] = request.condition
    elif search_params.get("cond"):
        filters_applied["condition"] = search_params["cond"]
    if request.country:
        filters_applied["country"] = request.country
    elif search_params.get("locn"):
        filters_applied["country"] = search_params["locn"]
    if request.status:
        filters_applied["status"] = request.status
    elif search_params.get("status"):
        filters_applied["status"] = search_params["status"]
    if request.trial_phase:
        filters_applied["phase"] = request.trial_phase
    effective_sponsor = request.sponsor or interpreted_sponsor
    if effective_sponsor:
        filters_applied["sponsor"] = effective_sponsor
    if effective_start is not None:
        filters_applied["start_year"] = effective_start
    if interpreted_start_month is not None and request.start_year is None:
        filters_applied["start_month"] = interpreted_start_month
    if effective_end is not None:
        filters_applied["end_year"] = effective_end
    if request.max_studies is not None:
        filters_applied["max_studies"] = request.max_studies

    # --- 5) Title / notes only (LLM); ungrounded entities rejected in labels ---
    try:
        viz_refinement = await classify_visualization(
            query=request.query,
            interpretation=interpretation,
            aggregated_data=aggregated_data,
            viz_type=viz_type,
            x_field=x_field,
            y_field=y_field,
            x_type=x_type,
            y_type=y_type,
            total_count=total_count,
            filters_applied=filters_applied,
        )
    except LLMServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # Encoding channels always come from aggregation maps — ignore any LLM fields.
    encoding = build_deterministic_encoding(
        viz_type=viz_type,
        x_field=x_field,
        y_field=y_field,
        x_type=x_type,
        y_type=y_type,
        series_field=series_field,
    )

    # --- 6) Assemble response + citations ------------------------------------
    response = build_response(
        aggregated_data=aggregated_data,
        studies=studies,
        viz_type=viz_type,
        title=viz_refinement.get("title", f"Clinical Trials {aggregation.replace('_', ' ').title()}"),
        encoding=encoding,
        x_field=x_field,
        y_field=y_field,
        series_field=series_field,
        x_type=x_type,
        y_type=y_type,
        total_count=total_count,
        filters_applied=filters_applied,
        aggregation=aggregation,
        notes=viz_refinement.get("meta", {}).get("notes", viz_refinement.get("notes")),
        total_available=total_available,
        truncated=truncated,
    )
    query_response_cache.set(request, response)
    return response
