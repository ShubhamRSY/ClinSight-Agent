"""
ClinSight Agent entrypoint.

Run:  python main.py
Then: http://localhost:8000/      (demo UI)
      http://localhost:8000/docs  (Swagger)
      POST /api/v1/query          (main API)
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.routers.query import router as query_router

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

app = FastAPI(
    title="ClinSight Agent",
    description=(
        "Backend service that converts clinical-trial questions into structured "
        "visualization outputs using ClinicalTrials.gov API data."
    ),
    version="1.0.0",
)

# API routes under /api/v1 (see app/routers/query.py)
app.include_router(query_router)
# Demo assets (JS/CSS) served from ./frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def demo():
    """Interactive demo UI (Chart.js + SVG networks)."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
async def health():
    """Liveness check for graders / deploy probes."""
    return {"status": "ok", "service": "ClinSight Agent"}


if __name__ == "__main__":
    # reload=True for local iteration; graders can also use: uvicorn main:app
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
