"""Deterministic chart titles/notes — no LLM invention of filters or counts.

``resolve_title_and_notes`` prefers the LLM title when every named entity is
present in Filters; otherwise it swaps in ``build_template_title`` /
``build_template_notes`` so labels stay grounded.
"""

from __future__ import annotations

import re
from typing import Any, Optional


_AGGREGATION_TITLES = {
    "by_year": "Trial Starts by Year",
    "by_phase": "Trials by Phase",
    "by_phase_group": "Early vs Late Phase Trials",
    "by_status": "Trials by Status",
    "by_sponsor": "Trials by Sponsor",
    "by_condition": "Trials by Condition",
    "by_location": "Trials by Country",
    "by_drug": "Trials by Intervention",
    "phase_by_status": "Phases by Status",
    "phase_by_drug": "Phases by Drug",
    "enrollment_histogram": "Enrollment Size Distribution",
    "enrollment_by_phase_group": "Enrollment: Early vs Late Phase",
    "year_enrollment_scatter": "Start Year vs Enrollment",
    "drug_sponsor_network": "Drug–Sponsor Network",
    "drug_condition_network": "Drug–Condition Network",
    "sponsor_condition_network": "Sponsor–Condition Network",
    "sponsor_site_network": "Sponsor–Site Network",
    "drug_investigator_network": "Drug–Investigator Network",
    "sponsor_investigator_network": "Sponsor–Investigator Network",
    "drug_drug_network": "Drug–Drug Co-occurrence Network",
}


def _filter_snippets(filters: dict) -> list[str]:
    """Human-readable snippets from applied filters for titles."""
    snippets: list[str] = []
    if filters.get("drugs"):
        snippets.append(" vs ".join(str(d) for d in filters["drugs"]))
    elif filters.get("drug_name"):
        snippets.append(str(filters["drug_name"]))
    elif filters.get("intervention"):
        snippets.append(str(filters["intervention"]))
    if filters.get("condition"):
        snippets.append(str(filters["condition"]))
    if filters.get("sponsor"):
        snippets.append(f"sponsored by {filters['sponsor']}")
    if filters.get("country"):
        snippets.append(str(filters["country"]))
    if filters.get("status"):
        snippets.append(str(filters["status"]).replace("_", " ").title())
    if filters.get("phase"):
        phase = str(filters["phase"]).replace("_", " ").title().replace("Phase", "Phase ")
        snippets.append(phase)
    return snippets


def _year_span_from_data(aggregated_data: list[dict]) -> Optional[str]:
    """Min–max year span from aggregated year labels."""
    years: list[int] = []
    for row in aggregated_data or []:
        raw = row.get("year") or row.get("x") or row.get("label")
        try:
            years.append(int(str(raw)[:4]))
        except (TypeError, ValueError):
            continue
    if not years:
        return None
    lo, hi = min(years), max(years)
    return str(lo) if lo == hi else f"{lo}–{hi}"


def build_template_title(
    aggregation: str,
    filters: dict,
    aggregated_data: list[dict] | None = None,
) -> str:
    """Build a title only from filters + aggregation + observed year span."""
    base = _AGGREGATION_TITLES.get(aggregation, "Clinical Trials")
    parts = _filter_snippets(filters or {})

    start = filters.get("start_year") if filters else None
    end = filters.get("end_year") if filters else None
    if start is not None and end is not None:
        parts.append(f"{start}–{end}" if start != end else str(start))
    elif start is not None:
        parts.append(f"since {start}")
    elif end is not None:
        parts.append(f"through {end}")
    else:
        span = _year_span_from_data(aggregated_data or [])
        if span and aggregation in {"by_year", "year_enrollment_scatter"}:
            parts.append(span)

    if not parts:
        return base
    return f"{base} ({', '.join(parts)})"


def build_template_notes(
    *,
    filters: dict,
    aggregation: str,
    total_count: int,
) -> str:
    """Short notes grounded only in applied filters and aggregation."""
    bits: list[str] = []
    if filters:
        bits.append(f"Filters applied: {filters}.")
    bits.append(f"Grouped by {aggregation.replace('_', ' ')}.")
    bits.append(f"Chart reflects {total_count} studies after local filters.")
    return " ".join(bits)


def allowed_entities_from_filters(filters: dict) -> set[str]:
    """Lowercased entity tokens that titles/notes may mention."""
    allowed: set[str] = set()
    if not filters:
        return allowed
    for key in ("drug_name", "intervention", "condition", "sponsor", "country", "status", "phase"):
        val = filters.get(key)
        if isinstance(val, str) and val.strip():
            allowed.add(val.strip().lower())
            for tok in val.lower().replace("_", " ").split():
                if len(tok) >= 4:
                    allowed.add(tok)
    drugs = filters.get("drugs")
    if isinstance(drugs, list):
        for d in drugs:
            if isinstance(d, str) and d.strip():
                allowed.add(d.strip().lower())
    return allowed


def text_mentions_ungrounded_entity(text: str, filters: dict) -> bool:
    """
    Heuristic: flag likely invented proper entities in free text.

    Looks for Capitalized multi-char tokens (and known drug-like tokens) that
    are absent from filters. Conservative — used to reject LLM titles/notes.
    """
    if not text:
        return False
    allowed = allowed_entities_from_filters(filters)
    # Common chart words that are always fine.
    stop = {
        "clinical", "trials", "trial", "study", "studies", "phase", "phases",
        "status", "statuses", "year", "years", "country", "countries", "drug",
        "drugs", "sponsor", "sponsors", "condition", "conditions", "enrollment",
        "distribution", "comparison", "network", "trend", "recruiting",
        "completed", "terminated", "active", "early", "late", "group",
        "intervention", "interventions", "since", "through", "versus",
        "united", "states", "south", "korea", "hong", "kong",
    }

    # Flag Title-Case words / ALLCAPS enums not present in filters.
    candidates = re.findall(r"\b([A-Z][a-zA-Z0-9][a-zA-Z0-9\-]{2,})\b", text)
    for cand in candidates:
        low = cand.lower()
        if low in stop:
            continue
        if low in allowed:
            continue
        # Allow if any allowed entity contains this token.
        if any(low in a or a in low for a in allowed):
            continue
        # Year-like numbers handled separately; skip pure YEAR in title from regex above.
        return True
    return False


def resolve_title_and_notes(
    *,
    llm_title: Optional[str],
    llm_notes: Optional[str],
    aggregation: str,
    filters: dict,
    aggregated_data: list[dict],
    total_count: int,
) -> tuple[str, str]:
    """Prefer LLM text only when it stays within filter-grounded entities."""
    template_title = build_template_title(aggregation, filters, aggregated_data)
    template_notes = build_template_notes(
        filters=filters,
        aggregation=aggregation,
        total_count=total_count,
    )

    title = template_title
    if llm_title and not text_mentions_ungrounded_entity(str(llm_title), filters):
        title = str(llm_title).strip() or template_title

    notes = template_notes
    if llm_notes and not text_mentions_ungrounded_entity(str(llm_notes), filters):
        notes = str(llm_notes).strip() or template_notes

    return title, notes


def build_deterministic_encoding(
    *,
    viz_type: str,
    x_field: str,
    y_field: str,
    x_type: str,
    y_type: str,
    series_field: Optional[str] = None,
) -> dict[str, Any]:
    """Encoding is always derived from aggregation field maps — never LLM field names."""
    if viz_type == "network_graph":
        return {
            "source": {"field": "source", "type": "nominal"},
            "target": {"field": "target", "type": "nominal"},
            "edge_weight": {"field": "edge_weight", "type": "quantitative"},
            "x": {"field": "source", "type": "nominal"},
            "y": {"field": "target", "type": "nominal"},
        }
    encoding: dict[str, Any] = {
        "x": {"field": x_field, "type": x_type},
        "y": {"field": y_field, "type": y_type},
    }
    if series_field:
        encoding["color"] = {"field": series_field, "type": "nominal"}
    if viz_type == "scatter_plot":
        encoding["size"] = {"field": y_field, "type": "quantitative"}
    return encoding
