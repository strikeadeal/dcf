"""FastAPI app: player list, match control, SSE stream, exports, and the built frontend."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .game import Game, Match
from .players import build_player, unavailable_reason

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="AI Chess Arena")
match: Match | None = None  # ponytail: one match per process; enough for a local viewer


def config() -> dict[str, Any]:
    # Re-read every time so config.yaml edits apply without a restart.
    return yaml.safe_load(Path(os.environ.get("ARENA_CONFIG", ROOT / "config.yaml")).read_text())


def games_dir() -> Path:
    return Path(os.environ.get("ARENA_GAMES_DIR", ROOT / "games"))


@app.get("/api/players")
def players() -> list[dict[str, Any]]:
    return [
        {"id": pid, "name": p.get("name", pid), "type": p.get("type"), "unavailable": unavailable_reason(p)}
        for pid, p in config()["players"].items()
    ]


class StartRequest(BaseModel):
    white: str
    black: str
    games: int = Field(1, ge=1, le=100)
    move_delay: float | None = Field(None, ge=0, le=30)


@app.post("/api/start")
async def start(req: StartRequest) -> dict[str, Any]:
    global match
    if match and not match.done:
        raise HTTPException(409, "A match is already running. Abort it first.")
    cfg = config()
    for pid in (req.white, req.black):
        if pid not in cfg["players"]:
            raise HTTPException(404, f"Unknown player {pid!r}")
        if reason := unavailable_reason(cfg["players"][pid]):
            raise HTTPException(400, f"{pid}: {reason}")
        try:  # surface config typos now rather than inside the match task
            build_player(pid, cfg["players"][pid], cfg.get("llm_defaults"))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"{pid}: bad config: {e}") from e
    match = Match(cfg, req.white, req.black, req.games, games_dir(), req.move_delay)
    return {"ok": True}


@app.post("/api/control/{action}")
def control(action: Literal["pause", "resume", "abort"]) -> dict[str, Any]:
    if not match or match.done:
        raise HTTPException(409, "No match is running")
    getattr(match, action)()
    return {"ok": True}


def sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        m = match
        if m is None:
            yield sse({"status": "idle"})
            return
        while True:
            changed = m.changed  # grab before snapshotting so no update is missed
            yield sse(m.snapshot())
            if m.done:
                return
            await changed.wait()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def current_game() -> Game:
    if not match or not match.game:
        raise HTTPException(404, "No game yet")
    return match.game


def attachment(name: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{name}"'}


@app.get("/api/game.pgn")
def game_pgn() -> PlainTextResponse:
    return PlainTextResponse(current_game().pgn(), headers=attachment("game.pgn"))


@app.get("/api/game.json")
def game_json() -> JSONResponse:
    return JSONResponse(current_game().log(), headers=attachment("game.json"))


DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
else:

    @app.get("/")
    def no_frontend() -> PlainTextResponse:
        return PlainTextResponse("Frontend not built. Run: cd frontend && npm install && npm run build", 503)
