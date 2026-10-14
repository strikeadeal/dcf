"""Provider adapters against faked SDK clients (no network, no keys)."""

import asyncio
import shutil
from types import SimpleNamespace
from typing import Any

import chess
import pytest

from arena.players import (
    AnthropicPlayer,
    OpenAIPlayer,
    StockfishPlayer,
    build_player,
    unavailable_reason,
)


def fake(responses: list[Any], seen: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    async def create(**kw: Any) -> Any:
        seen.append({**kw, "messages": list(kw["messages"])})  # snapshot: the list grows later
        return responses.pop(0)

    return create


def claude_reply(text: str, stop: str = "end_turn") -> Any:
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=50, output_tokens=7))


def test_anthropic_player(monkeypatch: pytest.MonkeyPatch) -> None:
    p = AnthropicPlayer("Claude", "claude-test", max_tokens=100, effort="low")
    seen: list[dict[str, Any]] = []
    replies = [claude_reply("", stop="refusal"), claude_reply("MOVE: Ke2"), claude_reply("Centre.\nMOVE: e4")]
    monkeypatch.setattr(p.client.messages, "create", fake(replies, seen))
    r = asyncio.run(p.choose_move(chess.Board()))
    assert (r.uci, r.illegal, r.tokens) == ("e2e4", 2, 114)
    first = seen[0]
    assert first["model"] == "claude-test" and first["max_tokens"] == 100
    assert first["output_config"] == {"effort": "low"} and "extra_body" not in first  # no temperature
    assert "MOVE: <move in SAN>" in first["system"]
    assert len(seen[1]["messages"]) == 1  # refusal: nothing appended
    assistant = seen[2]["messages"][1]
    assert assistant["role"] == "assistant" and assistant["content"][0].type == "thinking"  # echoed unchanged


def openai_reply(text: str) -> Any:
    msg = SimpleNamespace(content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(total_tokens=42))


def test_openai_player_official(monkeypatch: pytest.MonkeyPatch) -> None:
    p = OpenAIPlayer("GPT", "gpt-test", max_tokens=100)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(p.client.chat.completions, "create", fake([openai_reply("MOVE: d4")], seen))
    r = asyncio.run(p.choose_move(chess.Board()))
    assert (r.uci, r.tokens) == ("d2d4", 42)
    kw = seen[0]
    assert kw["max_completion_tokens"] == 100 and "max_tokens" not in kw and "temperature" not in kw
    assert kw["messages"][0]["role"] == "system" and kw["messages"][1]["role"] == "user"


def test_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    p = OpenAIPlayer("Llama", "llama3.1", base_url="http://localhost:11434/v1", max_tokens=100, temperature=0.2)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(p.client.chat.completions, "create", fake([openai_reply("MOVE: c4")], seen))
    assert asyncio.run(p.choose_move(chess.Board())).uci == "c2c4"
    assert seen[0]["max_tokens"] == 100 and seen[0]["temperature"] == 0.2


def test_build_player_merges_defaults_and_rejects_typos() -> None:
    p = build_player("x", {"type": "openai", "model": "m", "base_url": "http://h/v1"}, {"max_retries": 1})
    assert isinstance(p, OpenAIPlayer) and p.max_retries == 1 and p.name == "x"
    with pytest.raises(TypeError):
        build_player("r", {"type": "random", "modle": "oops"})


def test_unavailable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_TEST_KEY", raising=False)
    assert unavailable_reason({"type": "openai", "api_key_env": "ARENA_TEST_KEY"}) == "ARENA_TEST_KEY not set"
    monkeypatch.setenv("ARENA_TEST_KEY", "k")
    assert unavailable_reason({"type": "openai", "api_key_env": "ARENA_TEST_KEY"}) is None
    assert unavailable_reason({"type": "stockfish", "path": "/nonexistent/stockfish"}) == "Stockfish binary not found"
    assert unavailable_reason({"type": "nope"}) == "unknown type 'nope'"


@pytest.mark.skipif(not shutil.which("stockfish"), reason="stockfish binary not installed")
def test_stockfish_plays_legal_move() -> None:
    async def go() -> str | None:
        p = StockfishPlayer(skill_level=1, think_time=0.05)
        try:
            return (await p.choose_move(chess.Board())).uci
        finally:
            await p.close()

    assert chess.Move.from_uci(go_result := asyncio.run(go()) or "") in chess.Board().legal_moves, go_result
