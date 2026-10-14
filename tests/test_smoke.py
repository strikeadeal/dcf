"""Random vs Random end to end: no API keys needed."""

import asyncio
import contextlib
import io
import json
from pathlib import Path

import chess
import chess.pgn
import pytest
import yaml
from fastapi.testclient import TestClient

from arena.game import Game, Match
from arena.players import RandomPlayer

CONFIG = {
    "game": {"max_plies": 200, "move_delay": 0},
    "players": {
        "random": {"type": "random", "name": "Random"},
        "needs-key": {"type": "openai", "model": "m", "api_key_env": "ARENA_TEST_UNSET_KEY"},
        "typo": {"type": "random", "sede": 1},
    },
}


def test_random_vs_random_full_game(tmp_path: Path) -> None:
    g = Game(RandomPlayer("R1", seed=7), RandomPlayer("R2", seed=8), move_delay=0)
    asyncio.run(g.run())
    assert g.result in {"1-0", "0-1", "1/2-1/2"} and g.reason
    stem = g.save(tmp_path)
    pgn = chess.pgn.read_game(io.StringIO(stem.with_suffix(".pgn").read_text()))
    assert pgn is not None
    assert list(pgn.mainline_moves()) == g.board.move_stack
    assert (pgn.headers["White"], pgn.headers["Black"], pgn.headers["Result"]) == ("R1", "R2", g.result)
    log = json.loads(stem.with_suffix(".json").read_text())
    assert len(log["turns"]) == len(g.board.move_stack) and log["reason"] == g.reason


def test_http_series_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    games = tmp_path / "games"
    monkeypatch.setenv("ARENA_CONFIG", str(cfg))
    monkeypatch.setenv("ARENA_GAMES_DIR", str(games))
    monkeypatch.delenv("ARENA_TEST_UNSET_KEY", raising=False)
    from arena import server

    monkeypatch.setattr(server, "match", None)
    with TestClient(server.app) as client:
        assert client.get("/api/events").text == 'data: {"status": "idle"}\n\n'
        players = {p["id"]: p for p in client.get("/api/players").json()}
        assert players["random"]["unavailable"] is None
        assert players["needs-key"]["unavailable"] == "ARENA_TEST_UNSET_KEY not set"
        assert client.post("/api/start", json={"white": "needs-key", "black": "random"}).status_code == 400
        assert client.post("/api/start", json={"white": "nobody", "black": "random"}).status_code == 404
        r = client.post("/api/start", json={"white": "typo", "black": "random"})
        assert r.status_code == 400 and "bad config" in r.json()["detail"] and "sede" in r.json()["detail"]

        r = client.post("/api/start", json={"white": "random", "black": "random", "games": 2, "move_delay": 0})
        assert r.status_code == 200
        snaps = []
        with client.stream("GET", "/api/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            snaps = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
        last = snaps[-1]
        assert last["status"] == "finished" and last["result"] and last["reason"]
        series = last["series"]
        assert series["game"] == 2 and (series["a"], series["b"]) == ("Random", "Random")
        assert series["a_wins"] + series["draws"] + series["b_wins"] == 2

        pgn = client.get("/api/game.pgn")
        assert pgn.status_code == 200 and "attachment" in pgn.headers["content-disposition"]
        assert '[White "Random"]' in pgn.text
        assert client.get("/api/game.json").json()["result"] == last["result"]
        assert client.post("/api/control/pause").status_code == 409  # nothing running

    assert len(list(games.glob("*.pgn"))) == 2 and len(list(games.glob("*.json"))) == 2


def test_pause_resume_abort(tmp_path: Path) -> None:
    async def scenario() -> None:
        m = Match(CONFIG, "random", "random", 3, tmp_path, move_delay=0.01)
        await asyncio.sleep(0.1)
        assert m.game is not None
        m.pause()
        await asyncio.sleep(0.02)
        plies = len(m.game.board.move_stack)
        await asyncio.sleep(0.1)
        assert m.status == "paused" and len(m.game.board.move_stack) == plies
        m.resume()
        await asyncio.sleep(0.1)
        assert m.status == "running" and len(m.game.board.move_stack) > plies
        m.abort()
        with contextlib.suppress(asyncio.CancelledError):
            await m.task
        assert m.status == "aborted" and m.game.result == "*" and m.snapshot()["reason"] == "aborted"

    asyncio.run(scenario())
    saved = list(tmp_path.glob("*.pgn"))
    assert len(saved) == 1 and '[Result "*"]' in saved[0].read_text()
