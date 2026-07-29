"""Deterministic NL heuristics for clinical-trial query interpretation.

Extracts temporal bounds, conditions, sponsors, and compared drugs from query
text; strips LLM-invented entities that are not grounded in the query or
structured request. Used by ``interpret_query`` after the OpenAI call.
"""

from __future__ import annotations

import re
from datetime import date

from app.config import get_reference_date
from app.engine.study_fields import normalize_condition_name, normalize_country_name
from app.schemas.input import QueryRequest, VALID_STATUSES

_UNCLEAR_QUERY_MESSAGE = (
    "I couldn't interpret that as a clinical trials question. "
    "Try mentioning a drug, condition, sponsor, or what you'd like to visualize "
    "(e.g. 'Drug X trials by phase' or 'recruiting lung cancer trials by country')."
)

_VAGUE_QUERY_MESSAGE = (
    "Your question needs a clearer search target. "
    "Specify a drug, condition, sponsor, or geographic filter, "
    "or ask for an explicit overview like 'distribution of trial statuses'."
)

_TRIAL_INTENT_MARKERS = (
    "trial",
    "trials",
    "study",
    "studies",
    "clinical",
    "phase",
    "drug",
    "drugs",
    "intervention",
    "sponsor",
    "condition",
    "disease",
    "cancer",
    "enrollment",
    "recruiting",
    "completed",
    "status",
    "network",
    "compare",
    "distribution",
    "histogram",
    "trend",
    "timeline",
    "country",
    "countries",
    "geographic",
    "therapy",
    "treatment",
    "vaccine",
    "patients",
    "oncology",
)

_UNFILTERED_OVERVIEW_PHRASES = (
    "all clinical trials",
    "all trials",
    "overall trial",
    "trial status",
    "status distribution",
    "distribution of trial",
    "trials by status",
    "breakdown by status",
    "overview of trials",
)

_CONDITION_KEYWORDS: list[tuple[str, str]] = sorted(
    [
        ("non-small cell lung cancer", "Non-small Cell Lung Cancer"),
        ("non small cell lung cancer", "Non-small Cell Lung Cancer"),
        ("chronic lymphocytic leukemia", "Chronic Lymphocytic Leukemia"),
        ("small cell lung cancer", "Small Cell Lung Cancer"),
        ("glioblastoma multiforme", "Glioblastoma"),
        ("glioblastoma", "Glioblastoma"),
        ("melanoma", "Melanoma"),
        ("diabetes", "Diabetes"),
        ("nsclc", "Non-small Cell Lung Cancer"),
        ("sclc", "Small Cell Lung Cancer"),
        ("cll", "Chronic Lymphocytic Leukemia"),
        ("aml", "Acute Myeloid Leukemia"),
        ("copd", "Chronic Obstructive Pulmonary Disease"),
        ("hiv", "Human Immunodeficiency Virus"),
    ],
    key=lambda item: len(item[0]),
    reverse=True,
)

_DRUG_LIKE_SUFFIXES = (
    "mab",
    "nib",
    "tinib",
    "ciclib",
    "vir",
    "pril",
    "sartan",
    "statin",
    "cillin",
    "mycin",
    "zumab",
    "umab",
    "ximab",
    "limab",
    "parin",
)


def _tokenize_query(query: str) -> list[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z'-]*", query or "")


def _looks_like_drug_token(token: str) -> bool:
    t = token.lower()
    if len(t) < 5:
        return False
    return any(t.endswith(suffix) for suffix in _DRUG_LIKE_SUFFIXES)


def _has_trial_intent(query: str) -> bool:
    q = (query or "").lower()
    return any(marker in q for marker in _TRIAL_INTENT_MARKERS)


def _allows_unfiltered_overview(query: str) -> bool:
    q = (query or "").lower()
    return any(phrase in q for phrase in _UNFILTERED_OVERVIEW_PHRASES)


def _has_structured_request_filters(request: QueryRequest) -> bool:
    return any(
        (
            request.drug_name,
            request.condition,
            request.sponsor,
            request.country,
            request.trial_phase,
            request.status,
        )
    )


def _has_search_filters(search_params: dict, interpretation: dict) -> bool:
    if any(search_params.get(k) for k in ("term", "cond", "intr", "locn", "status")):
        return True
    if interpretation.get("sponsor"):
        return True
    return bool(interpretation.get("focus_drugs"))


def _maybe_infer_single_token_search(query: str, search_params: dict) -> dict:
    """Treat a lone drug-like token as an intervention search."""
    if _has_search_filters(search_params, {}):
        return search_params
    tokens = _tokenize_query(query)
    if len(tokens) != 1:
        return search_params
    token = tokens[0]
    if _looks_like_drug_token(token):
        return {**search_params, "intr": token}
    return search_params


def _reference_date() -> date:
    return get_reference_date()


def _subtract_months(ref: date, months: int) -> tuple[int, int]:
    """Start of an inclusive calendar-month window ending in ref's month."""
    months = max(1, months)
    year = ref.year
    month = ref.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return year, month


def _extract_temporal_bounds(text: str) -> tuple[int | None, int | None, int | None]:
    """Extract (start_year, end_year, start_month) from temporal phrases in text."""
    q = (text or "").lower()
    ref = _reference_date()

    # "in the last N months" / "past 6 months"
    m = re.search(r"(?:last|past|recent)\s+(\d+)\s*(?:month|mo)s?", q)
    if m:
        n = int(m.group(1))
        year, month = _subtract_months(ref, n)
        return (year, None, month)

    # "in the last N years" / "past N years"
    m = re.search(r"(?:last|past|recent)\s+(\d+)\s*(?:year|yr)s?", q)
    if m:
        n = int(m.group(1))
        return (ref.year - n, None, None)

    # "in the last N weeks" — approximate with month subtraction
    m = re.search(r"(?:last|past|recent)\s+(\d+)\s*(?:week|wk)s?", q)
    if m:
        n = int(m.group(1))
        weeks_as_months = max(1, (n + 3) // 4)
        year, month = _subtract_months(ref, weeks_as_months)
        return (year, None, month)

    start: int | None = None
    end: int | None = None

    # "between YEAR1 and YEAR2" / "from YEAR1 to YEAR2" / "YEAR1–YEAR2"
    m = re.search(
        r"\b(?:between|from)\s+(19\d{2}|20\d{2})\s*(?:and|to|–|-)\s*(19\d{2}|20\d{2})\b",
        q,
    )
    if m:
        return (int(m.group(1)), int(m.group(2)), None)

    # "since YEAR" / "after YEAR" / "from YEAR" (no upper bound in same phrase)
    m = re.search(r"\b(?:since|after|from)\s+(19\d{2}|20\d{2})\b", q)
    if m:
        start = int(m.group(1))

    # "before YEAR" / "up to YEAR" / "until YEAR" / "through YEAR"
    m = re.search(r"\b(?:before|up\s*to|until|through)\s+(19\d{2}|20\d{2})\b", q)
    if m:
        end = int(m.group(1))

    # "YEAR onwards" / "YEAR to present"
    m = re.search(r"(19\d{2}|20\d{2})\s*(?:onwards|onward|to\s+present)", q)
    if m:
        start = int(m.group(1))

    return (start, end, None)


def _extract_years_from_text(text: str) -> tuple[int | None, int | None]:
    """Extract (start_year, end_year) from temporal phrases in text."""
    start, end, _ = _extract_temporal_bounds(text)
    return (start, end)


def _coerce_year(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        year = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        year = int(value.strip())
    else:
        return None
    if year < 1900 or year > 2100:
        return None
    return year


_ALLOWED_STATUSES = VALID_STATUSES


def _extract_status_from_text(text: str) -> str | None:
    """Pull an explicit trial status from natural-language phrasing."""
    q = (text or "").lower()
    # Longer / negated phrases first so "not recruiting" does not become RECRUITING.
    patterns = (
        (r"\bactive[,\s]+not\s+recruiting\b", "ACTIVE_NOT_RECRUITING"),
        (r"\bnot\s+yet\s+recruiting\b", "NOT_YET_RECRUITING"),
        (r"\benrolling\s+by\s+invitation\b", "ENROLLING_BY_INVITATION"),
        (r"\bcompleted\b", "COMPLETED"),
        (r"\bterminated\b", "TERMINATED"),
        (r"\bwithdrawn\b", "WITHDRAWN"),
        (r"\bsuspended\b", "SUSPENDED"),
        (r"\brecruiting\b", "RECRUITING"),
    )
    for pattern, status in patterns:
        if re.search(pattern, q):
            return status
    return None


_PHASE_DESCRIPTORS: frozenset[str] = frozenset({"early", "late", "early phase", "late phase"})


def _is_enrollment_phase_comparison(query: str) -> bool:
    q = (query or "").lower()
    return (
        ("enrollment" in q or "sizes" in q)
        and "early" in q
        and "late" in q
        and ("phase" in q or " vs " in q or " versus " in q)
    )


def _looks_like_drug_name(part: str) -> bool:
    cleaned = " ".join(part.strip().split())
    if not cleaned or len(cleaned) > 60:
        return False
    lower = cleaned.lower()
    if lower in _PHASE_DESCRIPTORS:
        return False
    if lower.endswith(" phase") or lower.startswith("phase "):
        return False
    if "enrollment" in lower or "trial count" in lower or "sizes for" in lower:
        return False
    if lower.startswith("compare ") or " sizes for " in lower:
        return False
    return True


def _extract_compared_drugs(text: str) -> list[str]:
    """Extract Drug A / Drug B from explicit 'compare phases for A vs B' phrasing."""
    raw = (text or "").strip()
    if not raw or _is_enrollment_phase_comparison(raw):
        return []
    q = raw.lower()
    if "enrollment" in q and (" vs " in q or " versus " in q):
        return []

    m = re.search(
        r"compare\s+(?:the\s+)?phases?\s+(?:for|of|across|between)\s+"
        r"(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:\s*[?.!]|\s+by\b|\s+phases?\b|$)",
        raw,
        re.I,
    )
    if not m:
        return []

    left = m.group(1).strip(" ,")
    right = m.group(2).strip(" ,")
    right = re.sub(r"\b(?:phases?|trials?|by\s+phase).*$", "", right, flags=re.I).strip()
    drugs: list[str] = []
    seen: set[str] = set()
    for part in (left, right):
        part = re.sub(r"^(?:drug|intervention)\s+", "", part, flags=re.I).strip()
        part = " ".join(part.split())
        if not _looks_like_drug_name(part):
            continue
        name = part.title() if part.islower() or part.isupper() else part
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        drugs.append(name)
    return drugs if len(drugs) >= 2 else []


def _is_early_late_phase_comparison(query: str) -> bool:
    q = (query or "").lower()
    return (
        "early" in q
        and "late" in q
        and "phase" in q
        and any(v in q for v in (" vs ", " versus "))
        and not _is_enrollment_phase_comparison(query)
    )


def _entity_mentioned_in_text(query: str, entity: str) -> bool:
    q = (query or "").lower()
    entity = (entity or "").strip()
    if not entity:
        return False
    el = entity.lower()
    if el in q:
        return True
    for part in re.split(r"\s+or\s+", el, flags=re.I):
        part = part.strip()
        if len(part) >= 4 and part in q:
            return True
    stop = frozenset({
        "for", "the", "and", "with", "trials", "trial", "study", "studies", "drug", "drugs",
    })
    tokens = [t for t in re.findall(r"[a-z0-9]+", el) if t not in stop and len(t) >= 4]
    return bool(tokens) and all(t in q for t in tokens)


def _condition_grounded_in_text(query: str, cond: str) -> bool:
    """True if cond (or a known abbreviation that expands to it) appears in the query."""
    if _entity_mentioned_in_text(query, cond):
        return True
    q = (query or "").lower()
    cl = (cond or "").strip().lower()
    if not cl or not q:
        return False
    for keyword, canonical in _CONDITION_KEYWORDS:
        if canonical.lower() != cl:
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", q):
            return True
    return False


def _is_drug_comparison_query(text: str) -> bool:
    return len(_extract_compared_drugs(text)) >= 2


# Tokens that appear in questions but are not disease names. Rejects fragments
# like "this drug changed" scraped from "…for this drug changed over time?".
_CONDITION_STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "for", "of", "in", "on",
    "by", "with", "and", "or", "to", "from", "about", "across", "between",
    "how", "has", "have", "had", "what", "which", "where", "when", "why",
    "drug", "drugs", "trial", "trials", "study", "studies", "number", "over",
    "time", "changed", "change", "changes", "compare", "comparing", "versus",
    "vs", "distribution", "breakdown", "network", "phase", "phases", "early",
    "late", "sponsored", "sponsor", "sponsors", "status", "recruiting",
    "patients", "therapy", "treatment", "vaccine", "enrollment",
})


def _is_plausible_condition(name: str) -> bool:
    """Reject drug-vs-drug phrases, query fragments, and lone drug names as conditions."""
    raw = (name or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if re.search(r"\b(?:vs\.?|versus)\b", lower):
        return False
    if _is_drug_comparison_query(raw):
        return False
    tokens = _tokenize_query(raw)
    if not tokens:
        return False
    if len(tokens) == 1 and _looks_like_drug_token(tokens[0]):
        return False
    content = [t for t in tokens if t.lower() not in _CONDITION_STOPWORDS and len(t) >= 3]
    if not content:
        return False
    return True


def _extract_condition_from_text(text: str) -> str | None:
    if _is_drug_comparison_query(text):
        return None
    q = (text or "").lower()
    for keyword, canonical in _CONDITION_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", q):
            return canonical

    patterns = (
        r"(?:trials?\s+for|for|stud(?:y|ies)\s+(?:on|of|in|with))\s+"
        r"([a-zA-Z0-9][a-zA-Z0-9\s\-/]+?)"
        r"(?:\s+trials|\s+sponsored|\s+by|\s+network|\s+phase|\s+over|\s+in|\s+with|\s*$)",
        r"(?:network|distribution|breakdown|compare|comparing)\s+(?:for|of|in)\s+"
        r"([a-zA-Z0-9][a-zA-Z0-9\s\-/]+?)(?:\s+trials|\s*$|\s+by)",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if not m:
            continue
        raw = m.group(1).strip(" ,.")
        raw = re.sub(r"\s+(early|late|phase|phases|trials|trial).*$", "", raw, flags=re.I).strip()
        if not raw or raw.lower() in {"early", "late", "phase", "phases"}:
            continue
        if not _is_plausible_condition(raw):
            continue
        normalized = normalize_condition_name(raw)
        if normalized and _is_plausible_condition(normalized):
            return normalized
    return None


def _extract_sponsor_from_text(text: str) -> str | None:
    patterns = (
        r"(?:sponsored\s+by|sponsor(?:ed)?(?:\s+is)?)\s+([A-Za-z][A-Za-z0-9\s&.\'-]+?)"
        r"(?:\s+for|\s+trials|\s+in|\s+with|\s+and|\s*$)",
        r"([A-Za-z][A-Za-z0-9\s&.\'-]+?)\-sponsored",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if not m:
            continue
        name = m.group(1).strip(" ,.")
        name = re.sub(r"\s+(for|trials|in|with|and)\s+.*$", "", name, flags=re.I).strip()
        if len(name) >= 2:
            return name
    return None


def _strip_hallucinated_drugs(query: str, request: QueryRequest, interpretation: dict) -> None:
    """Remove LLM-invented drug filters not grounded in the query or structured request."""
    if request.drug_name:
        return

    params = dict(interpretation.get("search_params") or {})
    intr = params.get("intr")
    if intr and not _entity_mentioned_in_text(query, intr):
        params.pop("intr", None)

    focus = interpretation.get("focus_drugs")
    if isinstance(focus, list):
        kept = [d for d in focus if _entity_mentioned_in_text(query, d)]
        if len(kept) >= 2 and interpretation.get("aggregation") == "phase_by_drug":
            interpretation["focus_drugs"] = kept
            params["intr"] = " OR ".join(kept)
        else:
            interpretation.pop("focus_drugs", None)
            if params.get("intr") and not _entity_mentioned_in_text(query, params["intr"]):
                params.pop("intr", None)

    interpretation["search_params"] = params


def _strip_ungrounded_entities(query: str, request: QueryRequest, interpretation: dict) -> None:
    """Strip inventable search entities unless present in query text or structured request.

    Mirrors the drug grounding rule for term / locn / cond / sponsor so the LLM
    cannot invent filters that still drive CT.gov.
    """
    params = dict(interpretation.get("search_params") or {})

    if not request.drug_name:
        # Drug path still handled by dedicated helper for focus_drugs / OR lists.
        _strip_hallucinated_drugs(query, request, interpretation)
        params = dict(interpretation.get("search_params") or {})

    if not request.condition:
        cond = params.get("cond")
        if cond and not _condition_grounded_in_text(query, cond):
            params.pop("cond", None)

    if not request.country:
        locn = params.get("locn")
        if locn and not _entity_mentioned_in_text(query, locn):
            params.pop("locn", None)

    # Free-text term is especially hallucination-prone — require textual grounding.
    term = params.get("term")
    if term and not _entity_mentioned_in_text(query, term):
        params.pop("term", None)

    if not request.sponsor:
        sponsor = interpretation.get("sponsor")
        if sponsor and not _entity_mentioned_in_text(query, str(sponsor)):
            interpretation.pop("sponsor", None)

    interpretation["search_params"] = params


def _enrich_search_params_from_text(
    query: str,
    request: QueryRequest,
    interpretation: dict,
) -> None:
    """Deterministically fill condition/sponsor from query text; strip invented entities."""
    params = dict(interpretation.get("search_params") or {})

    focus_drugs = interpretation.get("focus_drugs") or []
    is_drug_compare = isinstance(focus_drugs, list) and len(focus_drugs) >= 2

    if not request.condition and not is_drug_compare:
        extracted_cond = _extract_condition_from_text(query)
        if extracted_cond:
            params["cond"] = extracted_cond
        else:
            # Do not keep LLM cond when the query has no extractable disease —
            # otherwise phrases like "this drug changed" survive grounding.
            params.pop("cond", None)

    if not request.sponsor:
        extracted_sponsor = _extract_sponsor_from_text(query)
        if extracted_sponsor:
            interpretation["sponsor"] = extracted_sponsor

    cond = params.get("cond")
    if cond and not _is_plausible_condition(cond):
        params.pop("cond", None)

    if is_drug_compare:
        params.pop("cond", None)

    interpretation["search_params"] = params
    # Admit only entities grounded in query text or structured request fields.
    _strip_ungrounded_entities(query, request, interpretation)


def _resolve_aggregation_intent(
    query: str,
    *,
    compared: list[str] | None = None,
) -> dict | None:
    """Map NL cues to an aggregation/viz pair.

    Returns the first matching intent. Designed as a single ordered table so
    question classes share one routing path instead of scattered one-offs.
    """
    q = (query or "").lower()
    compared = compared or []

    # --- Relationship / network class ---
    if any(k in q for k in (
        "co-occur", "co occur", "combination stud", "drug-to-drug",
        "drug to drug", "drug + drug", "drug+drug",
    )) or (("drug network" in q or "drug-network" in q) and "sponsor" not in q):
        return {"aggregation": "drug_drug_network", "viz_type": "network_graph"}
    if "network" in q and "investigator" in q and "sponsor" in q:
        return {"aggregation": "sponsor_investigator_network", "viz_type": "network_graph"}
    if "network" in q and "investigator" in q and "drug" in q:
        return {"aggregation": "drug_investigator_network", "viz_type": "network_graph"}
    if "network" in q and "sponsor" in q and "drug" in q:
        return {"aggregation": "drug_sponsor_network", "viz_type": "network_graph"}
    if "network" in q and "condition" in q and "drug" in q:
        return {"aggregation": "drug_condition_network", "viz_type": "network_graph"}
    if "network" in q and "sponsor" in q and ("site" in q or "countr" in q or "location" in q):
        return {"aggregation": "sponsor_site_network", "viz_type": "network_graph"}
    if "network" in q and "sponsor" in q and "condition" in q:
        return {"aggregation": "sponsor_condition_network", "viz_type": "network_graph"}

    # --- Comparison / crossed-dimension class ---
    if _is_enrollment_phase_comparison(query):
        return {
            "aggregation": "enrollment_by_phase_group",
            "viz_type": "grouped_bar_chart",
            "clear_focus_drugs": True,
        }
    if _is_early_late_phase_comparison(query):
        return {
            "aggregation": "by_phase_group",
            "viz_type": "bar_chart",
            "clear_focus_drugs": True,
        }
    if compared or (
        ("compare" in q and "phase" in q)
        and (" versus " in q or " vs " in q or " vs. " in q)
        and "enrollment" not in q
    ):
        return {"aggregation": "phase_by_drug", "viz_type": "grouped_bar_chart"}
    if "compare" in q and "sponsor" in q and (
        "condition" in q or "versus" in q or " vs " in q or "across" in q
    ):
        return {"aggregation": "sponsor_condition_network", "viz_type": "network_graph"}
    if any(k in q for k in (
        "phase by status", "phase and status", "phases by status",
        "grouped by phase and status", "by phase and status",
    )):
        return {"aggregation": "phase_by_status", "viz_type": "grouped_bar_chart"}

    # --- Temporal / continuous class ---
    if any(k in q for k in (
        "per year", "each year", "over time", "changed over", "trend", "timeline",
    )):
        return {"aggregation": "by_year", "viz_type": "time_series"}
    if any(k in q for k in (
        "year versus enrollment", "year vs enrollment", "start year versus",
        "start year vs", "enrollment scatter", "year vs size",
    )):
        return {"aggregation": "year_enrollment_scatter", "viz_type": "scatter_plot"}
    if any(k in q for k in (
        "enrollment size", "enrollment sizes", "distribution of enrollment",
        "enrollment histogram", "size distribution",
    )) and "phase" not in q:
        return {"aggregation": "enrollment_histogram", "viz_type": "histogram"}

    # --- Categorical ranking / distribution class ---
    if any(k in q for k in (
        "distributed across phase", "distribution of trials by phase",
        "across phases", "by phase", "trials by phase",
    )) and "status" not in q and "drug" not in q and "early" not in q and "late" not in q:
        return {"aggregation": "by_phase", "viz_type": "bar_chart"}
    if any(k in q for k in (
        "most common intervention", "common drugs", "common intervention",
        "by drug", "by intervention", "which drugs",
    )):
        return {"aggregation": "by_drug", "viz_type": "bar_chart"}
    if any(k in q for k in (
        "countries", "geographic", "which country", "by country", "by location",
    )):
        return {"aggregation": "by_location", "viz_type": "bar_chart"}
    if any(k in q for k in (
        "which sponsor", "top sponsor", "sponsors have", "by sponsor",
        "sponsor landscape", "most trials by sponsor", "which sponsors",
    )):
        return {"aggregation": "by_sponsor", "viz_type": "bar_chart"}
    if any(k in q for k in (
        "by condition", "which condition", "top condition", "common condition",
    )):
        return {"aggregation": "by_condition", "viz_type": "bar_chart"}
    if any(k in q for k in (
        "by status", "proportion", "status breakdown", "status distribution",
        "share of", "what proportion",
    )):
        return {"aggregation": "by_status", "viz_type": "pie_chart"}

    # Named-sponsor filter ("sponsored by Pfizer…") → status overview of that corpus.
    # Do NOT treat every mention of "sponsor" as this — ranking uses by_sponsor above.
    if ("sponsored by" in q or "-sponsored" in q) and "network" not in q:
        return {"aggregation": "by_status", "viz_type": "pie_chart"}

    return None


def _apply_query_heuristics(query: str, result: dict) -> dict:
    """
    Deterministic overrides after the LLM JSON parse.

    Fixes cases a single prompt gets wrong under pressure: temporal phrases,
    Drug A vs Drug B focus list, sponsor ranking vs status pie, network modes.
    First matching intent rule wins (see ``_resolve_aggregation_intent``).
    """
    q = (query or "").lower()
    out = dict(result)

    # Deterministic temporal bounds from the query text always win over the LLM.
    h_start, h_end, h_start_month = _extract_temporal_bounds(query or "")
    if h_start is not None:
        out["start_year"] = h_start
    if h_end is not None:
        out["end_year"] = h_end
    if h_start_month is not None:
        out["start_month"] = h_start_month

    compared = _extract_compared_drugs(query or "")
    if compared:
        out["focus_drugs"] = compared

    # Intent routing: first matching rule wins. Prefer specific relationship /
    # comparison intents over generic categorical defaults.
    intent = _resolve_aggregation_intent(query or "", compared=compared)
    if intent:
        out["aggregation"] = intent["aggregation"]
        if intent.get("viz_type"):
            out["viz_type"] = intent["viz_type"]
        if intent.get("clear_focus_drugs"):
            out.pop("focus_drugs", None)

    params = dict(out.get("search_params") or {})

    # Explicit status words in the query win when the LLM omitted them.
    text_status = _extract_status_from_text(query or "")
    if text_status and not params.get("status"):
        params["status"] = text_status

    # Drug A vs Drug B → search both interventions (not enrollment/phase comparisons).
    focus = out.get("focus_drugs") or compared
    if isinstance(focus, list) and len(focus) >= 2 and out.get("aggregation") == "phase_by_drug":
        params["intr"] = " OR ".join(focus)
        out["focus_drugs"] = focus

    status = params.get("status")
    if isinstance(status, str):
        parts = [p.strip().upper().replace(" ", "_") for p in status.split(",") if p.strip()]
        kept = [p for p in parts if p in _ALLOWED_STATUSES]
        if kept:
            params["status"] = ",".join(kept)
        else:
            params.pop("status", None)
    out["search_params"] = params

    return out


