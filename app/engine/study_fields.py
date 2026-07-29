"""Study field extractors and entity normalizers for CT.gov protocol JSON.

Shared by aggregators, filters, fetch, and the query interpreter. Deterministic
only — no LLM involvement.
"""

from __future__ import annotations

import re
from typing import Optional


def extract_field(study: dict, *path: str) -> Optional[str]:
    """Walk nested protocol JSON path; return a scalar string or None."""
    section = study.get("protocolSection", {})
    for key in path:
        if isinstance(section, dict):
            section = section.get(key, {})
        else:
            return None
    if isinstance(section, list):
        return section[0] if section else None
    if isinstance(section, str):
        return section
    if isinstance(section, (int, float)):
        return section
    return None


def extract_field_list(study: dict, *path) -> list:
    """Walk nested path; return a list (empty if missing)."""
    section = study.get("protocolSection", {})
    for key in path:
        if isinstance(section, dict):
            section = section.get(key, {})
        else:
            return []
    if isinstance(section, list):
        return section
    return []


def extract_date_year(study: dict, *path) -> Optional[int]:
    """Extract calendar year from a date field path."""
    year, _ = extract_date_parts(study, *path)
    return year


def extract_date_parts(study: dict, *path) -> tuple[Optional[int], Optional[int]]:
    """Return (year, month) from a CT.gov date string (YYYY, YYYY-MM, or YYYY-MM-DD)."""
    date_str = extract_field(study, *path)
    if not date_str or not isinstance(date_str, str) or len(date_str) < 4:
        return (None, None)
    parts = date_str.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        if month < 1 or month > 12:
            month = 1
        return (year, month)
    except ValueError:
        return (None, None)


def get_nct_id(study: dict) -> str:
    """NCT identifier for a study."""
    return extract_field(study, "identificationModule", "nctId") or ""


def get_brief_title(study: dict) -> str:
    """Brief title string."""
    return extract_field(study, "identificationModule", "briefTitle") or ""


def _format_phase(phase: Optional[str]) -> str:
    """Normalize raw phase enum to display label."""
    if not phase:
        return "NA"
    raw = str(phase).strip().upper().replace(" ", "_")
    aliases = {
        "EARLY_PHASE1": "Early Phase 1",
        "PHASE1": "Phase 1",
        "PHASE2": "Phase 2",
        "PHASE3": "Phase 3",
        "PHASE4": "Phase 4",
        "NA": "NA",
        "NOT_APPLICABLE": "NA",
    }
    if raw in aliases:
        return aliases[raw]
    if raw.startswith("PHASE"):
        return raw.replace("PHASE", "Phase ").replace("_", " ")
    return str(phase).replace("_", " ").title()


def _format_status(status: Optional[str]) -> str:
    """Normalize overall status for display."""
    return status.replace("_", " ").title() if status else "Unknown"


def get_phase(study: dict) -> str:
    """Primary display phase for a study."""
    phases = extract_field(study, "designModule", "phases")
    if isinstance(phases, list) and phases:
        return _format_phase(phases[0])
    if isinstance(phases, str):
        return _format_phase(phases)
    return "NA"


def get_enrollment(study: dict) -> Optional[int]:
    """Enrollment count if available."""
    value = extract_field(study, "designModule", "enrollmentInfo", "count")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_drugs(study: dict, limit: int = 3) -> list[str]:
    """Drug-like intervention names (filters out non-drugs)."""
    interventions = (
        study.get("protocolSection", {})
        .get("armsInterventionsModule", {})
        .get("interventions", [])
        or []
    )
    names = []
    seen = set()
    for inv in interventions:
        if _is_non_drug_type(inv):
            continue
        name = normalize_drug_name(inv.get("name") or "")
        if not name:
            continue
        key = name.casefold()
        if key in seen or key in NON_DRUG_INTERVENTIONS or _looks_like_non_drug_name(name):
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= limit:
            break
    return names


NON_DRUG_INTERVENTION_TYPES: frozenset[str] = frozenset({
    "procedure",
    "device",
    "radiation",
    "behavioral",
    "diagnostic_test",
})


def _is_non_drug_type(inv: dict) -> bool:
    """Return True if the intervention has a type that is clearly not a drug."""
    t = (inv.get("type") or "").strip().lower()
    return t in NON_DRUG_INTERVENTION_TYPES


NON_DRUG_INTERVENTIONS: frozenset[str] = frozenset({
    "biospecimen collection",
    "laboratory biomarker analysis",
    "computed tomography",
    "biopsy",
    "biopsy procedure",
    "positron emission tomography",
    "magnetic resonance imaging",
    "placebo",
    "no intervention",
    "dmards",
    "quality-of-life assessment",
    "quality of life assessment",
    "questionnaire administration",
    "questionnaire",
    "survey administration",
    "best practice",
    "pharmacological study",
    "laboratory procedure",
    "medical chart review",
    "electronic health record review",
    "follow-up",
    "follow-up care",
    "supportive care",
    "observation",
    "standard of care",
})


def _looks_like_non_drug_name(name: str) -> bool:
    """Heuristic reject for questionnaire / assessment style 'drugs'."""
    lower = (name or "").casefold()
    needles = (
        "questionnaire",
        "quality-of-life",
        "quality of life",
        "survey",
        "assessment",
        "interview",
        "diary",
        "education",
        "counseling",
        "counselling",
        "exercise",
        "physical therapy",
        "physiotherapy",
        "biospecimen",
        "imaging",
        "radiotherapy",
        "radiation therapy",
        "surgery",
        "resection",
        "ablation",
    )
    return any(n in lower for n in needles)


def normalize_drug_name(name: str) -> str:
    """Collapse whitespace and case so 'pembrolizumab' and 'Pembrolizumab' group together."""
    cleaned = " ".join(str(name or "").strip().split())
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", cleaned)
    cleaned = " ".join(cleaned.strip().split())
    if not cleaned:
        return ""
    return cleaned.title()


CONDITION_SYNONYM_MAP: dict[str, str] = {
    "carcinoma non small cell lung": "Non-small Cell Lung Cancer",
    "non small cell lung carcinoma": "Non-small Cell Lung Cancer",
    "non small cell lung cancer": "Non-small Cell Lung Cancer",
    "cll": "Chronic Lymphocytic Leukemia",
    "chronic lymphatic leukemia": "Chronic Lymphocytic Leukemia",
    "aml": "Acute Myeloid Leukemia",
    "acute myelogenous leukemia": "Acute Myeloid Leukemia",
    "all": "Acute Lymphoblastic Leukemia",
    "acute lymphoblastic leukemia": "Acute Lymphoblastic Leukemia",
    "cml": "Chronic Myeloid Leukemia",
    "chronic myelogenous leukemia": "Chronic Myeloid Leukemia",
    "nsclc": "Non-small Cell Lung Cancer",
    "sclc": "Small Cell Lung Cancer",
    "crc": "Colorectal Cancer",
    "hcc": "Hepatocellular Carcinoma",
    "rcc": "Renal Cell Carcinoma",
    "copd": "Chronic Obstructive Pulmonary Disease",
    "chf": "Congestive Heart Failure",
    "cad": "Coronary Artery Disease",
    "t2dm": "Type 2 Diabetes Mellitus",
    "oa": "Osteoarthritis",
    "ra": "Rheumatoid Arthritis",
    "ms": "Multiple Sclerosis",
    "als": "Amyotrophic Lateral Sclerosis",
    "hiv": "Human Immunodeficiency Virus",
    "aids": "Acquired Immunodeficiency Syndrome",
    "mds": "Myelodysplastic Syndrome",
    "mpn": "Myeloproliferative Neoplasm",
    "dlbcl": "Diffuse Large B-cell Lymphoma",
    "fl": "Follicular Lymphoma",
    "mcl": "Mantle Cell Lymphoma",
    "mzl": "Marginal Zone Lymphoma",
    "hl": "Hodgkin Lymphoma",
    "nhl": "Non-Hodgkin Lymphoma",
    "mm": "Multiple Myeloma",
    "gvhd": "Graft Versus Host Disease",
    "ibd": "Inflammatory Bowel Disease",
    "ckd": "Chronic Kidney Disease",
    "esrd": "End Stage Renal Disease",
    "nafld": "Non-alcoholic Fatty Liver Disease",
    "nash": "Non-alcoholic Steatohepatitis",
    "pcos": "Polycystic Ovary Syndrome",
    "pah": "Pulmonary Arterial Hypertension",
    "dvt": "Deep Vein Thrombosis",
    "pe": "Pulmonary Embolism",
    "tbi": "Traumatic Brain Injury",
    "sci": "Spinal Cord Injury",
}


def _condition_normalize_key(name: str) -> str:
    """Produce a stable dedup key for a condition name."""
    n = str(name or "").strip()
    n = re.sub(r"\s*\([^)]*\)\s*$", "", n)
    n = n.replace("-", " ").replace(",", " ")
    n = " ".join(n.lower().split())
    return n


def normalize_condition_name(name: str) -> str:
    """Return a canonical display name for a condition."""
    raw = " ".join(str(name or "").strip().split())
    if not raw:
        return ""
    key = _condition_normalize_key(raw)
    if key in CONDITION_SYNONYM_MAP:
        return CONDITION_SYNONYM_MAP[key]
    return raw


def get_conditions(study: dict, limit: int = 3) -> list[str]:
    """Condition names from the study (limited)."""
    seen_keys: set[str] = set()
    result: list[str] = []
    for raw in extract_field_list(study, "conditionsModule", "conditions"):
        c = normalize_condition_name(raw)
        if not c:
            continue
        key = _condition_normalize_key(c)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(c)
        if len(result) >= limit:
            break
    return result


def _condition_matches_target(condition: str, target: str) -> bool:
    """True when a study condition field matches the requested filter."""
    c_key = _condition_normalize_key(normalize_condition_name(condition))
    t_key = _condition_normalize_key(normalize_condition_name(target))
    if not c_key or not t_key:
        return False
    if c_key == t_key:
        return True
    if re.search(rf"\bwithout\s+{re.escape(t_key)}\b", c_key):
        return False
    if t_key in c_key:
        return True
    return False


def study_matches_condition_filter(study: dict, target: str) -> bool:
    """Keep studies whose listed conditions actually include the filter target."""
    target = (target or "").strip()
    if not target:
        return True
    conditions = get_conditions(study, limit=12)
    if not conditions:
        return False
    return any(_condition_matches_target(c, target) for c in conditions)


def get_sponsor(study: dict) -> Optional[str]:
    """Lead sponsor name or None."""
    return extract_field(study, "sponsorCollaboratorsModule", "leadSponsor", "name")


def get_countries(study: dict, limit: int = 3) -> list[str]:
    """Unique facility countries (limited)."""
    locations = (
        study.get("protocolSection", {})
        .get("contactsLocationsModule", {})
        .get("locations", [])
        or []
    )
    countries = []
    seen = set()
    for loc in locations:
        country = (loc.get("country") or "").strip()
        if country and country not in seen:
            seen.add(country)
            countries.append(country)
        if limit is not None and len(countries) >= limit:
            break
    return countries


# Common user/API aliases → canonical CT.gov country names.
COUNTRY_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "south korea": "South Korea",
    "prc": "China",
    "people's republic of china": "China",
    "mainland china": "China",
    "russia": "Russian Federation",
    "russian federation": "Russian Federation",
    "vietnam": "Viet Nam",
    "czech republic": "Czechia",
    "holland": "Netherlands",
    "uae": "United Arab Emirates",
}


def normalize_country_name(name: str) -> str:
    """Map common aliases to CT.gov country labels; otherwise title-case lightly."""
    raw = (name or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", " ")
    key = " ".join(key.split())
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    # Preserve known multi-word casing for common CT.gov values.
    return raw


def _country_match_keys(name: str) -> set[str]:
    """Comparable tokens for fuzzy-but-safe country matching."""
    canon = normalize_country_name(name)
    keys = {canon.lower(), (name or "").strip().lower()}
    # Also allow substring-safe exact token compare without punctuation.
    for k in list(keys):
        keys.add(re.sub(r"[^a-z0-9 ]", "", k))
    return {k for k in keys if k}


def countries_match(left: str, right: str) -> bool:
    """True when two country labels refer to the same place (incl. usa/United States)."""
    if not left or not right:
        return False
    a = _country_match_keys(left)
    b = _country_match_keys(right)
    if a & b:
        return True
    # Exact canonical equality after alias expansion.
    return normalize_country_name(left).lower() == normalize_country_name(right).lower()


def study_matches_country_filter(study: dict, country_filter: str) -> bool:
    """True if any study location country matches the filter (aliases allowed)."""
    needle = (country_filter or "").strip()
    if not needle:
        return True
    for country in get_countries(study, limit=None):
        if countries_match(country, needle):
            return True
    return False



def get_investigators(study: dict, limit: int = 2) -> list[str]:
    """Investigator names (limited)."""
    officials = (
        study.get("protocolSection", {})
        .get("contactsLocationsModule", {})
        .get("overallOfficials", [])
        or []
    )
    names = []
    for person in officials:
        name = (person.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names
