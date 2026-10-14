import asyncio
from typing import Any

import chess
import pytest

from arena.game import Game
from arena.players import LLMPlayer, RandomPlayer


class Scripted(LLMPlayer):
    """Replies from a script: str = model text, float = hang that long, Exception = API error."""

    def __init__(self, replies: list[Any], **kw: Any) -> None:
        super().__init__("Scripted", "fake-model", **kw)
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    async def _ask(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, int, dict[str, Any]]:
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, float):
            await asyncio.sleep(reply)
            reply = "MOVE: e4"
        return reply, 10, {"role": "assistant", "content": reply}


def play(player: LLMPlayer) -> Any:
    return asyncio.run(player.choose_move(chess.Board()))


def test_legal_after_one_illegal() -> None:
    p = Scripted(["MOVE: Ke2", "MOVE: e4"])
    r = play(p)
    assert (r.uci, r.retries, r.illegal, r.tokens, r.fallback) == ("e2e4", 1, 1, 20, None)
    reprompt = p.calls[1][-1]["content"]
    assert "'Ke2' is not a legal move" in reprompt and "Nf3" in reprompt  # error + legal list
    assert p.calls[1][-2] == {"role": "assistant", "content": "MOVE: Ke2"}


def test_forfeit_after_retries_exhausted() -> None:
    p = Scripted(["junk"] * 4, max_retries=3)
    r = play(p)
    assert (r.uci, r.retries, r.illegal, r.fallback) == (None, 3, 4, "forfeit")
    assert len(p.calls) == 4 and not p.replies


def test_random_fallback_policy() -> None:
    r = play(Scripted(["junk"] * 2, max_retries=1, on_failure="random_legal_move"))
    assert r.fallback == "random" and r.illegal == 2
    assert chess.Move.from_uci(r.uci) in chess.Board().legal_moves


def test_timeout_counts_as_failed_attempt() -> None:
    p = Scripted([1.0, "MOVE: e4"], timeout=0.05)
    r = play(p)
    assert (r.uci, r.illegal) == ("e2e4", 1)
    assert "timed out" in r.raw
    assert len(p.calls[1]) == 1  # nothing appended after a timeout


def test_api_error_counts_as_failed_attempt() -> None:
    r = play(Scripted([RuntimeError("boom"), "MOVE: d4"]))
    assert (r.uci, r.illegal) == ("d2d4", 1) and "boom" in r.raw


def test_bad_policy_rejected() -> None:
    with pytest.raises(ValueError):
        Scripted([], on_failure="resign")


def test_game_forfeit_ends_game_for_opponent() -> None:
    g = Game(Scripted(["junk"] * 4), RandomPlayer(seed=0), move_delay=0)
    asyncio.run(g.run())
    assert g.result == "0-1" and g.reason.startswith("forfeit: White (Scripted)")
    assert g.turns[-1]["fallback"] == "forfeit" and g.stats()["white"]["illegal"] == 4
    assert "{ forfeit: White" in g.pgn()


class Crashing(RandomPlayer):
    async def choose_move(self, board: chess.Board) -> Any:
        raise RuntimeError("engine died")


def test_crashing_player_forfeits_instead_of_hanging() -> None:
    g = Game(RandomPlayer(seed=1), Crashing("Crashy"), move_delay=0)
    asyncio.run(g.run())
    assert g.result == "1-0" and g.reason == "forfeit: Black (Crashy) made no legal move ([error: RuntimeError: engine died])"


def test_game_records_random_fallback_in_pgn() -> None:
    g = Game(Scripted(["junk"] * 4, on_failure="random_legal_move"), RandomPlayer(), max_plies=1, move_delay=0)
    asyncio.run(g.run())
    assert g.turns[0]["fallback"] == "random" and g.turns[0]["san"]
    assert "random fallback move after 4 failed attempts" in g.pgn()
