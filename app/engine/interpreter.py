"""LLM query interpreter: OpenAI call + enum validation + heuristic enrichment.

Natural-language heuristics live in ``heuristics``; this module owns the prompt,
OpenAI client call, and allow-list clamping of model JSON.
"""

from __future__ import annotations

import json
import re

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import OPENAI_API_KEY, OPENAI_MODEL, get_reference_date
from app.engine.heuristics import (
    _UNCLEAR_QUERY_MESSAGE,
    _VAGUE_QUERY_MESSAGE,
    _allows_unfiltered_overview,
    _apply_query_heuristics,
    _coerce_year,
    _enrich_search_params_from_text,
    _extract_compared_drugs,
    _extract_condition_from_text,
    _extract_status_from_text,
    _extract_temporal_bounds,
    _extract_years_from_text,
    _has_search_filters,
    _has_structured_request_filters,
    _has_trial_intent,
    _maybe_infer_single_token_search,
    _tokenize_query,
)
from app.engine.study_fields import normalize_country_name
from app.engine.viz_maps import ALLOWED_VIZ_TYPES, VALID_AGGREGATIONS
from app.schemas.input import QueryRequest, VALID_STATUSES


class LLMServiceError(Exception):
    """Raised when the OpenAI call fails in a user-visible way."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class QueryInterpretationError(Exception):
    """Raised when the query cannot be mapped to a scoped clinical-trials search."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 422


INTERPRETER_SYSTEM_PROMPT = """You are a clinical trial query interpreter. Analyze the user's question and extract structured search parameters.

This is a visualization system for clinical trials on ClinicalTrials.gov.

Return JSON with:
- search_params: object with any of {term, cond, intr, locn, status}
- focus_drugs: optional array of drug names when comparing specific interventions (e.g. ["Drug A", "Drug B"]). For "Drug A vs Drug B", include BOTH names and set intr to "Drug A OR Drug B".
- start_year: integer year extracted from temporal phrases like "since 2015", "after 2020", "from 2010 onwards", "in the last 5 years" or "in the last 6 months" (compute relative to REFERENCE_DATE). Omit if no temporal bound is mentioned.
- start_month: optional integer month (1–12) when a phrase like "last N months" implies a month-level cutoff within start_year.
- end_year: integer year extracted from "before 2018", "up to 2022", "between 2015 and 2023" (end of range). Omit if no upper bound.
- intent: short description of what the user wants to see
- aggregation: how to shape the data — one of:
  "by_year" (count trials per year / trends over time),
  "by_phase" (count trials per phase),
  "by_phase_group" (count trials in early vs late phase buckets),
  "by_status" (count by trial status),
  "by_condition" (count by medical condition),
  "by_sponsor" (count by lead sponsor),
  "by_location" (count by country),
  "by_drug" (count by intervention/drug),
  "phase_by_status" (phase crossed with status — for grouped/stacked bars),
  "phase_by_drug" (phase crossed with drug — compare phases across drugs),
  "enrollment_histogram" (distribution of enrollment sizes),
  "enrollment_by_phase_group" (enrollment size bins compared for early vs late phase trials),
  "year_enrollment_scatter" (year vs enrollment scatter),
  "drug_sponsor_network" (network edges between drugs and sponsors),
  "drug_condition_network" (network edges between drugs and conditions),
  "sponsor_condition_network" (network edges between sponsors and conditions),
  "sponsor_site_network" (network edges between sponsors and countries/sites),
  "drug_investigator_network" (network edges between drugs and investigators),
  "sponsor_investigator_network" (network edges between sponsors and investigators),
  "drug_drug_network" (network edges between co-occurring drugs in the same trial)
- viz_type: preferred visualization — one of:
  "bar_chart", "grouped_bar_chart", "time_series", "scatter_plot", "histogram",
  "network_graph", "pie_chart", "stacked_bar_chart", "table"
- needs_visualization: true when the user asks a recognizable clinical-trials question; false for gibberish, personal names, greetings, or unrelated text
- rejection_reason: when needs_visualization is false, a short user-facing explanation (omit when true)

Only include search_params that are explicitly mentioned or clearly implied. Never invent drug, condition, or sponsor filters — omit intr/cond unless the user named them.
Map user-friendly phase names to API format: "Phase 1"→"PHASE1", "Phase 2"→"PHASE2", "Phase 3"→"PHASE3", "Phase 4"→"PHASE4".
Map statuses: "Recruiting"→"RECRUITING", "Completed"→"COMPLETED", "Active, not recruiting"→"ACTIVE_NOT_RECRUITING", "Terminated"→"TERMINATED".
Never set status to ALL or any value outside that list. If the user does not specify status, omit status.

Temporal guidance:
- "since 2015" / "after 2020" → set start_year: 2015 (or the relevant year)
- "before 2018" / "up to 2022" → set end_year: 2018 (or the relevant year)
- "between 2015 and 2023" / "from 2015 to 2023" → start_year: 2015, end_year: 2023
- "in the last X years" → compute start_year = REFERENCE_DATE.year - X
- "in the last X months" → compute start_year and start_month from REFERENCE_DATE minus X months
- "in the last X weeks" → approximate from REFERENCE_DATE
- "recent years" / "last decade" → approximate from context
- Omit start_year/end_year when no temporal bound is mentioned.

Selection guidance:
- "over time" / "trend" / "timeline" / "per year" / "each year" → aggregation by_year, viz_type time_series
- "distribution" / "breakdown" of phases → by_phase + bar_chart
- "most common interventions/drugs" → by_drug + bar_chart
- "proportion" / "share" / "percentage" → pie_chart with categorical aggregation
- "phase by status" → phase_by_status + grouped_bar_chart
- "compare phases" for Drug A vs Drug B → phase_by_drug + grouped_bar_chart, focus_drugs=[A,B], intr="A OR B"
- "compare sponsors" across conditions → sponsor_condition_network + network_graph
- "enrollment distribution" / "histogram of enrollment" / "enrollment sizes" → enrollment_histogram + histogram
- "early vs late phase" / "early phase vs late phase" → by_phase_group + bar_chart (NOT phase_by_drug; do NOT invent a drug)
- "drug network" / "drug-to-drug network" for a condition → drug_drug_network + network_graph, set cond only
- "trials sponsored by X" → by_status or by_sponsor, extract sponsor from text (post-filter), do NOT invent drugs
- "enrollment vs year" / "scatter" → year_enrollment_scatter + scatter_plot
- "network of sponsors and drugs" / "sponsors + drugs" → drug_sponsor_network + network_graph
- "co-occur" / "combination" / "drug-to-drug" / "drug + drug" → drug_drug_network + network_graph
- "investigator" relationships → drug_investigator_network or sponsor_investigator_network
- Geographic / countries → by_location + bar_chart
- Generic questions without explicit grouping → by_status + pie_chart ONLY when the user clearly asks for a global status overview; otherwise require search_params

Output ONLY a valid JSON object, no other text."""


def validate_actionable_query(
    query: str,
    request: QueryRequest,
    interpretation: dict,
) -> None:
    """Reject queries that would trigger an unscoped fetch of all CT.gov studies."""
    if not interpretation.get("needs_visualization", True):
        reason = interpretation.get("rejection_reason") or _UNCLEAR_QUERY_MESSAGE
        raise QueryInterpretationError(reason)

    search_params = dict(interpretation.get("search_params") or {})
    if _has_structured_request_filters(request) or _has_search_filters(search_params, interpretation):
        return

    inferred = _maybe_infer_single_token_search(query, search_params)
    if inferred != search_params:
        interpretation["search_params"] = inferred
        return

    if _allows_unfiltered_overview(query):
        return

    tokens = _tokenize_query(query)
    if len(tokens) <= 2 and not _has_trial_intent(query):
        raise QueryInterpretationError(_UNCLEAR_QUERY_MESSAGE)

    raise QueryInterpretationError(_VAGUE_QUERY_MESSAGE)


def _validate_interpretation(result: dict) -> dict:
    """Constrain LLM output to known enums to avoid hallucination-prone free text."""
    if not isinstance(result, dict):
        return {
            "search_params": {},
            "intent": "fallback status breakdown",
            "aggregation": "by_status",
            "viz_type": "pie_chart",
            "needs_visualization": True,
            "start_year": None,
            "end_year": None,
            "start_month": None,
            "focus_drugs": None,
        }

    search_params = result.get("search_params") or {}
    if not isinstance(search_params, dict):
        search_params = {}
    cleaned_params = {}
    for key in ("term", "cond", "intr", "locn", "status"):
        value = search_params.get(key)
        if isinstance(value, str) and value.strip():
            cleaned_params[key] = value.strip()

    aggregation = result.get("aggregation", "by_status")
    if aggregation not in VALID_AGGREGATIONS:
        aggregation = "by_status"

    viz_type = result.get("viz_type")
    if viz_type not in ALLOWED_VIZ_TYPES:
        viz_type = None

    focus_raw = result.get("focus_drugs") or result.get("drugs")
    focus_drugs: list[str] = []
    if isinstance(focus_raw, list):
        seen: set[str] = set()
        for item in focus_raw:
            name = str(item or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            focus_drugs.append(name)

    start_month = result.get("start_month")
    if isinstance(start_month, (int, float)):
        start_month = int(start_month)
        if start_month < 1 or start_month > 12:
            start_month = None
    else:
        start_month = None

    needs_raw = result.get("needs_visualization", True)
    if isinstance(needs_raw, bool):
        needs_visualization = needs_raw
    else:
        # Non-bool garbage → prefer reject/clarify over silent visualize.
        needs_visualization = False

    rejection_reason = str(result.get("rejection_reason") or "").strip() or None

    return {
        "search_params": cleaned_params,
        "intent": str(result.get("intent") or "").strip() or "clinical trials visualization",
        "aggregation": aggregation,
        "viz_type": viz_type,
        "needs_visualization": needs_visualization,
        "rejection_reason": rejection_reason,
        "start_year": _coerce_year(result.get("start_year")),
        "end_year": _coerce_year(result.get("end_year")),
        "start_month": start_month,
        "focus_drugs": focus_drugs or None,
    }


async def interpret_query(request: QueryRequest) -> dict:
    """
    NL question → search params + aggregation.

    Flow: OpenAI JSON → clamp enums → heuristics (temporal/entities/intent)
    → structured request fields override NL/LLM → actionable-query gate.
    """
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # Include optional structured filters in the prompt so the model sees them.
    user_message = f"Query: {request.query}\n"
    if request.drug_name:
        user_message += f"Drug/Intervention: {request.drug_name}\n"
    if request.condition:
        user_message += f"Condition: {request.condition}\n"
    if request.trial_phase:
        user_message += f"Phase: {request.trial_phase}\n"
    if request.sponsor:
        user_message += f"Sponsor: {request.sponsor}\n"
    if request.country:
        user_message += f"Country: {request.country}\n"
    if request.start_year:
        user_message += f"Start Year: {request.start_year}\n"
    if request.end_year:
        user_message += f"End Year: {request.end_year}\n"
    if request.status:
        user_message += f"Status: {request.status}\n"

    try:
        ref = get_reference_date().isoformat()
        system_prompt = INTERPRETER_SYSTEM_PROMPT.replace("REFERENCE_DATE", ref)
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
    except RateLimitError as exc:
        raise LLMServiceError("OpenAI rate limit exceeded. Try again shortly.", 429) from exc
    except APITimeoutError as exc:
        raise LLMServiceError("OpenAI request timed out.", 504) from exc
    except (APIError, json.JSONDecodeError) as exc:
        raise LLMServiceError(f"OpenAI interpretation failed: {exc}", 502) from exc

    interpretation = _apply_query_heuristics(request.query, _validate_interpretation(result))
    _enrich_search_params_from_text(request.query, request, interpretation)
    # Explicit structured request fields always win over NL/LLM extraction
    # (conflict example: query "melanoma…" + condition="Diabetes" → Diabetes).
    params = dict(interpretation.get("search_params") or {})
    if request.drug_name:
        params["intr"] = request.drug_name
    if request.condition:
        params["cond"] = request.condition
    if request.sponsor:
        interpretation["sponsor"] = request.sponsor
    if request.country:
        # Explicit country filter always wins over any LLM-invented locn.
        params["locn"] = normalize_country_name(request.country) or request.country
    interpretation["search_params"] = params
    if request.start_year is not None:
        interpretation["start_year"] = request.start_year
    if request.end_year is not None:
        interpretation["end_year"] = request.end_year
    if request.status:
        # Already normalized/validated on QueryRequest (rejects unknown status enums).
        params = dict(interpretation.get("search_params") or {})
        params["status"] = request.status
        interpretation["search_params"] = params
    if request.trial_phase:
        # Keep structured phase on the interpretation for downstream notes/filters.
        interpretation["trial_phase"] = request.trial_phase
    validate_actionable_query(request.query, request, interpretation)
    return interpretation
