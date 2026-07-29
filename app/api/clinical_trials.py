"""Async ClinicalTrials.gov Data API v2 client.

Handles pagination (nextPageToken), retries on 429/5xx, timeouts, and
parameter hygiene (status/phase allow-lists, lead sponsor, phase).
"""

import asyncio
import re
from typing import Optional

import httpx

from app.config import (
    CLINICAL_TRIALS_BASE_URL,
    CT_MAX_RETRIES,
    CT_MAX_STUDIES,
    CT_PAGE_PAUSE_SECONDS,
    CT_PAGE_SIZE,
    CT_TIMEOUT_SECONDS,
)
from app.schemas.input import VALID_PHASES, VALID_STATUSES


# --- Error type the router maps to HTTP 429/502/504 ---

class ClinicalTrialsAPIError(Exception):
    """Raised when ClinicalTrials.gov returns an unexpected failure."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Honor Retry-After when present; otherwise exponential backoff."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(float(header), 0.5)
        except ValueError:
            pass
    # 429s need a longer cool-down than transient 5xx.
    base = 1.5 if response.status_code == 429 else 0.4
    return base * (2**attempt)


VALID_CT_STATUSES = VALID_STATUSES
VALID_CT_PHASES = VALID_PHASES


# --- Async client: connection reuse + resilient paging ---

class ClinicalTrialsClient:
    """Async ClinicalTrials.gov v2 client with connection reuse and resilient paging."""

    def __init__(
        self,
        base_url: str = CLINICAL_TRIALS_BASE_URL,
        page_size: int = CT_PAGE_SIZE,
        max_studies: int = CT_MAX_STUDIES,
        timeout: float = CT_TIMEOUT_SECONDS,
        max_retries: int = CT_MAX_RETRIES,
    ):
        self.base_url = base_url
        self.page_size = min(max(page_size, 1), 1000)
        self.max_studies = max(max_studies, 1)
        self.timeout = timeout
        self.max_retries = max(max_retries, 0)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "ClinicalTrialsClient":
        await self._get_client()
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    # Translate our search dict into CT.gov v2 query/filter query-string params.
    def _build_params(
        self,
        *,
        term: Optional[str] = None,
        cond: Optional[str] = None,
        intr: Optional[str] = None,
        locn: Optional[str] = None,
        lead: Optional[str] = None,
        spons: Optional[str] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        advanced_filter: Optional[str] = None,
        sort: Optional[str] = None,
        fields: Optional[str] = None,
        page_size: int = 100,
        page_token: Optional[str] = None,
        count_total: bool = True,
    ) -> dict:
        params: dict = {}
        if term:
            params["query.term"] = term
        if cond:
            params["query.cond"] = cond
        if intr:
            params["query.intr"] = intr
        if locn:
            params["query.locn"] = locn
        if lead:
            params["query.lead"] = lead
        if spons:
            params["query.spons"] = spons
        # Status allow-list (reject ALL / garbage enums).
        if status:
            normalized = status.upper().replace(" ", "_")
            # CT.gov rejects invalid enums like ALL; only forward known statuses.
            parts = [p.strip() for p in normalized.split(",") if p.strip()]
            kept = [p for p in parts if p in VALID_CT_STATUSES]
            if kept:
                params["filter.overallStatus"] = ",".join(kept)

        # Phase via filter.advanced AREA[Phase]… (filter.phase is rejected by live API).
        # Phase: live CT.gov v2 no longer accepts filter.phase (returns 400
        # "unknown parameter"). Use Essie AREA[Phase] inside filter.advanced.
        phase_expr: Optional[str] = None
        if phase:
            parts = [p.strip().upper().replace(" ", "_") for p in phase.split(",") if p.strip()]
            kept_phases: list[str] = []
            for raw in parts:
                compact = raw.replace("_", "")
                if compact == "EARLYPHASE1" or raw in {"EARLY_PHASE1", "EARLY_PHASE_1"}:
                    kept_phases.append("EARLY_PHASE1")
                elif compact in {"PHASE1", "PHASE2", "PHASE3", "PHASE4"}:
                    kept_phases.append(compact)
                elif compact == "NA" or raw in {"NA", "NOT_APPLICABLE"}:
                    kept_phases.append("NA")
            seen: set[str] = set()
            ordered: list[str] = []
            for p in kept_phases:
                if p in VALID_CT_PHASES and p not in seen:
                    seen.add(p)
                    ordered.append(p)
            if ordered:
                clauses = [f"AREA[Phase]{p}" for p in ordered]
                phase_expr = clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"

        advanced_parts = [p for p in (phase_expr, advanced_filter) if p]
        if advanced_parts:
            params["filter.advanced"] = (
                advanced_parts[0] if len(advanced_parts) == 1 else " AND ".join(advanced_parts)
            )
        if sort:
            params["sort"] = sort
        if fields:
            params["fields"] = fields
        params["pageSize"] = min(page_size, 1000)
        if page_token:
            params["pageToken"] = page_token
        if count_total:
            params["countTotal"] = "true"
        return params

    # GET with retries on 429/5xx and timeouts.
    async def _get_json(self, path: str, params: dict) -> dict:
        last_error: Exception | None = None
        # Extra attempts for rate limits beyond the configured retry budget.
        attempts = self.max_retries + 1 + 2
        client = await self._get_client()
        for attempt in range(attempts):
            try:
                response = await client.get(path, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    err = ClinicalTrialsAPIError(
                        f"ClinicalTrials.gov request failed ({response.status_code}): {response.text[:200]}",
                        status_code=response.status_code,
                    )
                    if attempt < attempts - 1:
                        await asyncio.sleep(_retry_after_seconds(response, attempt))
                        last_error = err
                        continue
                    raise err
                if response.status_code >= 400:
                    # Strip HTML from 4xx bodies for cleaner client errors.
                    body = response.text[:300]
                    body = re.sub(r"<[^>]+>", " ", body)
                    body = " ".join(body.split())
                    raise ClinicalTrialsAPIError(
                        f"ClinicalTrials.gov request failed ({response.status_code}): {body}",
                        status_code=response.status_code,
                    )
                return response.json()
            except httpx.TimeoutException as exc:
                last_error = ClinicalTrialsAPIError(
                    "ClinicalTrials.gov request timed out",
                    status_code=504,
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = ClinicalTrialsAPIError(
                    f"ClinicalTrials.gov network error: {exc}",
                    status_code=502,
                )
                last_error.__cause__ = exc
            except ClinicalTrialsAPIError:
                raise
            if attempt < attempts - 1:
                await asyncio.sleep(0.4 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def search_studies(
        self,
        term: Optional[str] = None,
        cond: Optional[str] = None,
        intr: Optional[str] = None,
        locn: Optional[str] = None,
        lead: Optional[str] = None,
        spons: Optional[str] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        advanced_filter: Optional[str] = None,
        sort: Optional[str] = None,
        fields: Optional[str] = None,
        page_size: int = 100,
        page_token: Optional[str] = None,
        count_total: bool = True,
    ) -> dict:
        """Single page of /studies (caller may pass pageToken for paging)."""
        params = self._build_params(
            term=term,
            cond=cond,
            intr=intr,
            locn=locn,
            lead=lead,
            spons=spons,
            status=status,
            phase=phase,
            advanced_filter=advanced_filter,
            sort=sort,
            fields=fields,
            page_size=page_size,
            page_token=page_token,
            count_total=count_total,
        )
        return await self._get_json("/studies", params)

    async def search_studies_paginated(
        self,
        term: Optional[str] = None,
        cond: Optional[str] = None,
        intr: Optional[str] = None,
        locn: Optional[str] = None,
        lead: Optional[str] = None,
        spons: Optional[str] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        advanced_filter: Optional[str] = None,
        sort: Optional[str] = None,
        fields: Optional[str] = None,
        max_studies: Optional[int] = None,
    ) -> dict:
        """
        Walk nextPageToken until exhausted or max_studies is reached.

        Returns:
          {
            studies: [...],
            totalCount: int | None,   # API-reported universe size
            fetchedCount: int,        # studies actually returned
            truncated: bool,
          }
        """
        limit = max_studies if max_studies is not None else self.max_studies
        limit = max(1, min(int(limit), 5000))
        studies: list[dict] = []
        total_count: Optional[int] = None
        page_token: Optional[str] = None

        # Page until we hit the fetch cap or CT.gov runs out of tokens.
        while len(studies) < limit:
            page_size = min(self.page_size, limit - len(studies))
            payload = await self.search_studies(
                term=term,
                cond=cond,
                intr=intr,
                locn=locn,
                lead=lead,
                spons=spons,
                status=status,
                phase=phase,
                advanced_filter=advanced_filter,
                sort=sort,
                fields=fields,
                page_size=page_size,
                page_token=page_token,
                count_total=(page_token is None),
            )
            batch = payload.get("studies") or []
            if page_token is None:
                total_count = payload.get("totalCount")
            studies.extend(batch)

            page_token = payload.get("nextPageToken")
            if not page_token or not batch:
                break
            # Gentle pacing between pages to reduce rate-limit hits.
            await asyncio.sleep(CT_PAGE_PAUSE_SECONDS)

        # truncated=True when more matching studies exist than we fetched.
        truncated = bool(
            page_token
            or (total_count is not None and len(studies) < total_count)
        )
        if len(studies) > limit:
            studies = studies[:limit]
            truncated = True

        return {
            "studies": studies,
            "totalCount": total_count if total_count is not None else len(studies),
            "fetchedCount": len(studies),
            "truncated": truncated and (total_count or 0) > len(studies),
        }

    async def get_study(self, nct_id: str, fields: Optional[str] = None) -> dict:
        """Fetch one study by NCT id (optional field projection)."""
        params = {}
        if fields:
            params["fields"] = fields
        return await self._get_json(f"/studies/{nct_id}", params)

    async def get_stats(self) -> dict:
        """CT.gov corpus size stats (diagnostics)."""
        return await self._get_json("/stats/size", {})
