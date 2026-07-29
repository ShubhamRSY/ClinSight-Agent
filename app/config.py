"""Runtime configuration from environment / .env (never commit secrets)."""

import os
from datetime import date

from dotenv import load_dotenv

# Loads .env from the working directory when present.
load_dotenv()

# --- OpenAI (interpret + title/notes) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# --- ClinicalTrials.gov client defaults ---
CLINICAL_TRIALS_BASE_URL = "https://clinicaltrials.gov/api/v2"

# Real-world API handling: paginate up to this many studies per query.
CT_PAGE_SIZE = int(os.getenv("CT_PAGE_SIZE", "200"))
CT_MAX_STUDIES = int(os.getenv("CT_MAX_STUDIES", "1000"))
CT_TIMEOUT_SECONDS = float(os.getenv("CT_TIMEOUT_SECONDS", "45"))
CT_MAX_RETRIES = int(os.getenv("CT_MAX_RETRIES", "3"))

# Identical QueryRequest responses are cached in-process for this many seconds (0 disables).
# --- In-process response cache ---
QUERY_CACHE_TTL_SECONDS = float(os.getenv("QUERY_CACHE_TTL_SECONDS", "300"))
QUERY_CACHE_MAX_ENTRIES = int(os.getenv("QUERY_CACHE_MAX_ENTRIES", "128"))

# Inter-page pause inside CT.gov pagination (seconds).
CT_PAGE_PAUSE_SECONDS = float(os.getenv("CT_PAGE_PAUSE_SECONDS", "0.2"))


# --- Reference "today" for relative NL like "last 6 months" ---

def get_reference_date() -> date:
    """Calendar 'today' for relative phrases like 'last 6 months'.

    Override with CLINSIGHT_REFERENCE_DATE (YYYY-MM-DD) for reproducible tests
    and demos. A legacy CLINSight_REFERENCE_DATE alias is also accepted.
    """
    raw = (
        os.getenv("CLINSIGHT_REFERENCE_DATE")
        or os.getenv("CLINSight_REFERENCE_DATE")
        or ""
    ).strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date.today()
