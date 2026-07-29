"""Aggregation ↔ visualization field maps and viz-type resolution.

Kept separate from aggregation implementations so routers/classifiers can import
encoding metadata without pulling every aggregator function.
"""

from __future__ import annotations

from typing import Optional

VALID_AGGREGATIONS = {
    "by_year",
    "by_phase",
    "by_phase_group",
    "by_status",
    "by_sponsor",
    "by_condition",
    "by_location",
    "by_drug",
    "phase_by_status",
    "phase_by_drug",
    "enrollment_histogram",
    "enrollment_by_phase_group",
    "year_enrollment_scatter",
    "drug_sponsor_network",
    "drug_condition_network",
    "sponsor_condition_network",
    "sponsor_site_network",
    "drug_investigator_network",
    "sponsor_investigator_network",
    "drug_drug_network",
}

DEFAULT_VIZ_TYPE = {
    "by_year": "time_series",
    "by_phase": "bar_chart",
    "by_phase_group": "bar_chart",
    "by_status": "pie_chart",
    "by_sponsor": "bar_chart",
    "by_condition": "bar_chart",
    "by_location": "bar_chart",
    "by_drug": "bar_chart",
    "phase_by_status": "grouped_bar_chart",
    "phase_by_drug": "grouped_bar_chart",
    "enrollment_histogram": "histogram",
    "enrollment_by_phase_group": "grouped_bar_chart",
    "year_enrollment_scatter": "scatter_plot",
    "drug_sponsor_network": "network_graph",
    "drug_condition_network": "network_graph",
    "sponsor_condition_network": "network_graph",
    "sponsor_site_network": "network_graph",
    "drug_investigator_network": "network_graph",
    "sponsor_investigator_network": "network_graph",
    "drug_drug_network": "network_graph",
}

FIELD_LABELS = {
    "by_year": ("year", "trial_count"),
    "by_phase": ("phase", "trial_count"),
    "by_phase_group": ("phase_group", "trial_count"),
    "by_status": ("status", "trial_count"),
    "by_sponsor": ("sponsor", "trial_count"),
    "by_condition": ("condition", "trial_count"),
    "by_location": ("country", "trial_count"),
    "by_drug": ("drug", "trial_count"),
    "phase_by_status": ("phase", "trial_count"),
    "phase_by_drug": ("phase", "trial_count"),
    "enrollment_histogram": ("enrollment_bin", "trial_count"),
    "enrollment_by_phase_group": ("enrollment_bin", "trial_count"),
    "year_enrollment_scatter": ("year", "enrollment"),
    "drug_sponsor_network": ("source", "edge_weight"),
    "drug_condition_network": ("source", "edge_weight"),
    "sponsor_condition_network": ("source", "edge_weight"),
    "sponsor_site_network": ("source", "edge_weight"),
    "drug_investigator_network": ("source", "edge_weight"),
    "sponsor_investigator_network": ("source", "edge_weight"),
    "drug_drug_network": ("source", "edge_weight"),
}

FIELD_TYPES = {
    "by_year": ("temporal", "quantitative"),
    "by_phase": ("nominal", "quantitative"),
    "by_phase_group": ("nominal", "quantitative"),
    "by_status": ("nominal", "quantitative"),
    "by_sponsor": ("nominal", "quantitative"),
    "by_condition": ("nominal", "quantitative"),
    "by_location": ("nominal", "quantitative"),
    "by_drug": ("nominal", "quantitative"),
    "phase_by_status": ("nominal", "quantitative"),
    "phase_by_drug": ("nominal", "quantitative"),
    "enrollment_histogram": ("ordinal", "quantitative"),
    "enrollment_by_phase_group": ("ordinal", "quantitative"),
    "year_enrollment_scatter": ("temporal", "quantitative"),
    "drug_sponsor_network": ("nominal", "quantitative"),
    "drug_condition_network": ("nominal", "quantitative"),
    "sponsor_condition_network": ("nominal", "quantitative"),
    "sponsor_site_network": ("nominal", "quantitative"),
    "drug_investigator_network": ("nominal", "quantitative"),
    "sponsor_investigator_network": ("nominal", "quantitative"),
    "drug_drug_network": ("nominal", "quantitative"),
}

SERIES_FIELD = {
    "phase_by_status": "status",
    "phase_by_drug": "drug",
    "enrollment_by_phase_group": "phase_group",
    "year_enrollment_scatter": "phase",
}

ALLOWED_VIZ_TYPES = {
    "bar_chart",
    "grouped_bar_chart",
    "time_series",
    "scatter_plot",
    "histogram",
    "network_graph",
    "pie_chart",
    "stacked_bar_chart",
    "table",
}

# Categorical aggregations that can render as pie when asked
PIE_ELIGIBLE = {"by_phase", "by_status", "by_condition", "by_drug", "by_location", "by_sponsor"}

_NETWORK_AGGREGATIONS = frozenset({
    "drug_sponsor_network",
    "drug_condition_network",
    "sponsor_condition_network",
    "sponsor_site_network",
    "drug_investigator_network",
    "sponsor_investigator_network",
    "drug_drug_network",
})


def resolve_viz_type(aggregation: str, requested: Optional[str] = None) -> str:
    """Pick a viz type from aggregation (+ optional LLM request)."""
    requested = (requested or "").strip().lower()
    default = DEFAULT_VIZ_TYPE.get(aggregation, "bar_chart")

    if aggregation in _NETWORK_AGGREGATIONS:
        return "network_graph"
    if aggregation == "year_enrollment_scatter":
        return "scatter_plot"
    if aggregation == "enrollment_histogram":
        return "histogram"
    if aggregation == "enrollment_by_phase_group":
        return "grouped_bar_chart"
    if aggregation == "by_phase_group":
        return "bar_chart"
    if aggregation in {"phase_by_status", "phase_by_drug"}:
        return "grouped_bar_chart" if requested != "stacked_bar_chart" else "stacked_bar_chart"
    if aggregation == "by_year":
        return "time_series"

    if requested in ALLOWED_VIZ_TYPES:
        if requested == "pie_chart" and aggregation in PIE_ELIGIBLE:
            return "pie_chart"
        if requested in {"bar_chart", "table"} and aggregation in PIE_ELIGIBLE | {
            "by_phase", "by_status", "by_sponsor", "by_condition", "by_location", "by_drug"
        }:
            return requested
        if requested == "histogram" and aggregation == "by_year":
            return "histogram"
    return default


def get_field_labels(aggregation: str) -> tuple[str, str]:
    """Return (x_field, y_field) names for an aggregation."""
    return FIELD_LABELS.get(aggregation, ("category", "count"))


def get_field_types(aggregation: str) -> tuple[str, str]:
    """Return (x_type, y_type) encoding types for an aggregation."""
    return FIELD_TYPES.get(aggregation, ("nominal", "quantitative"))


def get_series_field(aggregation: str) -> Optional[str]:
    """Optional series/color field name for grouped charts."""
    return SERIES_FIELD.get(aggregation)
