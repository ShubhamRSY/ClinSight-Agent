"""Request contract for POST /api/v1/query.

Optional structured filters are normalized (status/phase aliases → CT.gov enums)
and validated before any LLM or ClinicalTrials.gov call.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VALID_STATUSES = frozenset(
    {
        "RECRUITING",
        "NOT_YET_RECRUITING",
        "ACTIVE_NOT_RECRUITING",
        "COMPLETED",
        "TERMINATED",
        "WITHDRAWN",
        "SUSPENDED",
        "ENROLLING_BY_INVITATION",
        "AVAILABLE",
        "NO_LONGER_AVAILABLE",
        "TEMPORARILY_NOT_AVAILABLE",
        "APPROVED_FOR_MARKETING",
        "WITHHELD",
        "UNKNOWN",
    }
)

# Human-friendly aliases → CT.gov enum
STATUS_ALIASES = {
    "recruiting": "RECRUITING",
    "not yet recruiting": "NOT_YET_RECRUITING",
    "not_yet_recruiting": "NOT_YET_RECRUITING",
    "active not recruiting": "ACTIVE_NOT_RECRUITING",
    "active, not recruiting": "ACTIVE_NOT_RECRUITING",
    "active_not_recruiting": "ACTIVE_NOT_RECRUITING",
    "completed": "COMPLETED",
    "terminated": "TERMINATED",
    "withdrawn": "WITHDRAWN",
    "suspended": "SUSPENDED",
    "enrolling by invitation": "ENROLLING_BY_INVITATION",
    "enrolling_by_invitation": "ENROLLING_BY_INVITATION",
    "unknown": "UNKNOWN",
}

VALID_PHASES = frozenset(
    {
        "PHASE1",
        "PHASE2",
        "PHASE3",
        "PHASE4",
        "EARLY_PHASE1",
        "NA",
    }
)

PHASE_ALIASES = {
    "phase 1": "PHASE1",
    "phase1": "PHASE1",
    "phase i": "PHASE1",
    "phase 2": "PHASE2",
    "phase2": "PHASE2",
    "phase ii": "PHASE2",
    "phase 3": "PHASE3",
    "phase3": "PHASE3",
    "phase iii": "PHASE3",
    "phase 4": "PHASE4",
    "phase4": "PHASE4",
    "phase iv": "PHASE4",
    "early phase 1": "EARLY_PHASE1",
    "early_phase1": "EARLY_PHASE1",
    "early phase1": "EARLY_PHASE1",
    "na": "NA",
    "n/a": "NA",
    "not applicable": "NA",
}

_STATUS_HINT = (
    "Recruiting, Not Yet Recruiting, Active Not Recruiting, Completed, "
    "Terminated, Withdrawn, Suspended, Enrolling By Invitation, Unknown"
)
_PHASE_HINT = "Phase 1, Phase 2, Phase 3, Phase 4, Early Phase 1, or NA"


def normalize_status_value(value: str) -> str:
    """Normalize a single status token to a CT.gov enum, or raise ValueError."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("status cannot be empty")
    key = raw.lower().replace("_", " ").replace(",", " ")
    key = " ".join(key.split())
    if key in STATUS_ALIASES:
        return STATUS_ALIASES[key]
    enum = raw.strip().upper().replace(" ", "_")
    if enum in VALID_STATUSES:
        return enum
    raise ValueError(
        f"Invalid status '{raw}'. Use one of: {_STATUS_HINT}."
    )


def normalize_phase_value(value: str) -> str:
    """Normalize a phase string to a CT.gov enum, or raise ValueError."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("trial_phase cannot be empty")
    key = raw.lower().replace("_", " ")
    key = " ".join(key.split())
    if key in PHASE_ALIASES:
        return PHASE_ALIASES[key]
    enum = raw.strip().upper().replace(" ", "")
    # Allow PHASE_3 / PHASE3 style
    enum = enum.replace("_", "")
    mapped = {
        "PHASE1": "PHASE1",
        "PHASE2": "PHASE2",
        "PHASE3": "PHASE3",
        "PHASE4": "PHASE4",
        "EARLYPHASE1": "EARLY_PHASE1",
        "NA": "NA",
    }.get(enum)
    if mapped:
        return mapped
    raise ValueError(
        f"Invalid phase '{raw}'. Use one of: {_PHASE_HINT}."
    )


def _empty_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class QueryRequest(BaseModel):
    """POST /api/v1/query body: NL question plus optional structured filters."""

    query: str = Field(
        ...,
        description="Natural language question about clinical trials",
        min_length=1,
        examples=["How has the number of trials for this drug changed over time?"],
    )
    drug_name: Optional[str] = Field(
        None,
        description="Drug/intervention name (maps to CT.gov query.intr)",
        examples=["Pembrolizumab"],
    )
    condition: Optional[str] = Field(
        None,
        description="Disease or condition (maps to CT.gov query.cond)",
        examples=["Non-small Cell Lung Cancer"],
    )
    trial_phase: Optional[str] = Field(
        None,
        description=(
            "Trial phase. Accepts aliases (Phase 3, phase iii) and normalizes to "
            f"{_PHASE_HINT}."
        ),
        examples=["Phase 3"],
    )
    sponsor: Optional[str] = Field(
        None,
        description="Lead sponsor organization name (CT.gov query.lead + local filter)",
        examples=["Pfizer"],
    )
    country: Optional[str] = Field(
        None,
        description="Country location (aliases like usa → United States)",
        examples=["United States"],
    )
    start_year: Optional[int] = Field(
        None,
        ge=1900,
        le=2100,
        description="Inclusive start-year filter on study start date",
        examples=[2015],
    )
    end_year: Optional[int] = Field(
        None,
        ge=1900,
        le=2100,
        description="Inclusive end-year filter on study start date",
        examples=[2024],
    )
    status: Optional[str] = Field(
        None,
        description=(
            "Trial status. Accepts aliases (Recruiting) and normalizes to CT.gov enums. "
            f"Allowed: {_STATUS_HINT}. Comma-separated lists permitted."
        ),
        examples=["RECRUITING"],
    )
    max_studies: Optional[int] = Field(
        None,
        ge=100,
        le=5000,
        description=(
            "Max ClinicalTrials.gov records to fetch "
            "(default from CT_MAX_STUDIES, typically 1000). "
            "Higher values are slower but reduce truncation."
        ),
        examples=[1000],
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "How has the number of trials for this drug changed over time?",
                    "drug_name": "Pembrolizumab",
                },
                {
                    "query": "Which countries have the most recruiting trials for lung cancer?",
                    "condition": "Lung Cancer",
                    "status": "RECRUITING",
                },
            ]
        },
    )

    @field_validator("query")
    @classmethod
    def query_must_be_non_empty(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("query must be a non-empty string")
        return stripped

    @field_validator("drug_name", "condition", "sponsor", "country", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return _empty_to_none(value) if isinstance(value, str) or value is None else value

    @field_validator("drug_name", "condition", "sponsor", "country")
    @classmethod
    def reject_gibberish_text_filters(cls, value: Optional[str], info) -> Optional[str]:
        """Reject obviously invalid free-text optional filters (too short / nonsense tokens)."""
        if value is None:
            return None
        # Allow multi-word real entities; reject ultra-short junk and pure punctuation.
        if len(value) < 2:
            raise ValueError(f"{info.field_name} must be at least 2 characters")
        if not any(ch.isalnum() for ch in value):
            raise ValueError(f"{info.field_name} must contain letters or numbers")
        # Single token with no letters (e.g. "!!!") already covered; reject 1-char repeats.
        compact = "".join(ch for ch in value.lower() if ch.isalnum())
        if len(compact) >= 2 and len(set(compact)) == 1:
            raise ValueError(f"Invalid {info.field_name} '{value}'")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: Optional[str]) -> Optional[str]:
        value = _empty_to_none(value) if isinstance(value, str) or value is None else value
        if value is None:
            return None
        # Allow comma-separated lists; every part must be valid.
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts:
            return None
        normalized = [normalize_status_value(p) for p in parts]
        return ",".join(normalized)

    @field_validator("trial_phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: Optional[str]) -> Optional[str]:
        value = _empty_to_none(value) if isinstance(value, str) or value is None else value
        if value is None:
            return None
        return normalize_phase_value(value)

    @model_validator(mode="after")
    def check_year_range(self) -> "QueryRequest":
        if (
            self.start_year is not None
            and self.end_year is not None
            and self.start_year > self.end_year
        ):
            raise ValueError(
                f"start_year ({self.start_year}) cannot be after end_year ({self.end_year})"
            )
        return self
