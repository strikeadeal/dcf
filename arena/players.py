"""Players: the Player interface, LLM move protocol, and concrete players."""

import asyncio
import os
import random
import re
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import anthropic
import chess
import chess.engine
import openai


@dataclass
class MoveResult:
    uci: str | None  # None = no legal move produced (forfeit)
    raw: str = ""  # everything the player said, across attempts, with errors
    latency: float = 0.0  # seconds, all attempts
    tokens: int = 0  # input + output
    retries: int = 0  # attempts after the first
    illegal: int = 0  # failed attempts (illegal, unparseable, timeout, API error)
    fallback: str | None = None  # None | "forfeit" | "random"


class Player(ABC):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def choose_move(self, board: chess.Board) -> MoveResult:
        """`board` is a copy: FEN, move history and legal moves all live on it."""

    async def close(self) -> None:
        pass


class RandomPlayer(Player):
    def __init__(self, name: str = "Random", seed: int | None = None) -> None:
        super().__init__(name)
        self.rng = random.Random(seed)

    async def choose_move(self, board: chess.Board) -> MoveResult:
        move = self.rng.choice(list(board.legal_moves))
        return MoveResult(move.uci(), raw=f"random: {board.san(move)}")


class StockfishPlayer(Player):
    def __init__(
        self, name: str = "Stockfish", path: str = "stockfish", skill_level: int = 20, think_time: float = 0.1
    ) -> None:
        super().__init__(name)
        self.path, self.skill_level, self.think_time = path, skill_level, think_time
        self.engine: chess.engine.UciProtocol | None = None

    async def choose_move(self, board: chess.Board) -> MoveResult:
        if self.engine is None:
            _, self.engine = await chess.engine.popen_uci(shutil.which(self.path) or self.path)
            await self.engine.configure({"Skill Level": self.skill_level})
        start = time.perf_counter()
        result = await self.engine.play(board, chess.engine.Limit(time=self.think_time))
        uci = result.move.uci() if result.move else None
        return MoveResult(uci, raw=f"bestmove {uci}", latency=time.perf_counter() - start)

    async def close(self) -> None:
        if self.engine is not None:
            try:
                await self.engine.quit()
            except Exception:  # engine already gone (e.g. killed mid-abort)
                pass
            self.engine = None


# --- LLM protocol -----------------------------------------------------------

MOVE_RE = re.compile(r"\bMOVE:\s*[`*\"']*([^\s`*\"']+)", re.IGNORECASE)


def parse_move(text: str, board: chess.Board) -> chess.Move:
    """Take the last `MOVE: <x>` in `text`; accept SAN or UCI; raise ValueError if not legal."""
    found = MOVE_RE.findall(text)
    if not found:
        raise ValueError("no line of the form 'MOVE: <SAN>' found")
    token = found[-1].rstrip(".,;:!?")
    for parse in (board.parse_san, lambda t: chess.Move.from_uci(t.lower())):
        try:
            move = parse(token)
        except ValueError:
            continue
        if move in board.legal_moves:  # also rejects null moves ("--", "0000")
            return move
    raise ValueError(f"'{token}' is not a legal move in this position")


def legal_san(board: chess.Board) -> str:
    return ", ".join(board.san(m) for m in board.legal_moves)


def system_prompt(reasoning: bool) -> str:
    style = (
        "You may reason briefly before answering."
        if reasoning
        else "Reply with only that single line and nothing else."
    )
    return (
        "You are playing a game of chess. Each turn you get the side to move, the position as FEN, "
        "the move history and the list of legal moves. Choose one move from the legal list. "
        f"{style} The final line of your reply must be exactly `MOVE: <move in SAN>`, for example `MOVE: Nf3`."
    )


def turn_prompt(board: chess.Board) -> str:
    history = board.root().variation_san(board.move_stack) or "(none, game start)"
    return (
        f"Side to move: {'White' if board.turn else 'Black'}\n"
        f"FEN: {board.fen()}\n"
        f"Move history (SAN): {history}\n"
        f"Legal moves (SAN): {legal_san(board)}"
    )


class LLMPlayer(Player):
    """Owns prompting, parsing, retries and the failure policy. Subclasses implement `_ask`."""

    def __init__(
        self,
        name: str,
        model: str,
        max_tokens: int = 1024,
        temperature: float | None = None,
        timeout: float = 60,
        reasoning: bool = True,
        max_retries: int = 3,
        on_failure: str = "forfeit",
    ) -> None:
        if on_failure not in ("forfeit", "random_legal_move"):
            raise ValueError(f"on_failure must be 'forfeit' or 'random_legal_move', got {on_failure!r}")
        super().__init__(name)
        self.model, self.max_tokens, self.temperature = model, max_tokens, temperature
        self.timeout, self.reasoning = timeout, reasoning
        self.max_retries, self.on_failure = max_retries, on_failure

    @abstractmethod
    async def _ask(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, int, dict[str, Any]]:
        """Return (reply text, tokens used, assistant message to append on re-prompt)."""

    async def choose_move(self, board: chess.Board) -> MoveResult:
        system = system_prompt(self.reasoning)
        messages: list[dict[str, Any]] = [{"role": "user", "content": turn_prompt(board)}]
        log: list[str] = []
        tokens, start = 0, time.perf_counter()
        for attempt in range(self.max_retries + 1):
            log.append(f"--- attempt {attempt + 1} ---")
            try:
                text, used, assistant = await asyncio.wait_for(self._ask(system, messages), self.timeout)
            except TimeoutError:
                log.append(f"[error: timed out after {self.timeout}s]")
                continue
            except Exception as e:  # API/network errors count as a failed attempt
                log.append(f"[error: {type(e).__name__}: {e}]")
                continue
            tokens += used
            log.append(text)
            if not text.strip():
                log.append("[error: empty reply]")
                continue
            try:
                move = parse_move(text, board)
            except ValueError as e:
                log.append(f"[error: {e}]")
                messages += [
                    assistant,
                    {
                        "role": "user",
                        "content": f"Invalid reply: {e}.\nLegal moves (SAN): {legal_san(board)}\n"
                        "Try again. The final line must be exactly `MOVE: <SAN>`.",
                    },
                ]
                continue
            return MoveResult(
                move.uci(), "\n".join(log), time.perf_counter() - start, tokens, attempt, attempt
            )

        failed = self.max_retries + 1
        result = MoveResult(None, "", time.perf_counter() - start, tokens, self.max_retries, failed, "forfeit")
        if self.on_failure == "random_legal_move":
            result.uci, result.fallback = random.choice(list(board.legal_moves)).uci(), "random"
        log.append(f"[{failed} failed attempts: {result.fallback}]")
        result.raw = "\n".join(log)
        return result


class AnthropicPlayer(LLMPlayer):
    def __init__(self, name: str, model: str, api_key_env: str | None = None, effort: str | None = None, **kw: Any):
        super().__init__(name, model, **kw)
        self.effort = effort
        # SDK retries off: our loop owns retries and the per-attempt timeout.
        self.client = anthropic.AsyncAnthropic(
            api_key=os.environ.get(api_key_env) if api_key_env else None, timeout=self.timeout, max_retries=0
        )

    async def _ask(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, int, dict[str, Any]]:
        extra: dict[str, Any] = {}
        if self.effort:
            extra["output_config"] = {"effort": self.effort}
        if self.temperature is not None:  # current Claude models reject it; older ones accept it
            extra["extra_body"] = {"temperature": self.temperature}
        resp = await self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=messages, **extra
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused")
        text = "".join(b.text for b in resp.content if b.type == "text")
        # Echo content unchanged (thinking blocks included) so the re-prompt stays valid.
        return text, resp.usage.input_tokens + resp.usage.output_tokens, {"role": "assistant", "content": resp.content}


class OpenAIPlayer(LLMPlayer):
    """OpenAI, or any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) via base_url."""

    def __init__(
        self, name: str, model: str, api_key_env: str | None = None, base_url: str | None = None, **kw: Any
    ):
        super().__init__(name, model, **kw)
        self.base_url = base_url
        key = os.environ.get(api_key_env) if api_key_env else None
        self.client = openai.AsyncOpenAI(
            api_key=key or "not-needed", base_url=base_url, timeout=self.timeout, max_retries=0
        )

    async def _ask(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, int, dict[str, Any]]:
        extra: dict[str, Any] = {}
        if self.temperature is not None:
            extra["temperature"] = self.temperature
        # ponytail: official OpenAI wants max_completion_tokens; compat servers (Ollama) document max_tokens.
        extra["max_tokens" if self.base_url else "max_completion_tokens"] = self.max_tokens
        resp = await self.client.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": system}, *messages], **extra
        )
        text = resp.choices[0].message.content or ""
        used = resp.usage.total_tokens if resp.usage else 0
        return text, used, {"role": "assistant", "content": text}


TYPES: dict[str, type[Player]] = {
    "random": RandomPlayer,
    "stockfish": StockfishPlayer,
    "anthropic": AnthropicPlayer,
    "openai": OpenAIPlayer,
}


def unavailable_reason(cfg: dict[str, Any]) -> str | None:
    kind = cfg.get("type")
    if kind not in TYPES:
        return f"unknown type {kind!r}"
    if kind == "stockfish" and not shutil.which(cfg.get("path", "stockfish")):
        return "Stockfish binary not found"
    env = cfg.get("api_key_env")
    if env and not os.environ.get(env):
        return f"{env} not set"
    return None


def build_player(pid: str, cfg: dict[str, Any], llm_defaults: dict[str, Any] | None = None) -> Player:
    kwargs = {k: v for k, v in cfg.items() if k != "type"}
    kwargs.setdefault("name", pid)
    cls = TYPES[cfg["type"]]
    if issubclass(cls, LLMPlayer):
        kwargs = {**(llm_defaults or {}), **kwargs}
    return cls(**kwargs)  # unknown config keys raise TypeError: catches typos
