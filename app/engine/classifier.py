"""Second-stage LLM: chart title and notes only.

Counts, viz type, and encoding are already decided in code. If the model
mentions entities absent from Filters, ``resolve_title_and_notes`` falls back
to deterministic templates.
"""

import json

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.config import OPENAI_API_KEY, OPENAI_MODEL
from app.engine.interpreter import LLMServiceError
from app.engine.labels import resolve_title_and_notes

VIZ_CLASSIFIER_PROMPT = """You are a visualization designer for clinical trials data. Given a user's question, the query interpretation, and aggregated data, produce the final visualization specification.

Return JSON with:
- title: human-readable title for the chart
- meta: { sorting (asc/desc or null), notes (assumptions or interpretation details) }

The aggregation type, viz type, and encoding have already been determined in code.
You only refine title and notes.

Do NOT invent numeric values. Counts already exist in the aggregated data.
Title and notes MUST reflect Filters and the actual aggregated data only.
Do NOT mention drugs, conditions, sponsors, countries, or years that are absent from Filters.
If Filters include start_year/end_year, mention that range. If they do not, do not claim a year cutoff.
When the data includes year labels, the title's year span must match the min/max years present in the data.
Output ONLY valid JSON."""


async def classify_visualization(
    query: str,
    interpretation: dict,
    aggregated_data: list[dict],
    viz_type: str,
    x_field: str,
    y_field: str,
    x_type: str,
    y_type: str,
    total_count: int,
    filters_applied: dict,
) -> dict:
    """Ask the LLM for title/notes only; fall back to grounded templates on failure."""
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # Strip internal citation ids before sending to the LLM.
    safe_rows = []
    for row in aggregated_data[:20]:
        safe_rows.append({k: v for k, v in row.items() if not str(k).startswith("_")})

    aggregation = interpretation.get("aggregation", "by_status")
    user_message = (
        f"User query: {query}\n"
        f"Aggregation: {aggregation}\n"
        f"Intent: {interpretation.get('intent', '')}\n"
        f"Viz type: {viz_type}\n"
        f"X: {x_field} ({x_type}), Y: {y_field} ({y_type})\n"
        f"Total records: {total_count}\n"
        f"Filters: {json.dumps(filters_applied)}\n"
        f"Aggregated data (first 20 rows): {json.dumps(safe_rows)}\n\n"
        "Generate title and notes only."
    )

    llm_title = None
    llm_notes = None
    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": VIZ_CLASSIFIER_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        if isinstance(result, dict):
            llm_title = result.get("title")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            llm_notes = meta.get("notes") or result.get("notes")
    except RateLimitError as exc:
        raise LLMServiceError("OpenAI rate limit exceeded. Try again shortly.", 429) from exc
    except APITimeoutError as exc:
        raise LLMServiceError("OpenAI request timed out.", 504) from exc
    except (APIError, json.JSONDecodeError):
        # Fall back to deterministic templates rather than failing the whole query.
        llm_title = None
        llm_notes = None

    title, notes = resolve_title_and_notes(
        llm_title=str(llm_title) if llm_title else None,
        llm_notes=str(llm_notes) if llm_notes else None,
        aggregation=aggregation,
        filters=filters_applied or {},
        aggregated_data=aggregated_data,
        total_count=total_count,
    )
    return {
        "title": title,
        "encoding": {},  # Encoding is always built deterministically in the router.
        "meta": {"sorting": None, "notes": notes},
        "notes": notes,
    }
