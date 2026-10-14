import asyncio

import chess
import pytest

from arena.game import DRAW, Game, end_reason
from arena.players import MoveResult, Player


@pytest.mark.parametrize(
    "fen,result,reason",
    [
        ("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3", "0-1", "checkmate"),
        ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", DRAW, "stalemate"),
        ("8/8/8/8/8/8/k7/7K w - - 0 1", DRAW, "insufficient material"),
        ("8/8/8/8/8/k7/8/R6K w - - 100 80", DRAW, "fifty-move rule"),
        ("8/8/8/8/8/k7/8/R6K w - - 150 80", DRAW, "seventy-five-move rule"),
    ],
)
def test_end_conditions(fen: str, result: str, reason: str) -> None:
    assert end_reason(chess.Board(fen), 200) == (result, reason)


def test_ongoing_position_does_not_end() -> None:
    assert end_reason(chess.Board("8/8/8/8/8/k7/8/R6K w - - 99 80"), 200) is None


def test_threefold_repetition() -> None:
    board = chess.Board()
    for san in ["Nf3", "Nf6", "Ng1", "Ng8", "Nf3", "Nf6", "Ng1"]:
        board.push_san(san)
    assert end_reason(board, 200) is None  # repetition only available, not yet on the board
    board.push_san("Ng8")
    assert end_reason(board, 200) == (DRAW, "threefold repetition")


def test_move_cap() -> None:
    board = chess.Board()
    for san in ["Nf3", "Nf6", "Ng1", "Ng8"]:
        board.push_san(san)
    assert end_reason(board, 5) is None
    assert end_reason(board, 4) == (DRAW, "move cap (4 plies)")


class Fixed(Player):
    def __init__(self, moves: list[str]) -> None:
        super().__init__("Fixed")
        self.moves = moves

    async def choose_move(self, board: chess.Board) -> MoveResult:
        return MoveResult(self.moves.pop(0))


def test_orchestrator_detects_mate() -> None:
    g = Game(Fixed(["f2f3", "g2g4"]), Fixed(["e7e5", "d8h4"]), move_delay=0)
    asyncio.run(g.run())
    snap = g.snapshot()
    assert (g.result, g.reason) == ("0-1", "checkmate")
    assert [m["san"] for m in snap["moves"]] == ["f3", "e5", "g4", "Qh4#"] and snap["check"] and snap["last_move"] == ["d8", "h4"]


def test_orchestrator_from_fen_stops_immediately() -> None:
    g = Game(Fixed([]), Fixed([]), fen="7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", move_delay=0)
    asyncio.run(g.run())
    assert (g.result, g.reason, g.turns) == (DRAW, "stalemate", [])
