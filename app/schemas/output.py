"""Response contracts: Vega-lite-inspired visualization + metadata.

Designed so a frontend can render without guessing:
- ``visualization.type`` selects the renderer
- ``visualization.encoding`` maps channels → data fields + scale types
- ``visualization.data[]`` carries canonical channels (label/value/x/y/…)
  plus optional domain keys (year, phase, …) and ``citations``
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Allowed visualization.type values ---

class VizType(str, Enum):
    bar_chart = "bar_chart"
    grouped_bar_chart = "grouped_bar_chart"
    time_series = "time_series"
    scatter_plot = "scatter_plot"
    histogram = "histogram"
    network_graph = "network_graph"
    pie_chart = "pie_chart"
    stacked_bar_chart = "stacked_bar_chart"
    table = "table"


class FieldType(str, Enum):
    """Encoding scale types (Vega-lite style)."""

    nominal = "nominal"
    ordinal = "ordinal"
    quantitative = "quantitative"
    temporal = "temporal"


class ChannelEncoding(BaseModel):
    """One visual channel → data field mapping."""

    field: str = Field(..., description="DataPoint attribute or domain key to read", min_length=1)
    type: FieldType = Field(..., description="Scale type for the channel")

    model_config = ConfigDict(extra="forbid")


# --- Deep citation: NCT id + URL + field-path excerpt ---

class Citation(BaseModel):
    """Traceability link from a chart mark back to a ClinicalTrials.gov study."""

    nct_id: str = Field(..., description="ClinicalTrials.gov identifier (NCT…)", min_length=3)
    url: str = Field(
        ...,
        description="Canonical study page URL on clinicaltrials.gov",
        min_length=8,
    )
    excerpt: str = Field(
        ...,
        description=(
            "Supporting text built from exact API field paths/values that justify "
            "why this study belongs in the datum (never a hallucinated summary)"
        ),
    )

    model_config = ConfigDict(extra="forbid")


# --- One chart mark (bar / slice / point / network edge) ---

class DataPoint(BaseModel):
    """One rendered mark (bar, slice, point, or network edge).

    Canonical channels are always preferred by the demo UI. Domain keys such as
    ``year``, ``phase``, ``status``, ``country``, ``drug``, ``sponsor``,
    ``condition``, ``enrollment_bin``, ``phase_group``, and ``trial_count``
    may also appear when useful for tooltips or debugging (``extra`` allowed).
    """

    label: Optional[str] = Field(None, description="Human-readable category / mark label")
    value: Optional[float | int] = Field(None, description="Primary numeric measure")
    series: Optional[str] = Field(None, description="Series/group name for multi-series charts")
    x: Optional[str | int | float] = Field(None, description="X-channel value (category, year, or node id)")
    y: Optional[str | int | float] = Field(None, description="Y-channel value (measure or node id)")
    size: Optional[float] = Field(None, description="Mark size (scatter / bubble)")
    source: Optional[str] = Field(None, description="Network edge source node id")
    target: Optional[str] = Field(None, description="Network edge target node id")
    edge_weight: Optional[float] = Field(None, description="Network edge weight")
    contributing_count: Optional[int] = Field(
        None,
        ge=0,
        description="How many fetched studies contributed to this datum",
    )
    citations: Optional[list[Citation]] = Field(
        None,
        description="Sample of NCT records supporting this datum",
    )

    model_config = ConfigDict(extra="allow")

    @field_validator("citations")
    @classmethod
    def empty_citations_to_none(cls, value: Optional[list[Citation]]) -> Optional[list[Citation]]:
        if value is not None and len(value) == 0:
            return None
        return value


# --- Vega-lite-style channel bindings the frontend reads ---

class Encoding(BaseModel):
    """Channel map telling the frontend which DataPoint fields to plot."""

    x: Optional[ChannelEncoding] = Field(None, description="X-axis / primary category channel")
    y: Optional[ChannelEncoding] = Field(None, description="Y-axis / primary measure channel")
    color: Optional[ChannelEncoding] = Field(None, description="Color / series channel")
    size: Optional[ChannelEncoding] = Field(None, description="Size channel (scatter)")
    source: Optional[ChannelEncoding] = Field(None, description="Network source channel")
    target: Optional[ChannelEncoding] = Field(None, description="Network target channel")
    edge_weight: Optional[ChannelEncoding] = Field(None, description="Network weight channel")

    model_config = ConfigDict(extra="forbid")


# --- Honesty + provenance (filters, truncation, notes) ---

class Metadata(BaseModel):
    """Response metadata for chrome, chips, and truncation honesty."""

    filters: dict[str, Any] = Field(default_factory=dict, description="Filters applied to build this chart")
    source: str = Field("clinicaltrials.gov", description="Upstream data source")
    units: Optional[str] = Field(None, description="Units for the primary measure when non-count")
    time_granularity: Optional[str] = Field(None, description="Time bucket size when temporal (e.g. year)")
    sorting: Optional[str] = Field(None, description="Sort order applied to data, if any")
    grouping: Optional[str] = Field(None, description="Aggregation key (e.g. by_year, drug_sponsor_network)")
    total_records: Optional[int] = Field(
        None,
        ge=0,
        description="Studies used after local filters (chart denominator)",
    )
    total_available: Optional[int] = Field(
        None,
        ge=0,
        description="API-reported matches (may exceed fetched page budget)",
    )
    truncated: Optional[bool] = Field(
        None,
        description="True when fetch was capped below total_available",
    )
    notes: Optional[str] = Field(None, description="Assumptions, grounding notes, citation summary")

    model_config = ConfigDict(extra="forbid")


# --- Chart payload: type + title + encoding + data[] ---

class VisualizationSpec(BaseModel):
    type: VizType = Field(..., description="Chart renderer to use")
    title: str = Field(..., description="Human-readable chart title", min_length=1)
    encoding: Encoding = Field(..., description="Channel → field map")
    data: list[DataPoint] = Field(..., description="Ordered marks to render")

    model_config = ConfigDict(extra="forbid")


# --- Top-level API response: visualization + meta ---

class VisualizationResponse(BaseModel):
    """Top-level assignment contract: visualization + meta."""

    visualization: VisualizationSpec
    meta: Metadata = Field(..., description="Filters, counts, notes for the UI chrome")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "visualization": {
                        "type": "bar_chart",
                        "title": "Trials by Country (Lung Cancer, Recruiting)",
                        "encoding": {
                            "x": {"field": "country", "type": "nominal"},
                            "y": {"field": "trial_count", "type": "quantitative"},
                        },
                        "data": [
                            {
                                "label": "United States",
                                "value": 42,
                                "x": "United States",
                                "y": 42,
                                "contributing_count": 42,
                                "country": "United States",
                                "trial_count": 42,
                                "citations": [
                                    {
                                        "nct_id": "NCT01234567",
                                        "url": "https://clinicaltrials.gov/study/NCT01234567",
                                        "excerpt": "identificationModule.nctId=NCT01234567 | contactsLocationsModule.locations.country='United States'",
                                    }
                                ],
                            }
                        ],
                    },
                    "meta": {
                        "filters": {"condition": "Lung Cancer", "status": "RECRUITING"},
                        "source": "clinicaltrials.gov",
                        "grouping": "by_location",
                        "total_records": 80,
                        "total_available": 120,
                        "truncated": False,
                        "notes": "Grouped by location.",
                    },
                }
            ]
        },
    )


class ErrorBody(BaseModel):
    """Structured error payload returned as ``{"detail": ...}`` by FastAPI."""

    detail: str = Field(..., description="Human-readable error message")
