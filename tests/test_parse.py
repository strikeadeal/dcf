import chess
import pytest

from arena.players import parse_move


@pytest.mark.parametrize(
    "text,uci",
    [
        ("MOVE: Nf3", "g1f3"),
        ("The knight develops.\nMOVE: Nf3", "g1f3"),
        ("MOVE: g1f3", "g1f3"),  # UCI
        ("MOVE: G1F3", "g1f3"),
        ("**MOVE: e4**", "e2e4"),  # markdown
        ("MOVE: `e4`", "e2e4"),
        ("move: e4.", "e2e4"),  # case, trailing punctuation
        ("MOVE: e4!?", "e2e4"),  # annotation
        ("MOVE: e4\nActually, on reflection:\nMOVE: d4", "d2d4"),  # multiple MOVE lines: last wins
    ],
)
def test_parses(text: str, uci: str) -> None:
    assert parse_move(text, chess.Board()).uci() == uci


@pytest.mark.parametrize(
    "text,uci",
    [
        ("MOVE: O-O", "e1g1"),
        ("MOVE: 0-0-0", "e1c1"),
        ("MOVE: bxa8=Q+", "b7a8q"),  # promotion with check suffix
        ("MOVE: b7a8q", "b7a8q"),
    ],
)
def test_castling_and_promotion(text: str, uci: str) -> None:
    board = chess.Board("r3k2r/1P6/8/8/8/8/8/R3K2R w KQkq - 0 1")
    assert parse_move(text, board).uci() == uci


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I'll play e4",  # no MOVE line
        "MOVE:",
        "MOVE: banana",
        "MOVE: Ke2",  # illegal SAN
        "MOVE: e2e5",  # illegal UCI
        "MOVE: --",  # python-chess parses these as null moves
        "MOVE: 0000",
        "I would remove: e4",  # 'MOVE:' inside another word
    ],
)
def test_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_move(text, chess.Board())
