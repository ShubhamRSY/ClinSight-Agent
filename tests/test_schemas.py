"""Schema contract tests — unambiguous I/O for frontend consumers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.builder import build_response
from app.engine.labels import build_deterministic_encoding
from app.schemas.input import QueryRequest
from app.schemas.output import (
    ChannelEncoding,
    DataPoint,
    Encoding,
    FieldType,
    VisualizationResponse,
)


def test_query_request_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        QueryRequest(query="x", unexpected="nope")


def test_encoding_requires_typed_channels():
    enc = Encoding(
        x=ChannelEncoding(field="country", type=FieldType.nominal),
        y=ChannelEncoding(field="trial_count", type=FieldType.quantitative),
    )
    dumped = enc.model_dump()
    assert dumped["x"] == {"field": "country", "type": "nominal"}
    assert dumped["y"]["type"] == "quantitative"

    with pytest.raises(ValidationError):
        Encoding(x={"field": "country", "type": "not-a-real-type"})


def test_datapoint_x_y_are_scalars_not_objects():
    with pytest.raises(ValidationError):
        DataPoint(x={"nested": True}, y=1)


def test_build_response_uses_channel_encoding_and_contributing_count():
    encoding = build_deterministic_encoding(
        viz_type="bar_chart",
        x_field="country",
        y_field="trial_count",
        x_type="nominal",
        y_type="quantitative",
    )
    response = build_response(
        aggregated_data=[
            {
                "country": "United States",
                "trial_count": 3,
                "contributing_count": 3,
                "_study_ids": ["NCT1"],
            }
        ],
        studies=[
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT1", "briefTitle": "Study"},
                    "statusModule": {"overallStatus": "RECRUITING"},
                    "contactsLocationsModule": {"locations": [{"country": "United States"}]},
                }
            }
        ],
        viz_type="bar_chart",
        title="Countries",
        encoding=encoding,
        x_field="country",
        y_field="trial_count",
        series_field=None,
        x_type="nominal",
        y_type="quantitative",
        total_count=3,
        filters_applied={"status": "RECRUITING"},
        aggregation="by_location",
    )
    assert isinstance(response, VisualizationResponse)
    point = response.visualization.data[0]
    assert point.contributing_count == 3
    assert point.label == "United States"
    assert response.visualization.encoding.x.field == "country"
    assert response.visualization.encoding.x.type == FieldType.nominal
    # Private aggregator keys must not leak into the public contract.
    dumped = response.model_dump()
    assert "_study_ids" not in dumped["visualization"]["data"][0]


def test_visualization_response_round_trip_openapi_example():
    example = VisualizationResponse.model_config["json_schema_extra"]["examples"][0]
    parsed = VisualizationResponse.model_validate(example)
    assert parsed.visualization.type.value == "bar_chart"
    assert parsed.visualization.data[0].citations[0].nct_id.startswith("NCT")
    assert "clinicaltrials.gov/study/" in parsed.visualization.data[0].citations[0].url
