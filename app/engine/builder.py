"""Assemble VisualizationResponse from aggregated rows + study records.

Attaches deep citations (nct_id + url + field-path excerpt) per datum and sets
meta.truncated / total_available when the fetch cap was hit.
"""

from __future__ import annotations

from typing import Optional

from app.engine.citations import citations_for_datum
from app.schemas.output import (
    ChannelEncoding,
    DataPoint,
    Encoding,
    FieldType,
    Metadata,
    VisualizationResponse,
    VisualizationSpec,
    VizType,
)


def _build_study_map(studies: list[dict]) -> dict[str, dict]:
    return {
        s.get("protocolSection", {}).get("identificationModule", {}).get("nctId", ""): s
        for s in studies
    }


def build_data_points(
    aggregated_data: list[dict],
    x_field: str,
    y_field: str,
    studies: list[dict],
    series_field: Optional[str] = None,
    viz_type: str = "bar_chart",
    aggregation: str = "by_status",
) -> list[DataPoint]:
    study_map = _build_study_map(studies)
    points = []
    reserved = {
        "label", "value", "series", "x", "y", "size", "source", "target",
        "edge_weight", "citations", "_study_ids", "contributing_count",
    }
    for row in aggregated_data:
        # Domain keys (year, phase, …) stay for tooltips; never leak private _* keys.
        extra_fields = {
            k: v
            for k, v in row.items()
            if k not in reserved and not str(k).startswith("_")
        }
        contributing = row.get("contributing_count")
        if contributing is not None:
            try:
                contributing = int(contributing)
            except (TypeError, ValueError):
                contributing = None

        citations = citations_for_datum(
            study_ids=row.get("_study_ids", []),
            study_map=study_map,
            aggregation=aggregation,
            datum=row,
        )

        if viz_type == "network_graph":
            source = row.get("source")
            target = row.get("target")
            weight = row.get("edge_weight", row.get(y_field, 0))
            dp = DataPoint(
                label=str(row.get("label") or f"{source} → {target}"),
                value=weight,
                x=source,
                y=target,
                source=str(source) if source is not None else None,
                target=str(target) if target is not None else None,
                edge_weight=float(weight) if weight is not None else None,
                contributing_count=contributing,
                citations=citations,
                **extra_fields,
            )
        elif viz_type == "scatter_plot":
            dp = DataPoint(
                label=str(row.get("label") or row.get(x_field, "")),
                value=row.get(y_field),
                series=str(row.get(series_field)) if series_field and row.get(series_field) is not None else None,
                x=row.get(x_field),
                y=row.get(y_field),
                size=float(row.get(y_field)) if row.get(y_field) is not None else None,
                contributing_count=contributing,
                citations=citations,
                **extra_fields,
            )
        else:
            dp = DataPoint(
                label=str(row.get(x_field, row.get("label", ""))),
                value=row.get(y_field, 0),
                series=str(row.get(series_field)) if series_field and row.get(series_field) is not None else None,
                x=row.get(x_field),
                y=row.get(y_field),
                contributing_count=contributing,
                citations=citations,
                **extra_fields,
            )
        points.append(dp)
    return points


def build_response(
    aggregated_data: list[dict],
    studies: list[dict],
    viz_type: str,
    title: str,
    encoding: dict,
    x_field: str,
    y_field: str,
    series_field: Optional[str],
    x_type: str,
    y_type: str,
    total_count: int,
    filters_applied: dict,
    aggregation: str,
    notes: Optional[str] = None,
    total_available: Optional[int] = None,
    truncated: bool = False,
) -> VisualizationResponse:
    """Assemble the assignment visualization + meta contract.

    ``encoding`` is expected to already be deterministic (from aggregation field
    maps). LLM field names are never trusted here.
    """
    data_points = build_data_points(
        aggregated_data,
        x_field,
        y_field,
        studies,
        series_field,
        viz_type=viz_type,
        aggregation=aggregation,
    )

    cited = sum(1 for p in data_points if p.citations)
    citation_note = (
        f"Deep citations attached to {cited}/{len(data_points)} data points "
        "(sample of contributing NCT records with API field excerpts)."
    )
    fetch_note = ""
    if truncated and total_available is not None:
        fetch_note = (
            f" Fetched {total_count} of {total_available} matching API studies "
            "(pagination capped for latency); chart counts reflect fetched records "
            "after local filters."
        )
    elif (
        total_available is not None
        and total_count is not None
        and total_available > total_count
    ):
        fetch_note = (
            f" Chart uses {total_count} studies after local filters "
            f"(API matched about {total_available})."
        )
    if notes:
        notes = f"{notes} {citation_note}{fetch_note}".strip()
    else:
        notes = f"{citation_note}{fetch_note}".strip()

    enc = Encoding.model_validate(encoding) if encoding else Encoding(
        x=ChannelEncoding(field=x_field, type=FieldType(x_type)),
        y=ChannelEncoding(field=y_field, type=FieldType(y_type)),
    )
    # Fill optional network/scatter fields if the caller omitted them.
    if viz_type == "network_graph":
        enc.source = enc.source or ChannelEncoding(field="source", type=FieldType.nominal)
        enc.target = enc.target or ChannelEncoding(field="target", type=FieldType.nominal)
        enc.edge_weight = enc.edge_weight or ChannelEncoding(
            field="edge_weight", type=FieldType.quantitative
        )
        enc.x = enc.x or ChannelEncoding(field="source", type=FieldType.nominal)
        enc.y = enc.y or ChannelEncoding(field="target", type=FieldType.nominal)
    else:
        if series_field and not enc.color:
            enc.color = ChannelEncoding(field=series_field, type=FieldType.nominal)
        if viz_type == "scatter_plot" and not enc.size:
            enc.size = ChannelEncoding(field=y_field, type=FieldType.quantitative)

    meta = Metadata(
        filters=filters_applied,
        source="clinicaltrials.gov",
        time_granularity="year" if aggregation in {"by_year", "year_enrollment_scatter"} else None,
        grouping=aggregation,
        total_records=total_count,
        total_available=total_available,
        truncated=truncated,
        notes=notes,
        units="enrollment (participants)" if aggregation in {
            "enrollment_histogram",
            "enrollment_by_phase_group",
            "year_enrollment_scatter",
        } else None,
    )

    viz = VisualizationSpec(
        type=VizType(viz_type),
        title=title,
        encoding=enc,
        data=data_points,
    )

    return VisualizationResponse(visualization=viz, meta=meta)
