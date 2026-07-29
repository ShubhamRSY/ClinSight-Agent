from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.routers.query import router as query_router

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

app = FastAPI(
    title="ClinSight Agent",
    description="Backend service that converts clinical-trial questions into structured visualization outputs using ClinicalTrials.gov API data.",
    version="1.0.0",
)

app.include_router(query_router)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def demo():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ClinSight Agent"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
