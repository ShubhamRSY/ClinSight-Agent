"""Tests for the in-process TTL query response cache."""

from app.schemas.input import QueryRequest
from app.schemas.output import (
    DataPoint,
    Encoding,
    Metadata,
    VisualizationResponse,
    VisualizationSpec,
)
from app.services.cache import QueryResponseCache


def _sample_response() -> VisualizationResponse:
    return VisualizationResponse(
        visualization=VisualizationSpec(
            type="bar_chart",
            title="Example",
            encoding=Encoding(
                x={"field": "country", "type": "nominal"},
                y={"field": "trial_count", "type": "quantitative"},
            ),
            data=[DataPoint(label="United States", value=3, x="United States", y=3)],
        ),
        meta=Metadata(
            filters={"status": "RECRUITING"},
            source="clinicaltrials.gov",
            grouping="by_location",
            total_records=3,
        ),
    )


def test_query_response_cache_hit_and_miss():
    cache = QueryResponseCache(ttl_seconds=60, max_entries=8)
    req = QueryRequest(query="countries for lung cancer", status="RECRUITING")
    assert cache.get(req) is None
    cache.set(req, _sample_response())
    hit = cache.get(req)
    assert hit is not None
    assert hit.visualization.title == "Example"


def test_query_response_cache_key_ignores_none_fields():
    a = QueryRequest(query="x", status="RECRUITING")
    b = QueryRequest(query="x", status="RECRUITING", drug_name=None)
    assert QueryResponseCache.cache_key(a) == QueryResponseCache.cache_key(b)


def test_query_response_cache_disabled_when_ttl_zero():
    cache = QueryResponseCache(ttl_seconds=0)
    req = QueryRequest(query="x")
    cache.set(req, _sample_response())
    assert cache.get(req) is None
