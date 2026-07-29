import pytest

from app.engine.aggregator import (
    aggregate_by_phase,
    aggregate_by_year,
    aggregate_entity_network,
    aggregate_enrollment_histogram,
    resolve_viz_type,
)
from app.engine.citations import build_supporting_excerpt, citations_for_datum
from app.engine.interpreter import (
    _apply_query_heuristics,
    _extract_temporal_bounds,
    _extract_years_from_text,
    _validate_interpretation,
)
from app.services.filters import apply_structured_filters
from app.schemas.input import QueryRequest


def _study(
    nct: str,
    *,
    title: str = "Example Trial",
    status: str = "COMPLETED",
    phases: list[str] | None = None,
    year: str = "2020-01-15",
    enrollment: int | None = 100,
    sponsor: str = "Acme Pharma",
    conditions: list[str] | None = None,
    drugs: list[str] | None = None,
    country: str = "United States",
    investigator: str | None = "Jane Doe",
) -> dict:
    interventions = [{"name": d} for d in (drugs or ["DrugX"])]
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": title},
            "statusModule": {
                "overallStatus": status,
                "startDateStruct": {"date": year},
            },
            "designModule": {
                "phases": phases or ["PHASE3"],
                "enrollmentInfo": {"count": enrollment},
            },
            "conditionsModule": {"conditions": conditions or ["Diabetes"]},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "armsInterventionsModule": {"interventions": interventions},
            "contactsLocationsModule": {
                "locations": [{"country": country}],
                "overallOfficials": [{"name": investigator}] if investigator else [],
            },
        }
    }


def test_aggregate_by_year_counts_and_ids():
    studies = [
        _study("NCT1", year="2019-05-01"),
        _study("NCT2", year="2019-08-01"),
        _study("NCT3", year="2020-01-01"),
    ]
    rows = aggregate_by_year(studies)
    assert rows[0]["year"] == "2019"
    assert rows[0]["trial_count"] == 2
    assert rows[0]["contributing_count"] == 2
    assert set(rows[0]["_study_ids"]) == {"NCT1", "NCT2"}


def test_aggregate_by_phase():
    studies = [
        _study("NCT1", phases=["PHASE2"]),
        _study("NCT2", phases=["PHASE2"]),
        _study("NCT3", phases=["PHASE3"]),
    ]
    rows = aggregate_by_phase(studies)
    assert rows[0]["phase"].startswith("Phase")
    assert rows[0]["trial_count"] == 2


def test_enrollment_histogram_bins():
    studies = [
        _study("NCT1", enrollment=20),
        _study("NCT2", enrollment=80),
        _study("NCT3", enrollment=80),
    ]
    rows = aggregate_enrollment_histogram(studies)
    labels = {r["enrollment_bin"]: r["trial_count"] for r in rows}
    assert labels["0–50"] == 1
    assert labels["51–100"] == 2


def test_drug_drug_network_pairs():
    studies = [
        _study("NCT1", drugs=["A", "B", "C"]),
        _study("NCT2", drugs=["A", "B"]),
        _study("NCT3", drugs=["A"]),
    ]
    rows = aggregate_entity_network(studies, mode="drug_drug", top_n=5)
    assert rows
    assert rows[0]["source"].startswith("Drug:")
    assert rows[0]["target"].startswith("Drug:")
    assert rows[0]["edge_weight"] >= 1


def test_phase_by_drug_grouped():
    from app.engine.aggregator import aggregate_phase_by_drug
    studies = [
        _study("NCT1", phases=["PHASE2"], drugs=["Pembrolizumab"]),
        _study("NCT2", phases=["PHASE2"], drugs=["Nivolumab"]),
        _study("NCT3", phases=["PHASE3"], drugs=["Pembrolizumab"]),
    ]
    rows = aggregate_phase_by_drug(studies)
    assert any(r["drug"] == "Pembrolizumab" for r in rows)
    assert any(r["phase"].startswith("Phase") for r in rows)


def test_resolve_viz_type_constraints():
    assert resolve_viz_type("by_year", "pie_chart") == "time_series"
    assert resolve_viz_type("by_status", "pie_chart") == "pie_chart"
    assert resolve_viz_type("drug_investigator_network", "bar_chart") == "network_graph"


def test_citation_excerpt_includes_supporting_api_field():
    study = _study("NCT99", phases=["PHASE3"], year="2018-04-01")
    excerpt = build_supporting_excerpt(study, "by_phase", {"phase": "Phase 3"})
    assert "identificationModule.nctId=NCT99" in excerpt
    assert "designModule.phases=" in excerpt
    assert "PHASE3" in excerpt

    year_excerpt = build_supporting_excerpt(study, "by_year", {"year": "2018"})
    assert "statusModule.startDateStruct.date=2018-04-01" in year_excerpt


def test_citations_for_datum_returns_nct_url_and_excerpt():
    studies = [_study("NCT10"), _study("NCT11")]
    study_map = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in studies}
    cites = citations_for_datum(
        ["NCT10", "NCT11"],
        study_map,
        aggregation="by_status",
        datum={"status": "Completed"},
    )
    assert cites is not None
    assert len(cites) == 2
    assert cites[0].nct_id == "NCT10"
    assert cites[0].url == "https://clinicaltrials.gov/study/NCT10"
    assert "overallStatus=" in cites[0].excerpt
    assert "identificationModule.nctId=NCT10" in cites[0].excerpt


def test_validate_interpretation_clamps_unknown_enums():
    result = _validate_interpretation(
        {
            "search_params": {"cond": "Diabetes", "bogus": "x"},
            "aggregation": "made_up",
            "viz_type": "sparkles",
            "intent": "test",
        }
    )
    assert result["aggregation"] == "by_status"
    assert result["viz_type"] is None
    assert result["search_params"] == {"cond": "Diabetes"}
    assert result["needs_visualization"] is True


def test_extract_years_since_2015():
    start, end = _extract_years_from_text(
        "How has the number of trials for Pembrolizumab changed per year since 2015?"
    )
    assert start == 2015
    assert end is None


def test_extract_years_between_and_last_n():
    assert _extract_years_from_text("trials between 2015 and 2020") == (2015, 2020)
    assert _extract_years_from_text("in the last 5 years") == (2021, None)


def test_heuristics_force_start_year_from_query_text():
    # Even if the LLM omits or invents a wrong start_year, text wins.
    result = _apply_query_heuristics(
        "How has the number of trials for Pembrolizumab changed per year since 2015?",
        {
            "search_params": {"intr": "Pembrolizumab"},
            "aggregation": "by_status",
            "viz_type": "pie_chart",
            "start_year": None,
            "end_year": None,
        },
    )
    assert result["start_year"] == 2015
    assert result["aggregation"] == "by_year"
    assert result["viz_type"] == "time_series"


def test_apply_structured_filters_honors_interpreted_start_year():
    studies = [
        _study("NCT1", year="2012-01-01"),
        _study("NCT2", year="2015-06-01"),
        _study("NCT3", year="2018-03-01"),
    ]
    request = QueryRequest(query="since 2015")
    filtered = apply_structured_filters(studies, request, start_year_override=2015)
    years = [
        s["protocolSection"]["statusModule"]["startDateStruct"]["date"][:4]
        for s in filtered
    ]
    assert years == ["2015", "2018"]


def test_format_phase_labels():
    from app.engine.aggregator import _format_phase

    assert _format_phase("EARLY_PHASE1") == "Early Phase 1"
    assert _format_phase("PHASE1") == "Phase 1"
    assert _format_phase("PHASE3") == "Phase 3"
    assert _format_phase(None) == "NA"


def test_extract_status_recruiting_from_query():
    from app.engine.interpreter import _extract_status_from_text, _apply_query_heuristics

    assert _extract_status_from_text(
        "Which countries have the most recruiting trials for lung cancer?"
    ) == "RECRUITING"
    assert _extract_status_from_text("active, not recruiting trials") == "ACTIVE_NOT_RECRUITING"

    result = _apply_query_heuristics(
        "Which countries have the most recruiting trials for lung cancer?",
        {"search_params": {"cond": "lung cancer"}, "aggregation": "by_status", "viz_type": None},
    )
    assert result["search_params"]["status"] == "RECRUITING"
    assert result["aggregation"] == "by_location"


def test_aggregate_by_location_skips_unspecified():
    from app.engine.aggregator import aggregate_by_location as agg_loc

    studies = [
        _study("NCT1", country="United States"),
        _study("NCT2", country="China"),
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT3", "briefTitle": "No site"},
                "statusModule": {"overallStatus": "RECRUITING", "startDateStruct": {"date": "2020"}},
                "designModule": {"phases": ["PHASE2"], "enrollmentInfo": {"count": 10}},
                "conditionsModule": {"conditions": ["Lung Cancer"]},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme"}},
                "armsInterventionsModule": {"interventions": [{"name": "DrugX"}]},
                "contactsLocationsModule": {"locations": [], "overallOfficials": []},
            }
        },
    ]
    rows = agg_loc(studies, top_n=10)
    countries = [r["country"] for r in rows]
    assert "Unspecified" not in countries
    assert "United States" in countries


def test_extract_compared_drugs_vs_pattern():
    from app.engine.interpreter import _apply_query_heuristics, _extract_compared_drugs

    assert _extract_compared_drugs(
        "Compare phases for Pembrolizumab vs Nivolumab"
    ) == ["Pembrolizumab", "Nivolumab"]

    result = _apply_query_heuristics(
        "Compare phases for Pembrolizumab vs Nivolumab",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert result["aggregation"] == "phase_by_drug"
    assert result["focus_drugs"] == ["Pembrolizumab", "Nivolumab"]
    assert "Nivolumab" in result["search_params"]["intr"]
    assert "Pembrolizumab" in result["search_params"]["intr"]


def test_get_drugs_case_folds():
    from app.engine.aggregator import get_drugs

    study = _study("NCT1", drugs=["Pembrolizumab", "pembrolizumab", "Carboplatin"])
    names = get_drugs(study, limit=5)
    assert names.count("Pembrolizumab") == 1
    assert "Carboplatin" in names


def test_phase_by_drug_respects_focus_drugs():
    from app.engine.aggregator import aggregate_phase_by_drug

    studies = [
        _study("NCT1", phases=["PHASE2"], drugs=["Pembrolizumab", "Carboplatin"]),
        _study("NCT2", phases=["PHASE2"], drugs=["Nivolumab", "Paclitaxel"]),
        _study("NCT3", phases=["PHASE3"], drugs=["Pembrolizumab"]),
        _study("NCT4", phases=["PHASE3"], drugs=["Nivolumab"]),
    ]
    rows = aggregate_phase_by_drug(
        studies,
        focus_drugs=["Pembrolizumab", "Nivolumab"],
    )
    drugs = {r["drug"] for r in rows}
    assert drugs == {"Pembrolizumab", "Nivolumab"}
    assert "Carboplatin" not in drugs
    assert "Paclitaxel" not in drugs


def test_extract_years_last_6_months(monkeypatch):
    monkeypatch.setenv("CLINSIGHT_REFERENCE_DATE", "2026-07-29")
    start, end, month = _extract_temporal_bounds("trials in the last 6 months")
    assert start == 2026
    assert end is None
    assert month == 2


def test_extract_years_last_18_months(monkeypatch):
    monkeypatch.setenv("CLINSIGHT_REFERENCE_DATE", "2026-07-29")
    start, end, month = _extract_temporal_bounds("studies from the last 18 months")
    assert start == 2025
    assert end is None
    assert month == 2


def test_extract_years_last_weeks(monkeypatch):
    monkeypatch.setenv("CLINSIGHT_REFERENCE_DATE", "2026-07-29")
    start, end, month = _extract_temporal_bounds("recruiting in the last 12 weeks")
    assert start == 2026
    assert end is None
    assert month is not None


def test_extract_compared_drugs_rejects_early_vs_late_phase():
    from app.engine.interpreter import _extract_compared_drugs

    assert _extract_compared_drugs(
        "Compare enrollment sizes for early vs late phase"
    ) == []


def test_heuristics_early_vs_late_phase_routes_to_enrollment():
    from app.engine.interpreter import _apply_query_heuristics

    result = _apply_query_heuristics(
        "Compare enrollment sizes for early vs late phase",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert result["aggregation"] == "enrollment_by_phase_group"
    assert result["viz_type"] == "grouped_bar_chart"
    assert result.get("focus_drugs") is None


def test_apply_structured_filters_honors_start_month():
    studies = [
        _study("NCT1", year="2026-01-15"),
        _study("NCT2", year="2026-02-10"),
        _study("NCT3", year="2026-03-01"),
    ]
    request = QueryRequest(query="last 6 months")
    filtered = apply_structured_filters(
        studies,
        request,
        start_year_override=2026,
        start_month_override=2,
    )
    ncts = [s["protocolSection"]["identificationModule"]["nctId"] for s in filtered]
    assert ncts == ["NCT2", "NCT3"]


def test_enrollment_by_phase_group_aggregation():
    from app.engine.aggregator import aggregate_enrollment_by_phase_group

    studies = [
        _study("NCT1", phases=["PHASE1"], enrollment=30),
        _study("NCT2", phases=["PHASE1"], enrollment=80),
        _study("NCT3", phases=["PHASE3"], enrollment=30),
        _study("NCT4", phases=["PHASE3"], enrollment=500),
    ]
    rows = aggregate_enrollment_by_phase_group(studies)
    keys = {(r["phase_group"], r["enrollment_bin"]): r["trial_count"] for r in rows}
    assert keys[("Early phase", "0–50")] == 1
    assert keys[("Early phase", "51–100")] == 1
    assert keys[("Late phase", "0–50")] == 1
    assert keys[("Late phase", "251–500")] == 1


def test_intervention_filter_key_for_radiation():
    from app.services.filters import intervention_filter_key

    assert intervention_filter_key("Radiation Therapy") == "intervention"
    assert intervention_filter_key("Pembrolizumab") == "drug_name"


def test_get_drugs_filters_non_pharmacological():
    from app.engine.aggregator import get_drugs

    study = _study(
        "NCT1",
        drugs=["Pembrolizumab", "Biospecimen Collection", "Computed Tomography", "Carboplatin"],
    )
    names = get_drugs(study, limit=10)
    assert "Pembrolizumab" in names
    assert "Carboplatin" in names
    assert "Biospecimen Collection" not in names
    assert "Computed Tomography" not in names


def test_interpretation_rejects_gibberish_single_word():
    from app.engine.interpreter import QueryInterpretationError, validate_actionable_query

    interpretation = {
        "search_params": {},
        "aggregation": "by_status",
        "viz_type": "pie_chart",
        "needs_visualization": True,
    }
    request = QueryRequest(query="shuham")
    try:
        validate_actionable_query("shuham", request, interpretation)
        assert False, "expected QueryInterpretationError"
    except QueryInterpretationError as exc:
        assert exc.status_code == 422
        assert "clinical trials" in str(exc).lower()


def test_interpretation_allows_explicit_status_overview():
    from app.engine.interpreter import validate_actionable_query

    interpretation = {
        "search_params": {},
        "aggregation": "by_status",
        "viz_type": "pie_chart",
        "needs_visualization": True,
    }
    request = QueryRequest(query="Show the distribution of trial statuses")
    validate_actionable_query(request.query, request, interpretation)


def test_interpretation_allows_recruiting_filter():
    from app.engine.interpreter import validate_actionable_query

    interpretation = {
        "search_params": {"status": "RECRUITING"},
        "aggregation": "by_status",
        "viz_type": "pie_chart",
        "needs_visualization": True,
    }
    request = QueryRequest(query="recruiting trials")
    validate_actionable_query(request.query, request, interpretation)


def test_interpretation_infers_drug_like_token():
    from app.engine.interpreter import validate_actionable_query

    interpretation = {
        "search_params": {},
        "aggregation": "by_phase",
        "viz_type": "bar_chart",
        "needs_visualization": True,
    }
    request = QueryRequest(query="pembrolizumab")
    validate_actionable_query(request.query, request, interpretation)
    assert interpretation["search_params"]["intr"] == "pembrolizumab"


def test_interpretation_honors_needs_visualization_false():
    from app.engine.interpreter import QueryInterpretationError, validate_actionable_query

    interpretation = {
        "search_params": {},
        "needs_visualization": False,
        "rejection_reason": "That looks like a name, not a trials question.",
    }
    request = QueryRequest(query="shuham")
    try:
        validate_actionable_query("shuham", request, interpretation)
        assert False, "expected QueryInterpretationError"
    except QueryInterpretationError as exc:
        assert "name" in str(exc).lower()


def test_strip_hallucinated_pembrolizumab_for_melanoma_query():
    from app.engine.interpreter import _apply_query_heuristics, _enrich_search_params_from_text, _validate_interpretation

    llm_result = {
        "search_params": {"intr": "Pembrolizumab", "cond": "Melanoma"},
        "aggregation": "by_phase",
        "viz_type": "bar_chart",
        "needs_visualization": True,
    }
    query = "early phase vs late phase trials for melanoma"
    interpretation = _apply_query_heuristics(query, _validate_interpretation(llm_result))
    request = QueryRequest(query=query)
    _enrich_search_params_from_text(query, request, interpretation)
    assert interpretation["aggregation"] == "by_phase_group"
    assert "intr" not in interpretation["search_params"]
    assert interpretation["search_params"]["cond"] == "Melanoma"


def test_strip_hallucinated_pembrolizumab_for_cll_query():
    from app.engine.interpreter import _apply_query_heuristics, _enrich_search_params_from_text, _validate_interpretation

    llm_result = {
        "search_params": {"intr": "Pembrolizumab"},
        "aggregation": "by_status",
        "viz_type": "pie_chart",
        "needs_visualization": True,
    }
    query = "trials for CLL"
    interpretation = _apply_query_heuristics(query, _validate_interpretation(llm_result))
    request = QueryRequest(query=query)
    _enrich_search_params_from_text(query, request, interpretation)
    assert "intr" not in interpretation["search_params"]
    assert interpretation["search_params"]["cond"] == "Chronic Lymphocytic Leukemia"


def test_drug_network_for_glioblastoma_routes_correctly():
    from app.engine.interpreter import _apply_query_heuristics, _enrich_search_params_from_text, _validate_interpretation

    llm_result = {
        "search_params": {"intr": "Pembrolizumab", "cond": "Glioblastoma"},
        "aggregation": "drug_condition_network",
        "viz_type": "network_graph",
        "needs_visualization": True,
    }
    query = "drug network for glioblastoma"
    interpretation = _apply_query_heuristics(query, _validate_interpretation(llm_result))
    request = QueryRequest(query=query)
    _enrich_search_params_from_text(query, request, interpretation)
    assert interpretation["aggregation"] == "drug_drug_network"
    assert "intr" not in interpretation["search_params"]
    assert interpretation["search_params"]["cond"] == "Glioblastoma"


def test_pfizer_nsclc_extracts_sponsor_and_condition():
    from app.engine.interpreter import _apply_query_heuristics, _enrich_search_params_from_text, _validate_interpretation

    llm_result = {
        "search_params": {"intr": "Pembrolizumab", "cond": "NSCLC"},
        "aggregation": "by_status",
        "viz_type": "pie_chart",
        "needs_visualization": True,
    }
    query = "trials sponsored by Pfizer for NSCLC"
    interpretation = _apply_query_heuristics(query, _validate_interpretation(llm_result))
    request = QueryRequest(query=query)
    _enrich_search_params_from_text(query, request, interpretation)
    assert "intr" not in interpretation["search_params"]
    assert interpretation["search_params"]["cond"] == "Non-small Cell Lung Cancer"
    assert interpretation["sponsor"] == "Pfizer"


def test_by_phase_group_aggregation():
    from app.engine.aggregator import aggregate_by_phase_group

    studies = [
        _study("NCT1", phases=["PHASE1"]),
        _study("NCT2", phases=["PHASE1"]),
        _study("NCT3", phases=["PHASE3"]),
        _study("NCT4", phases=["PHASE4"]),
        _study("NCT5", phases=["PHASE2"]),
    ]
    rows = aggregate_by_phase_group(studies)
    counts = {r["phase_group"]: r["trial_count"] for r in rows}
    assert counts["Early phase"] == 2
    assert counts["Mid phase"] == 1
    assert counts["Late phase"] == 2


def test_sponsor_ranking_vs_sponsored_by_overview():
    """Ranking sponsors ≠ status pie of a named-sponsor corpus."""
    from app.engine.interpreter import _apply_query_heuristics

    ranked = _apply_query_heuristics(
        "Which sponsors have the most clinical trials for diabetes?",
        {"search_params": {"cond": "Diabetes"}, "aggregation": "by_status", "viz_type": "pie_chart"},
    )
    assert ranked["aggregation"] == "by_sponsor"
    assert ranked["viz_type"] == "bar_chart"

    filtered = _apply_query_heuristics(
        "trials sponsored by Pfizer for NSCLC",
        {"search_params": {}, "aggregation": "by_phase", "viz_type": "bar_chart"},
    )
    assert filtered["aggregation"] == "by_status"
    assert filtered["viz_type"] == "pie_chart"


def test_intent_routes_scatter_histogram_and_networks():
    from app.engine.interpreter import _apply_query_heuristics

    scatter = _apply_query_heuristics(
        "Show start year versus enrollment for pembrolizumab trials",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert scatter["aggregation"] == "year_enrollment_scatter"

    hist = _apply_query_heuristics(
        "Show the distribution of enrollment sizes for diabetes trials",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert hist["aggregation"] == "enrollment_histogram"

    cooccur = _apply_query_heuristics(
        "Show drug-to-drug co-occurrence network for melanoma",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert cooccur["aggregation"] == "drug_drug_network"

    site = _apply_query_heuristics(
        "Show a network of sponsors and countries for diabetes trials",
        {"search_params": {}, "aggregation": "by_status", "viz_type": None},
    )
    assert site["aggregation"] == "sponsor_site_network"


def test_compare_phases_does_not_set_spurious_condition():
    from app.engine.interpreter import (
        _apply_query_heuristics,
        _enrich_search_params_from_text,
        _extract_condition_from_text,
        _validate_interpretation,
    )

    query = "Compare phases for Pembrolizumab vs Nivolumab"
    assert _extract_condition_from_text(query) is None

    interpretation = _apply_query_heuristics(
        query,
        _validate_interpretation(
            {
                "search_params": {"cond": "Pembrolizumab vs Nivolumab", "intr": "Pembrolizumab OR Nivolumab"},
                "aggregation": "phase_by_drug",
                "viz_type": "grouped_bar_chart",
                "needs_visualization": True,
            }
        ),
    )
    request = QueryRequest(query=query)
    _enrich_search_params_from_text(query, request, interpretation)
    assert interpretation["focus_drugs"] == ["Pembrolizumab", "Nivolumab"]
    assert "cond" not in interpretation["search_params"]


def test_country_aliases_normalize():
    from app.engine.aggregator import normalize_country_name, countries_match

    assert normalize_country_name("usa") == "United States"
    assert normalize_country_name("UK") == "United Kingdom"
    assert countries_match("usa", "United States")
    assert countries_match("China", "china")
    assert not countries_match("China", "Taiwan")


def test_study_matches_country_filter_any_site():
    from app.engine.aggregator import study_matches_country_filter

    study = {
        "protocolSection": {
            "contactsLocationsModule": {
                "locations": [
                    {"country": "Taiwan"},
                    {"country": "China"},
                    {"country": "United States"},
                ]
            }
        }
    }
    assert study_matches_country_filter(study, "china")
    assert study_matches_country_filter(study, "usa")
    assert not study_matches_country_filter(study, "India")


def test_aggregate_by_location_counts_all_countries():
    from app.engine.aggregator import aggregate_by_location

    studies = [
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT1"},
                "contactsLocationsModule": {
                    "locations": [{"country": "Taiwan"}, {"country": "China"}]
                },
            }
        },
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT2"},
                "contactsLocationsModule": {
                    "locations": [{"country": "China"}]
                },
            }
        },
    ]
    # Without filter: both countries counted (multi-site contributes to each).
    rows = {r["country"]: r["trial_count"] for r in aggregate_by_location(studies)}
    assert rows["China"] == 2
    assert rows["Taiwan"] == 1

    # With China filter: only China bar (no Taiwan leak).
    filtered = aggregate_by_location(studies, country_filter="china")
    assert len(filtered) == 1
    assert filtered[0]["country"] == "China"
    assert filtered[0]["trial_count"] == 2


def test_apply_structured_filters_country_post_filter():
    from app.services.filters import apply_structured_filters
    from app.schemas.input import QueryRequest

    studies = [
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT_CN"},
                "contactsLocationsModule": {
                    "locations": [{"country": "United States"}, {"country": "China"}]
                },
                "statusModule": {"startDateStruct": {"date": "2020"}},
            }
        },
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT_US"},
                "contactsLocationsModule": {
                    "locations": [{"country": "United States"}]
                },
                "statusModule": {"startDateStruct": {"date": "2020"}},
            }
        },
    ]
    request = QueryRequest(query="countries", country="china")
    filtered = apply_structured_filters(studies, request)
    assert len(filtered) == 1
    assert filtered[0]["protocolSection"]["identificationModule"]["nctId"] == "NCT_CN"


def test_last_six_months_uses_reference_date(monkeypatch):
    from datetime import date
    import app.config as config
    import app.engine.interpreter as interpreter

    monkeypatch.setenv("CLINSIGHT_REFERENCE_DATE", "2026-07-29")
    # Clear any cached behavior by calling through config helper.
    assert config.get_reference_date() == date(2026, 7, 29)
    start, end, month = interpreter._extract_temporal_bounds("trials in the last 6 months")
    assert start == 2026
    assert end is None
    assert month == 2


def test_ct_client_builds_lead_and_phase_params():
    from app.api.clinical_trials import ClinicalTrialsClient

    client = ClinicalTrialsClient(max_retries=0)
    params = client._build_params(lead="Pfizer", phase="PHASE3", cond="NSCLC", page_size=50)
    assert params["query.lead"] == "Pfizer"
    assert params["filter.phase"] == "PHASE3"
    assert params["query.cond"] == "NSCLC"


def test_get_drugs_filters_questionnaire_noise():
    from app.engine.aggregator import get_drugs

    study = {
        "protocolSection": {
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "Temozolomide"},
                    {"type": "OTHER", "name": "Quality-of-Life Assessment"},
                    {"type": "OTHER", "name": "Questionnaire Administration"},
                    {"type": "DRUG", "name": "Bevacizumab"},
                ]
            }
        }
    }
    drugs = get_drugs(study, limit=10)
    assert "Temozolomide" in drugs
    assert "Bevacizumab" in drugs
    assert not any("Quality" in d for d in drugs)
    assert not any("Questionnaire" in d for d in drugs)


def test_strip_ungrounded_entities_removes_invented_filters():
    from app.engine.interpreter import _enrich_search_params_from_text, _validate_interpretation
    from app.schemas.input import QueryRequest

    interpretation = _validate_interpretation(
        {
            "search_params": {
                "cond": "InventedDiseaseXYZ",
                "locn": "Atlantis",
                "term": "secret-token-abc",
                "intr": "Pembrolizumab",
            },
            "aggregation": "by_status",
            "viz_type": "pie_chart",
            "needs_visualization": True,
            "sponsor": "FakePharmaCorp",
        }
    )
    request = QueryRequest(query="trials for diabetes")
    _enrich_search_params_from_text(request.query, request, interpretation)
    params = interpretation["search_params"]
    assert "intr" not in params  # pembro not in query
    assert params.get("locn") is None or "Atlantis" not in str(params.get("locn"))
    assert params.get("term") is None
    assert interpretation.get("sponsor") in (None,)
    # diabetes should be filled from text
    assert "diabetes" in str(params.get("cond") or "").lower()


def test_template_title_and_notes_are_filter_grounded():
    from app.engine.labels import (
        build_template_title,
        resolve_title_and_notes,
        build_deterministic_encoding,
        text_mentions_ungrounded_entity,
    )

    filters = {"condition": "Lung Cancer", "status": "RECRUITING"}
    title = build_template_title("by_location", filters)
    assert "Lung Cancer" in title
    assert "Recruiting" in title or "RECRUITING" in title

    assert text_mentions_ungrounded_entity(
        "Pembrolizumab Trends for Lung Cancer",
        filters,
    )
    safe_title, notes = resolve_title_and_notes(
        llm_title="Pembrolizumab Trends for Lung Cancer",
        llm_notes="Includes Merck data",
        aggregation="by_location",
        filters=filters,
        aggregated_data=[{"country": "United States", "trial_count": 3}],
        total_count=3,
    )
    assert "Pembrolizumab" not in safe_title
    assert "Merck" not in notes

    enc = build_deterministic_encoding(
        viz_type="bar_chart",
        x_field="country",
        y_field="trial_count",
        x_type="nominal",
        y_type="quantitative",
    )
    assert enc["x"]["field"] == "country"
    assert enc["y"]["field"] == "trial_count"


def test_citation_excerpt_no_first_item_fallback():
    from app.engine.citations import build_supporting_excerpt

    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT1", "briefTitle": "Example"},
            "conditionsModule": {"conditions": ["Diabetes", "Obesity"]},
            "contactsLocationsModule": {"locations": [{"country": "France"}]},
            "armsInterventionsModule": {
                "interventions": [{"name": "Metformin"}, {"name": "Placebo"}]
            },
        }
    }
    # Asking for China should NOT fall back to France.
    loc_excerpt = build_supporting_excerpt(study, "by_location", {"country": "China"})
    assert "France" not in loc_excerpt
    assert "identificationModule.nctId=NCT1" in loc_excerpt

    # Exact country match still works.
    fr = build_supporting_excerpt(study, "by_location", {"country": "France"})
    assert "France" in fr

    drug_excerpt = build_supporting_excerpt(study, "by_drug", {"drug": "Pembrolizumab"})
    assert "Metformin" not in drug_excerpt
    # Case-insensitive drug grounding still cites the real intervention name.
    met = build_supporting_excerpt(study, "by_drug", {"drug": "metformin"})
    assert "Metformin" in met


def test_optional_filter_rejects_invalid_status():
    from pydantic import ValidationError
    from app.schemas.input import QueryRequest

    with pytest.raises(ValidationError) as exc:
        QueryRequest(
            query="Which countries have the most recruiting trials for lung cancer?",
            status="fuck",
        )
    assert "Invalid status" in str(exc.value)


def test_apply_structured_filters_status_post_filter():
    """Status must be enforced locally, not only via CT.gov query params."""
    studies = [
        _study("NCT1", status="RECRUITING"),
        _study("NCT2", status="COMPLETED"),
        _study("NCT3", status="TERMINATED"),
    ]
    request = QueryRequest(query="recruiting trials", status="RECRUITING")
    kept = apply_structured_filters(studies, request)
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in kept] == ["NCT1"]


def test_usa_country_alias_normalizes():
    from app.engine.study_fields import normalize_country_name
    from app.engine import interpreter as interp

    assert normalize_country_name("USa") == "United States"
    assert normalize_country_name("USA") == "United States"
    # Regression: interpret_query country path must keep this import wired.
    assert callable(interp.normalize_country_name)


def test_trend_end_year_uses_reference_date(monkeypatch):
    from datetime import date
    from app.services.fetch import trend_end_year

    monkeypatch.setenv("CLINSIGHT_REFERENCE_DATE", "2020-06-15")
    # Reload get_reference_date path — trend_end_year calls get_reference_date each time.
    from app.config import get_reference_date

    assert get_reference_date() == date(2020, 6, 15)
    assert trend_end_year(None) == 2021
    assert trend_end_year(2018) == 2018


def test_optional_filter_rejects_invalid_phase():
    from pydantic import ValidationError
    from app.schemas.input import QueryRequest

    with pytest.raises(ValidationError) as exc:
        QueryRequest(query="lung cancer trials", trial_phase="banana")
    assert "Invalid phase" in str(exc.value)


def test_optional_filter_accepts_status_and_phase_aliases():
    from app.schemas.input import QueryRequest

    req = QueryRequest(
        query="Which countries have the most recruiting trials for lung cancer?",
        status="Recruiting",
        trial_phase="Phase 3",
    )
    assert req.status == "RECRUITING"
    assert req.trial_phase == "PHASE3"


def test_optional_filter_rejects_inverted_year_range():
    from pydantic import ValidationError
    from app.schemas.input import QueryRequest

    with pytest.raises(ValidationError) as exc:
        QueryRequest(query="diabetes trials", start_year=2024, end_year=2015)
    assert "start_year" in str(exc.value).lower() or "cannot be after" in str(exc.value)


def test_optional_filter_rejects_blank_gibberish_entity():
    from pydantic import ValidationError
    from app.schemas.input import QueryRequest

    with pytest.raises(ValidationError):
        QueryRequest(query="lung cancer trials", drug_name="a")

    with pytest.raises(ValidationError):
        QueryRequest(query="lung cancer trials", condition="@@@")


def test_optional_filter_full_bad_payload_does_not_construct():
    """Mirrors the demo form filled with invalid Status=fuck plus other filters."""
    from pydantic import ValidationError
    from app.schemas.input import QueryRequest

    with pytest.raises(ValidationError) as exc:
        QueryRequest(
            query="Which countries have the most recruiting trials for lung cancer?",
            drug_name="Pembrolizumab",
            condition="Diabetes",
            trial_phase="Phase 3",
            sponsor="Pfizer",
            country="United States",
            status="fuck",
            start_year=2015,
            end_year=2024,
        )
    assert "Invalid status" in str(exc.value)


def test_optional_filter_valid_combo_constructs():
    from app.schemas.input import QueryRequest

    req = QueryRequest(
        query="Which countries have the most recruiting trials for lung cancer?",
        condition="Lung Cancer",
        status="RECRUITING",
    )
    assert req.status == "RECRUITING"
    assert req.condition == "Lung Cancer"


def test_build_start_date_advanced_filter_last_six_months():
    from app.services.filters import build_start_date_advanced_filter

    assert build_start_date_advanced_filter(2026, 2, None) == "AREA[StartDate]RANGE[2026-02-01,MAX]"


def test_trend_fetch_sort_avoids_newest_first_bias():
    from app.services.fetch import resolve_study_fetch_sort

    assert resolve_study_fetch_sort("by_year", "AREA[StartDate]RANGE[2015-01-01,MAX]") is None
    assert resolve_study_fetch_sort("by_status", "AREA[StartDate]RANGE[2026-02-01,MAX]") == "StartDate:desc"
    assert resolve_study_fetch_sort("by_status", None) is None


def test_trend_fetch_limit_defaults_high():
    from app.services.fetch import resolve_study_fetch_limit

    assert resolve_study_fetch_limit("by_year", None) == 5000
    assert resolve_study_fetch_limit("by_status", None) is None
    assert resolve_study_fetch_limit("by_year", 800) == 800


@pytest.mark.asyncio
async def test_year_bucketed_fetch_queries_each_calendar_year(monkeypatch):
    from app.api.clinical_trials import ClinicalTrialsClient
    from app.services.fetch import fetch_studies_for_query
    from app.schemas.input import QueryRequest

    client = ClinicalTrialsClient(max_retries=0)
    calls: list[dict] = []

    async def fake_paginated(**kwargs):
        calls.append(kwargs)
        year_filter = kwargs.get("advanced_filter") or ""
        year = year_filter.split("[")[2][:4] if "RANGE[" in year_filter else "0000"
        return {
            "studies": [{"protocolSection": {"identificationModule": {"nctId": f"NCT{year}"}}}],
            "totalCount": 1,
            "truncated": False,
        }

    monkeypatch.setattr(client, "search_studies_paginated", fake_paginated)

    request = QueryRequest(query="Pembrolizumab per year since 2015", drug_name="Pembrolizumab")
    result = await fetch_studies_for_query(
        client,
        search_params={"intr": "Pembrolizumab"},
        request=request,
        focus_drugs=[],
        fields="protocolSection.identificationModule.nctId",
        aggregation="by_year",
        effective_start_year=2015,
        effective_end_year=2017,
        sort=None,
        max_studies=5000,
    )

    assert result["fetchedCount"] == 3
    assert len(calls) == 3
    assert calls[0]["advanced_filter"] == "AREA[StartDate]RANGE[2015-01-01,2015-12-31]"
    assert calls[1]["advanced_filter"] == "AREA[StartDate]RANGE[2016-01-01,2016-12-31]"
    assert calls[2]["advanced_filter"] == "AREA[StartDate]RANGE[2017-01-01,2017-12-31]"
    assert all(call["sort"] is None for call in calls)


def test_study_matches_condition_filter_excludes_without_diabetes():
    from app.engine.aggregator import study_matches_condition_filter

    obesity_only = {
        "protocolSection": {
            "conditionsModule": {"conditions": ["Obesity", "Overweight"]},
        }
    }
    diabetes_study = {
        "protocolSection": {
            "conditionsModule": {"conditions": ["Type 2 Diabetes Mellitus"]},
        }
    }
    assert not study_matches_condition_filter(obesity_only, "Diabetes")
    assert study_matches_condition_filter(diabetes_study, "Diabetes")


def test_study_matches_condition_filter_accepts_type2_diabetes():
    from app.engine.aggregator import study_matches_condition_filter

    study = {
        "protocolSection": {
            "conditionsModule": {"conditions": ["Diabetes Mellitus, Type 2"]},
        }
    }
    assert study_matches_condition_filter(study, "Diabetes")


def test_normalize_condition_collapses_whitespace():
    from app.engine.aggregator import normalize_condition_name

    assert normalize_condition_name("Non-small Cell  Lung Cancer") == "Non-small Cell Lung Cancer"
    assert normalize_condition_name("  Diabetes  Type 2  ") == "Diabetes Type 2"


def test_normalize_condition_maps_synonyms():
    from app.engine.aggregator import normalize_condition_name

    assert normalize_condition_name("Carcinoma, Non-Small-Cell Lung") == "Non-small Cell Lung Cancer"
    assert normalize_condition_name("Non Small Cell Lung Cancer") == "Non-small Cell Lung Cancer"
    assert normalize_condition_name("Non-Small Cell Lung Cancer") == "Non-small Cell Lung Cancer"
    assert normalize_condition_name("Non-small Cell Lung Cancer") == "Non-small Cell Lung Cancer"


def test_get_conditions_deduplicates_variants():
    from app.engine.aggregator import get_conditions

    raw_study = {
        "protocolSection": {
            "conditionsModule": {
                "conditions": [
                    "Non-small Cell Lung Cancer",
                    "Non Small Cell Lung Cancer",
                    "Carcinoma, Non-Small-Cell Lung",
                    "Non-Small Cell Lung Cancer",
                    "Diabetes",
                ]
            }
        }
    }
    conds = get_conditions(raw_study, limit=10)
    assert conds.count("Non-small Cell Lung Cancer") == 1
    assert "Diabetes" in conds
