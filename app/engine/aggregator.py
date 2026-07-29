"""Deterministic aggregation of ClinicalTrials.gov studies into chart rows.

Important design rule: the LLM never invents trial_count / edge_weight values.
Every bar, slice, point, and network edge is counted here from fetched studies.

Field extractors live in ``study_fields``; viz encoding maps in ``viz_maps``.
This module owns bucket/network aggregators and the ``aggregate_studies`` entrypoint.
"""

from __future__ import annotations

from collections import defaultdict

from app.engine.study_fields import (
    countries_match,
    extract_date_year,
    extract_field,
    extract_field_list,
    get_conditions,
    get_countries,
    get_drugs,
    get_enrollment,
    get_investigators,
    get_nct_id,
    get_phase,
    get_sponsor,
    _format_status,
)
from app.engine.viz_maps import (
    ALLOWED_VIZ_TYPES,
    VALID_AGGREGATIONS,
    get_field_labels,
    get_field_types,
    get_series_field,
    resolve_viz_type,
)

# Re-export study field helpers so existing ``from app.engine.aggregator import …`` keeps working.
from app.engine.study_fields import (  # noqa: F401
    CONDITION_SYNONYM_MAP,
    COUNTRY_ALIASES,
    NON_DRUG_INTERVENTIONS,
    NON_DRUG_INTERVENTION_TYPES,
    extract_date_parts,
    extract_field_list,
    get_brief_title,
    normalize_condition_name,
    normalize_country_name,
    normalize_drug_name,
    study_matches_condition_filter,
    study_matches_country_filter,
    _format_phase,
    _format_status,
)

CITATION_SAMPLE_SIZE = 8
DEFAULT_TOP_N = 15
NETWORK_TOP_N = 25
NETWORK_ENTITY_LIMIT = 3
SCATTER_POINT_LIMIT = 80

def aggregate_by_year(studies: list[dict]) -> list[dict]:
    """Count trials by start year; attach sample NCT ids for citations."""
    buckets: dict[int, list[dict]] = defaultdict(list)
    for s in studies:
        year = extract_date_year(s, "statusModule", "startDateStruct", "date")
        if year:
            buckets[year].append(s)
    result = []
    for y in sorted(buckets):
        members = buckets[y]
        result.append({
            "year": str(y),
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_phase(studies: list[dict]) -> list[dict]:
    """Count trials by protocol phase."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        buckets[get_phase(s)].append(s)
    result = []
    for p in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True):
        members = buckets[p]
        result.append({
            "phase": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_status(studies: list[dict]) -> list[dict]:
    """Count trials by overall status."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        buckets[_format_status(extract_field(s, "statusModule", "overallStatus"))].append(s)
    result = []
    for p in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True):
        members = buckets[p]
        result.append({
            "status": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_sponsor(studies: list[dict], top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """Top-N lead sponsors by trial count."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        sponsor = get_sponsor(s)
        if sponsor:
            buckets[sponsor].append(s)
    result = []
    for p in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True)[:top_n]:
        members = buckets[p]
        result.append({
            "sponsor": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_condition(studies: list[dict], top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """Top-N conditions by trial count."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        for c in get_conditions(s):
            buckets[c].append(s)
    result = []
    for p in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True)[:top_n]:
        members = buckets[p]
        result.append({
            "condition": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_location(
    studies: list[dict],
    top_n: int = DEFAULT_TOP_N,
    country_filter: str | None = None,
) -> list[dict]:
    """Count trials per country.

    A multi-site trial contributes to every unique country it lists (not only the
    first site). When country_filter is set, only that country is bucketed so a
    China filter cannot surface Taiwan/Australia/etc. from the same protocols.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        countries = get_countries(s, limit=None)
        if not countries:
            continue
        if country_filter:
            matched = [c for c in countries if countries_match(c, country_filter)]
            if matched:
                # Prefer the CT.gov spelling already on the study (China not "china").
                label = matched[0]
                buckets[label].append(s)
            continue
        for country in countries:
            buckets[country].append(s)
    result = []
    ranked = sorted(buckets, key=lambda k: len(buckets[k]), reverse=True)[:top_n]
    for p in ranked:
        members = buckets[p]
        result.append({
            "country": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_by_drug(studies: list[dict], top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """Top-N drug interventions by trial count."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in studies:
        for name in get_drugs(s):
            buckets[name].append(s)
    result = []
    for p in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True)[:top_n]:
        members = buckets[p]
        result.append({
            "drug": p,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_phase_by_status(studies: list[dict]) -> list[dict]:
    """Grouped phase × status trial counts."""
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in studies:
        key = (get_phase(s), _format_status(extract_field(s, "statusModule", "overallStatus")))
        buckets[key].append(s)
    result = []
    for (phase, status) in sorted(buckets, key=lambda k: len(buckets[k]), reverse=True):
        members = buckets[(phase, status)]
        result.append({
            "phase": phase,
            "status": status,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


_ENROLLMENT_BINS = [
    (0, 50, "0–50"),
    (51, 100, "51–100"),
    (101, 250, "101–250"),
    (251, 500, "251–500"),
    (501, 1000, "501–1000"),
    (1001, 5000, "1001–5000"),
    (5001, 10**9, "5000+"),
]


def _enrollment_bin_label(enrollment: int) -> str | None:
    """Map enrollment size to a histogram bin label."""
    for low, high, label in _ENROLLMENT_BINS:
        if low <= enrollment <= high:
            return label
    return None


def _phase_group_label(study: dict) -> str | None:
    """Map a study to Early / Mid / Late phase for phase-group charts.

    Early: Early Phase 1, Phase 1. Mid: Phase 2. Late: Phase 3, Phase 4.
    If a study lists both early and late phases, early wins (conservative).
    """
    phases = extract_field_list(study, "designModule", "phases")
    if not phases:
        return None
    normalized = {str(p).strip().upper().replace(" ", "_") for p in phases if p}
    early = {"EARLY_PHASE1", "PHASE1"}
    mid = {"PHASE2"}
    late = {"PHASE3", "PHASE4"}
    if normalized & early:
        return "Early phase"
    if normalized & late:
        return "Late phase"
    if normalized & mid:
        return "Mid phase"
    return None


def aggregate_by_phase_group(studies: list[dict]) -> list[dict]:
    """Count trials in early / mid / late phase buckets."""
    order = ("Early phase", "Mid phase", "Late phase")
    buckets: dict[str, list[dict]] = {g: [] for g in order}
    for s in studies:
        group = _phase_group_label(s)
        if group is None:
            continue
        buckets[group].append(s)

    result = []
    for group in order:
        members = buckets[group]
        if not members:
            continue
        result.append({
            "phase_group": group,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_enrollment_histogram(studies: list[dict]) -> list[dict]:
    """Histogram of enrollment sizes across studies."""
    bins = _ENROLLMENT_BINS
    buckets: dict[str, list[dict]] = {label: [] for _, _, label in bins}
    for s in studies:
        enrollment = get_enrollment(s)
        if enrollment is None:
            continue
        for low, high, label in bins:
            if low <= enrollment <= high:
                buckets[label].append(s)
                break
    result = []
    for _, _, label in bins:
        if not buckets[label]:
            continue
        members = buckets[label]
        result.append({
            "enrollment_bin": label,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_enrollment_by_phase_group(studies: list[dict]) -> list[dict]:
    """Enrollment size bins compared across early vs late phase trials."""
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in studies:
        group = _phase_group_label(s)
        enrollment = get_enrollment(s)
        if group is None or enrollment is None:
            continue
        label = _enrollment_bin_label(enrollment)
        if label is None:
            continue
        buckets[(label, group)].append(s)

    result = []
    for low, _, bin_label in _ENROLLMENT_BINS:
        for group in ("Early phase", "Late phase"):
            members = buckets.get((bin_label, group), [])
            if not members:
                continue
            result.append({
                "enrollment_bin": bin_label,
                "phase_group": group,
                "trial_count": len(members),
                "contributing_count": len(members),
                "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
            })
    return result


def aggregate_year_enrollment_scatter(studies: list[dict], limit: int = SCATTER_POINT_LIMIT) -> list[dict]:
    """Scatter rows of start year vs enrollment (capped)."""
    points = []
    for s in studies:
        year = extract_date_year(s, "statusModule", "startDateStruct", "date")
        enrollment = get_enrollment(s)
        if year is None or enrollment is None:
            continue
        nct = get_nct_id(s)
        points.append({
            "year": year,
            "enrollment": enrollment,
            "phase": get_phase(s),
            "label": nct,
            "contributing_count": 1,
            "_study_ids": [nct],
        })
    points.sort(key=lambda p: (p["year"], p["enrollment"]))
    return points[:limit]


def aggregate_phase_by_drug(
    studies: list[dict],
    top_drugs: int = 8,
    focus_drugs: list[str] | None = None,
) -> list[dict]:
    """Grouped comparison: phase counts broken down by intervention/drug.

    When focus_drugs is set (e.g. Pembrolizumab vs Nivolumab), only those
    series are shown — not co-occurring chemotherapies from the same trials.

    Drugs with zero matches produce no rows (we do not invent 0-height bars).
    """
    focus_canonical = [normalize_drug_name(d) for d in (focus_drugs or []) if normalize_drug_name(d)]
    focus_keys = {d.casefold(): d for d in focus_canonical}

    if focus_keys:
        keep = set(focus_keys.values())
    else:
        drug_freq: dict[str, int] = defaultdict(int)
        for s in studies:
            for d in get_drugs(s, limit=5):
                drug_freq[d] += 1
        keep = {
            name
            for name, _ in sorted(drug_freq.items(), key=lambda kv: kv[1], reverse=True)[:top_drugs]
        }

    def matched_drugs(study: dict) -> list[str]:
        """matched drugs."""
        found: list[str] = []
        seen = set()
        # Wider scan when comparing specific drugs so long intervention lists still match.
        for d in get_drugs(study, limit=12 if focus_keys else 5):
            if focus_keys:
                hit = None
                d_key = d.casefold()
                if d_key in focus_keys:
                    hit = focus_keys[d_key]
                else:
                    for fk, fname in focus_keys.items():
                        if fk in d_key or d_key in fk:
                            hit = fname
                            break
                if hit and hit not in seen:
                    seen.add(hit)
                    found.append(hit)
            elif d in keep and d not in seen:
                seen.add(d)
                found.append(d)
        return found

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in studies:
        phase = get_phase(s)
        for d in matched_drugs(s):
            if d in keep:
                buckets[(phase, d)].append(s)

    result = []
    for (phase, drug) in sorted(buckets, key=lambda k: (k[0], k[1])):
        members = buckets[(phase, drug)]
        result.append({
            "phase": phase,
            "drug": drug,
            "trial_count": len(members),
            "contributing_count": len(members),
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result


def aggregate_entity_network(
    studies: list[dict],
    mode: str = "drug_sponsor",
    top_n: int = NETWORK_TOP_N,
) -> list[dict]:
    """Build weighted bipartite (or drug–drug) edges from study co-occurrence.

    Edge weight = number of studies linking the two entities. Caps keep charts
    readable while still surfacing the densest relationships.
    """
    edge_studies: dict[tuple[str, str], list[dict]] = defaultdict(list)
    n = NETWORK_ENTITY_LIMIT

    for s in studies:
        left: list[str] = []
        right: list[str] = []
        if mode == "drug_sponsor":
            left = [f"Drug: {d}" for d in get_drugs(s, limit=n)]
            sponsor = get_sponsor(s)
            right = [f"Sponsor: {sponsor}"] if sponsor else []
        elif mode == "drug_condition":
            left = [f"Drug: {d}" for d in get_drugs(s, limit=n)]
            right = [f"Condition: {c}" for c in get_conditions(s, limit=n)]
        elif mode == "sponsor_condition":
            sponsor = get_sponsor(s)
            left = [f"Sponsor: {sponsor}"] if sponsor else []
            right = [f"Condition: {c}" for c in get_conditions(s, limit=n)]
        elif mode == "sponsor_site":
            sponsor = get_sponsor(s)
            left = [f"Sponsor: {sponsor}"] if sponsor else []
            right = [f"Site: {c}" for c in get_countries(s, limit=n)]
        elif mode == "drug_investigator":
            left = [f"Drug: {d}" for d in get_drugs(s, limit=n)]
            right = [f"Investigator: {n_}" for n_ in get_investigators(s, limit=n)]
        elif mode == "sponsor_investigator":
            sponsor = get_sponsor(s)
            left = [f"Sponsor: {sponsor}"] if sponsor else []
            right = [f"Investigator: {n_}" for n_ in get_investigators(s, limit=n)]
        elif mode == "drug_drug":
            drugs = get_drugs(s, limit=max(n, 5))
            # Undirected co-occurrence pairs within the same study.
            for i in range(len(drugs)):
                for j in range(i + 1, len(drugs)):
                    a, b = sorted([drugs[i], drugs[j]])
                    edge_studies[(f"Drug: {a}", f"Drug: {b}")].append(s)
            continue
        else:
            left = [f"Drug: {d}" for d in get_drugs(s, limit=n)]
            sponsor = get_sponsor(s)
            right = [f"Sponsor: {sponsor}"] if sponsor else []

        for src in left:
            for tgt in right:
                edge_studies[(src, tgt)].append(s)

    ranked = sorted(edge_studies.items(), key=lambda item: len(item[1]), reverse=True)[:top_n]
    result = []
    for (src, tgt), members in ranked:
        result.append({
            "source": src,
            "target": tgt,
            "edge_weight": len(members),
            "trial_count": len(members),
            "contributing_count": len(members),
            "label": f"{src} → {tgt}",
            "_study_ids": [get_nct_id(s) for s in members[:CITATION_SAMPLE_SIZE]],
        })
    return result

AGGREGATORS = {
    "by_year": aggregate_by_year,
    "by_phase": aggregate_by_phase,
    "by_phase_group": aggregate_by_phase_group,
    "by_status": aggregate_by_status,
    "by_sponsor": aggregate_by_sponsor,
    "by_condition": aggregate_by_condition,
    # by_location handled in aggregate_studies (needs optional country_filter)
    "by_drug": aggregate_by_drug,
    "phase_by_status": aggregate_phase_by_status,
    "phase_by_drug": aggregate_phase_by_drug,
    "enrollment_histogram": aggregate_enrollment_histogram,
    "enrollment_by_phase_group": aggregate_enrollment_by_phase_group,
    "year_enrollment_scatter": aggregate_year_enrollment_scatter,
    "drug_sponsor_network": lambda studies: aggregate_entity_network(studies, "drug_sponsor"),
    "drug_condition_network": lambda studies: aggregate_entity_network(studies, "drug_condition"),
    "sponsor_condition_network": lambda studies: aggregate_entity_network(studies, "sponsor_condition"),
    "sponsor_site_network": lambda studies: aggregate_entity_network(studies, "sponsor_site"),
    "drug_investigator_network": lambda studies: aggregate_entity_network(studies, "drug_investigator"),
    "sponsor_investigator_network": lambda studies: aggregate_entity_network(studies, "sponsor_investigator"),
    "drug_drug_network": lambda studies: aggregate_entity_network(studies, "drug_drug"),
}



def aggregate_studies(
    studies: list[dict],
    aggregation: str,
    focus_drugs: list[str] | None = None,
    country_filter: str | None = None,
) -> list[dict]:
    """Dispatch to the aggregator for ``aggregation``; unknown keys yield []."""
    if aggregation == "phase_by_drug":
        return aggregate_phase_by_drug(studies, focus_drugs=focus_drugs)
    if aggregation == "by_location":
        return aggregate_by_location(studies, country_filter=country_filter)
    aggregator = AGGREGATORS.get(aggregation)
    if aggregator:
        return aggregator(studies)
    return []


__all__ = [
    "AGGREGATORS",
    "ALLOWED_VIZ_TYPES",
    "VALID_AGGREGATIONS",
    "aggregate_studies",
    "resolve_viz_type",
    "get_field_labels",
    "get_field_types",
    "get_series_field",
    "extract_field",
    "extract_date_parts",
    "get_nct_id",
    "normalize_condition_name",
    "normalize_country_name",
    "study_matches_condition_filter",
    "study_matches_country_filter",
]
