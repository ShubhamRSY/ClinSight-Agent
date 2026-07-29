import pytest

from app.api.clinical_trials import ClinicalTrialsClient


@pytest.mark.asyncio
async def test_search_studies_paginated_follows_tokens(monkeypatch):
    client = ClinicalTrialsClient(page_size=2, max_studies=5, max_retries=0)

    pages = [
        {
            "studies": [{"id": 1}, {"id": 2}],
            "nextPageToken": "t1",
            "totalCount": 5,
        },
        {
            "studies": [{"id": 3}, {"id": 4}],
            "nextPageToken": "t2",
            "totalCount": 5,
        },
        {
            "studies": [{"id": 5}],
            "totalCount": 5,
        },
    ]
    calls = {"n": 0}

    async def fake_search_studies(**kwargs):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx]

    monkeypatch.setattr(client, "search_studies", fake_search_studies)
    result = await client.search_studies_paginated(cond="Diabetes")
    assert result["fetchedCount"] == 5
    assert result["totalCount"] == 5
    assert result["truncated"] is False
    assert len(result["studies"]) == 5


@pytest.mark.asyncio
async def test_search_studies_paginated_respects_max(monkeypatch):
    client = ClinicalTrialsClient(page_size=100, max_studies=3, max_retries=0)

    async def fake_search_studies(**kwargs):
        return {
            "studies": [{"id": i} for i in range(kwargs["page_size"])],
            "nextPageToken": "more",
            "totalCount": 100,
        }

    monkeypatch.setattr(client, "search_studies", fake_search_studies)
    result = await client.search_studies_paginated()
    assert result["fetchedCount"] == 3
    assert result["truncated"] is True


def test_build_params_includes_lead_sponsor():
    client = ClinicalTrialsClient(max_retries=0)
    params = client._build_params(lead="Pfizer", cond="NSCLC", page_size=10)
    assert params["query.lead"] == "Pfizer"
    assert "query.spons" not in params


def test_build_params_normalizes_phase_filter():
    client = ClinicalTrialsClient(max_retries=0)
    params = client._build_params(phase="PHASE3", page_size=10)
    assert params["filter.phase"] == "PHASE3"
    params2 = client._build_params(phase="EARLY_PHASE1,PHASE1", page_size=10)
    assert params2["filter.phase"] == "EARLY_PHASE1,PHASE1"
