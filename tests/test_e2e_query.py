"""HTTP-level end-to-end tests for POST /api/v1/query (external deps mocked)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas.output import VisualizationResponse
from main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_query_cache():
    from app.routers.query import query_response_cache

    query_response_cache.clear()
    yield
    query_response_cache.clear()


def _sample_study(
    nct: str = "NCT00000001",
    *,
    status: str = "RECRUITING",
    condition: str = "Lung Cancer",
    country: str = "United States",
    phase: str = "PHASE3",
    year: str = "2020-01-15",
    drug: str = "Pembrolizumab",
    sponsor: str = "Acme Pharma",
) -> dict:
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct,
                "briefTitle": f"{drug} study for {condition}",
            },
            "statusModule": {
                "overallStatus": status,
                "startDateStruct": {"date": year},
            },
            "designModule": {
                "phases": [phase],
                "enrollmentInfo": {"count": 100},
            },
            "conditionsModule": {"conditions": [condition]},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": drug}],
            },
            "contactsLocationsModule": {
                "locations": [{"country": country}],
                "overallOfficials": [{"name": "Dr Example"}],
            },
        }
    }


@pytest.fixture
def mock_pipeline(monkeypatch):
    """Mock interpret + CT.gov + classifier so HTTP path is deterministic."""

    async def fake_interpret(request):
        return {
            "search_params": {
                "cond": request.condition or "Lung Cancer",
                "status": request.status or "RECRUITING",
            },
            "aggregation": "by_location",
            "viz_type": "bar_chart",
            "needs_visualization": True,
            "start_year": request.start_year,
            "end_year": request.end_year,
            "start_month": None,
            "sponsor": request.sponsor,
            "focus_drugs": None,
        }

    async def fake_fetch(self, **kwargs):
        return {
            "studies": [
                _sample_study("NCT111", country="United States"),
                _sample_study("NCT222", country="China", drug="Nivolumab"),
            ],
            "totalCount": 2,
            "fetchedCount": 2,
            "truncated": False,
        }

    async def fake_classify(**kwargs):
        return {
            "title": "Countries with Recruiting Lung Cancer Trials",
            "encoding": {},
            "meta": {"notes": "Mocked classification"},
        }

    monkeypatch.setattr("app.routers.query.interpret_query", fake_interpret)
    monkeypatch.setattr(
        "app.api.clinical_trials.ClinicalTrialsClient.search_studies_paginated",
        fake_fetch,
    )
    monkeypatch.setattr("app.routers.query.classify_visualization", fake_classify)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")


def test_e2e_query_returns_visualization(client, mock_pipeline, monkeypatch):
    monkeypatch.setattr("app.routers.query.OPENAI_API_KEY", "test-key")
    res = client.post(
        "/api/v1/query",
        json={
            "query": "Which countries have the most recruiting trials for lung cancer?",
            "status": "RECRUITING",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "visualization" in body and "meta" in body
    viz = body["visualization"]
    assert viz["type"] == "bar_chart"
    assert viz["title"]
    assert len(viz["data"]) >= 1
    assert body["meta"]["grouping"] == "by_location"
    assert body["meta"]["source"] == "clinicaltrials.gov"
    # Round-trip through response model.
    VisualizationResponse.model_validate(body)


def test_e2e_response_includes_deep_citations(client, mock_pipeline, monkeypatch):
    """Each datum carries NCT id + url + field-path excerpt for source traceability."""
    monkeypatch.setattr("app.routers.query.OPENAI_API_KEY", "test-key")
    res = client.post(
        "/api/v1/query",
        json={
            "query": "Which countries have the most recruiting trials for lung cancer?",
            "status": "RECRUITING",
        },
    )
    assert res.status_code == 200, res.text
    points = res.json()["visualization"]["data"]
    assert points
    cited = [p for p in points if p.get("citations")]
    assert cited, "expected at least one datum with citations"
    cite = cited[0]["citations"][0]
    assert cite["nct_id"].startswith("NCT")
    assert cite["url"] == f"https://clinicaltrials.gov/study/{cite['nct_id']}"
    assert "identificationModule.nctId=" in cite["excerpt"]
    assert cited[0].get("contributing_count") is not None


def test_e2e_invalid_status_rejected_before_pipeline(client, monkeypatch):
    monkeypatch.setattr("app.routers.query.OPENAI_API_KEY", "test-key")
    res = client.post(
        "/api/v1/query",
        json={
            "query": "Which countries have the most recruiting trials for lung cancer?",
            "status": "not-a-real-status",
        },
    )
    assert res.status_code == 422
    detail = res.json().get("detail")
    assert detail
    text = str(detail).lower()
    assert "status" in text or "invalid" in text


def test_e2e_identical_queries_are_cached(client, mock_pipeline, monkeypatch):
    monkeypatch.setattr("app.routers.query.OPENAI_API_KEY", "test-key")
    calls = {"interpret": 0}

    async def counting_interpret(request):
        calls["interpret"] += 1
        return {
            "search_params": {"cond": "Lung Cancer", "status": "RECRUITING"},
            "aggregation": "by_location",
            "viz_type": "bar_chart",
            "needs_visualization": True,
            "start_year": None,
            "end_year": None,
            "start_month": None,
            "sponsor": None,
            "focus_drugs": None,
        }

    monkeypatch.setattr("app.routers.query.interpret_query", counting_interpret)

    payload = {
        "query": "Which countries have the most recruiting trials for lung cancer?",
        "status": "RECRUITING",
    }
    first = client.post("/api/v1/query", json=payload)
    second = client.post("/api/v1/query", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls["interpret"] == 1


def test_e2e_filters_strip_to_nothing_returns_clarification(client, monkeypatch):
    """Fetched studies that all fail local filters → 422 clarification, not 404."""
    monkeypatch.setattr("app.routers.query.OPENAI_API_KEY", "test-key")

    async def fake_interpret(request):
        return {
            "search_params": {"cond": "Lung Cancer", "locn": "China"},
            "aggregation": "by_location",
            "viz_type": "bar_chart",
            "needs_visualization": True,
            "start_year": None,
            "end_year": None,
            "start_month": None,
            "sponsor": None,
            "focus_drugs": None,
        }

    async def fake_fetch(*args, **kwargs):
        # Only US sites — China country filter will wipe them.
        return {
            "studies": [
                _sample_study("NCT111", country="United States"),
                _sample_study("NCT222", country="United States"),
            ],
            "totalCount": 2,
            "fetchedCount": 2,
            "truncated": False,
        }

    monkeypatch.setattr("app.routers.query.interpret_query", fake_interpret)
    monkeypatch.setattr("app.routers.query.fetch_studies_for_query", fake_fetch)

    res = client.post(
        "/api/v1/query",
        json={
            "query": "lung cancer trials in China",
            "country": "China",
        },
    )
    assert res.status_code == 422, res.text
    detail = str(res.json().get("detail", "")).lower()
    assert "after applying filters" in detail
    assert "relaxing" in detail or "try" in detail


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
