"""Game orchestration: end detection, the move loop, exports, and multi-game series."""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .players import MoveResult, Player, build_player

DRAW = "1/2-1/2"
NAMES = {chess.Termination.SEVENTYFIVE_MOVES: "seventy-five-move rule"}


def end_reason(board: chess.Board, max_plies: int) -> tuple[str, str] | None:
    if outcome := board.outcome():  # checkmate, stalemate, insufficient material, 75-move, fivefold
        term = outcome.termination
        return outcome.result(), NAMES.get(term, term.name.lower().replace("_", " "))
    # Not outcome(claim_draw=True): that ends the game when a repetition is merely available next move.
    if board.is_repetition(3):
        return DRAW, "threefold repetition"
    if board.is_fifty_moves():
        return DRAW, "fifty-move rule"
    if len(board.move_stack) >= max_plies:
        return DRAW, f"move cap ({max_plies} plies)"
    return None


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class Game:
    white: Player
    black: Player
    max_plies: int = 200
    move_delay: float = 1.0
    fen: str = chess.STARTING_FEN
    running: asyncio.Event = field(default_factory=_set_event)  # cleared = paused
    on_change: Callable[[], None] = lambda: None
    board: chess.Board = field(init=False)
    turns: list[dict[str, Any]] = field(default_factory=list, init=False)
    result: str | None = field(default=None, init=False)
    reason: str | None = field(default=None, init=False)
    started: datetime = field(default_factory=datetime.now, init=False)

    def __post_init__(self) -> None:
        self.board = chess.Board(self.fen)

    async def run(self) -> None:
        try:
            while not (end := end_reason(self.board, self.max_plies)):
                if self.turns:
                    await asyncio.sleep(self.move_delay)
                await self.running.wait()
                white_to_move = self.board.turn
                player = self.white if white_to_move else self.black
                try:
                    res = await player.choose_move(self.board.copy())
                except Exception as e:  # a crashed player (e.g. engine died) forfeits rather than hanging the match
                    res = MoveResult(None, raw=f"[error: {type(e).__name__}: {e}]", fallback="forfeit")
                try:
                    move = chess.Move.from_uci(res.uci) if res.uci else None
                except ValueError:
                    move = None
                if move is None or move not in self.board.legal_moves:
                    self._record(player, res, None)
                    side = "White" if white_to_move else "Black"
                    end = (
                        "0-1" if white_to_move else "1-0",
                        f"forfeit: {side} ({player.name}) made no legal move"
                        + (f" after {res.illegal} failed attempts" if res.illegal else f" ({res.raw})"),
                    )
                    break
                self._record(player, res, move)
                self.on_change()
            self.result, self.reason = end
        except asyncio.CancelledError:
            self.result, self.reason = "*", "aborted"
            raise
        finally:
            for p in (self.white, self.black):
                await p.close()
            self.on_change()

    def _record(self, player: Player, res: MoveResult, move: chess.Move | None) -> None:
        turn = {
            "ply": len(self.board.move_stack) + 1,
            "color": "white" if self.board.turn else "black",
            "player": player.name,
            "san": self.board.san(move) if move else None,
            "uci": res.uci,
            "latency": round(res.latency, 3),
            "tokens": res.tokens,
            "retries": res.retries,
            "illegal": res.illegal,
            "fallback": res.fallback,
            "raw": res.raw,
        }
        if move:
            self.board.push(move)
        turn["fen"] = self.board.fen()
        self.turns.append(turn)

    def stats(self) -> dict[str, dict[str, Any]]:
        out = {}
        for color in ("white", "black"):
            turns = [t for t in self.turns if t["color"] == color]
            moves = [t for t in turns if t["san"]]
            out[color] = {
                "moves": len(moves),
                "illegal": sum(t["illegal"] for t in turns),
                "avg_latency": round(sum(t["latency"] for t in turns) / len(turns), 2) if turns else 0,
                "tokens": sum(t["tokens"] for t in turns),
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        last_move = self.board.move_stack[-1] if self.board.move_stack else None
        return {
            "white": self.white.name,
            "black": self.black.name,
            "fen": self.board.fen(),
            "turn": "white" if self.board.turn else "black",
            "check": self.board.is_check(),
            "last_move": [chess.square_name(last_move.from_square), chess.square_name(last_move.to_square)]
            if last_move
            else None,
            "moves": [{k: t[k] for k in ("san", "illegal", "fallback")} for t in self.turns if t["san"]],
            "last": self.turns[-1] if self.turns else None,
            "stats": self.stats(),
            "result": self.result,
            "reason": self.reason,
        }

    def pgn(self) -> str:
        game = chess.pgn.Game.from_board(self.board)
        game.headers.update(
            Event="AI Chess Arena",
            Date=self.started.strftime("%Y.%m.%d"),
            White=self.white.name,
            Black=self.black.name,
            Result=self.result or "*",
            Termination=self.reason or "unterminated",
        )
        played = [t for t in self.turns if t["san"]]
        for node, turn in zip(game.mainline(), played):
            if turn["fallback"] == "random":
                node.comment = f"random fallback move after {turn['illegal']} failed attempts by {turn['player']}"
        if self.reason and self.reason.startswith("forfeit"):
            game.end().comment = self.reason
        return str(game) + "\n"

    def log(self) -> dict[str, Any]:
        return {
            "white": self.white.name,
            "black": self.black.name,
            "start_fen": self.fen,
            "started": self.started.isoformat(timespec="seconds"),
            "result": self.result,
            "reason": self.reason,
            "stats": self.stats(),
            "turns": self.turns,
        }

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        slug = lambda s: re.sub(r"[^\w.-]+", "_", s)  # noqa: E731
        stem = directory / f"{self.started:%Y%m%d-%H%M%S-%f}_{slug(self.white.name)}_vs_{slug(self.black.name)}"
        stem.with_suffix(".pgn").write_text(self.pgn())
        stem.with_suffix(".json").write_text(json.dumps(self.log(), indent=2))
        return stem


class Match:
    """N games between players A and B, alternating colours. Must be created inside a running loop."""

    def __init__(
        self, config: dict[str, Any], a: str, b: str, games: int, games_dir: Path, move_delay: float | None = None
    ) -> None:
        self.config, self.ids, self.n, self.games_dir = config, (a, b), games, games_dir
        settings = config.get("game", {})
        self.max_plies = settings.get("max_plies", 200)
        self.move_delay = settings.get("move_delay", 1.0) if move_delay is None else move_delay
        self.names = tuple(config["players"][pid].get("name", pid) for pid in self.ids)
        self.tally = {"a_wins": 0, "draws": 0, "b_wins": 0}
        self.index = 0
        self.game: Game | None = None
        self.done = self.aborted = False
        self.running = _set_event()
        self.changed = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def _build(self, pid: str) -> Player:
        return build_player(pid, self.config["players"][pid], self.config.get("llm_defaults"))

    async def _run(self) -> None:
        try:
            for i in range(self.n):
                self.index = i
                if i:
                    await asyncio.sleep(self.move_delay * 3)  # let the result banner be read
                a_white = i % 2 == 0
                a, b = self._build(self.ids[0]), self._build(self.ids[1])
                white, black = (a, b) if a_white else (b, a)
                self.game = Game(
                    white, black, self.max_plies, self.move_delay, running=self.running, on_change=self.notify
                )
                self.notify()  # show the fresh board now, not after the first (possibly slow) move
                try:
                    await self.game.run()
                finally:
                    self.game.save(self.games_dir)
                winner = {"1-0": "white", "0-1": "black"}.get(self.game.result or "")
                if winner is None:
                    self.tally["draws"] += 1
                else:
                    self.tally["a_wins" if (winner == "white") == a_white else "b_wins"] += 1
        finally:
            self.done = True
            self.notify()

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def pause(self) -> None:
        self.running.clear()
        self.notify()

    def resume(self) -> None:
        self.running.set()
        self.notify()

    def abort(self) -> None:
        if not self.done:
            self.aborted = True
            self.task.cancel()

    @property
    def status(self) -> str:
        if self.done:
            return "aborted" if self.aborted else "finished"
        return "running" if self.running.is_set() else "paused"

    def snapshot(self) -> dict[str, Any]:
        snap = self.game.snapshot() if self.game else {}
        snap["status"] = self.status
        snap["series"] = {"game": self.index + 1, "of": self.n, "a": self.names[0], "b": self.names[1], **self.tally}
        return snap
