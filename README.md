# AI Chess Arena

Pick a player for White and one for Black, press Start, and watch the game play out live in the browser. Players can be Claude models, OpenAI models (or anything with an OpenAI-compatible API, such as Ollama), Stockfish, or a random-move baseline.

- Backend: Python 3.11+, FastAPI, [python-chess](https://python-chess.readthedocs.io/) (rules, legality, game endings).
- Frontend: Vite + TypeScript with [chessground](https://github.com/lichess-org/chessground), served by FastAPI.
- Live updates: Server-Sent Events. Each event is a full snapshot of the game, so a page refresh picks up mid-game.

## Quick start (no API keys needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..   # Node 20.19+ or 22.12+

cp .env.example .env          # add keys later if you want LLM players
uvicorn arena.server:app      # then open http://localhost:8000
```

Choose **Random mover** for both sides and press Start. Players without an API key or binary still appear in the dropdowns, greyed out, with the reason they're unavailable.

Frontend development with hot reload: run `uvicorn arena.server:app --reload` and, in `frontend/`, `npm run dev`. Then open http://localhost:5173. Vite proxies `/api` to port 8000.

## Tests

```bash
pytest
```

The suite covers move parsing, retries and the failure policy (using a scripted fake LLM), every game ending from known FEN positions, the provider adapters against faked SDK clients, and a Random-vs-Random smoke test, both in-process and over HTTP with SSE. The Stockfish test is skipped if the binary isn't on `PATH`.

## Adding a model

Add an entry under `players:` in `config.yaml`. The key is the id; `name` is what the UI shows. Changes take effect on the next page load, with no restart.

```yaml
players:
  claude-haiku:
    type: anthropic              # anthropic | openai | stockfish | random
    name: Claude Haiku 5.5
    model: claude-haiku-5-5
    api_key_env: ANTHROPIC_API_KEY   # name of the env var holding the key (set it in .env)
    max_tokens: 16000            # Claude's thinking counts toward this
    effort: medium               # optional, Claude only: low | medium | high | xhigh | max
    # any llm_defaults key can be overridden per player:
    timeout: 90
    on_failure: random_legal_move

  my-local-model:
    type: openai                 # any OpenAI-compatible server
    name: Qwen (Ollama)
    model: qwen2.5:14b
    base_url: http://localhost:11434/v1
    max_tokens: 1024
    temperature: 0.2
```

Notes:

- `temperature` is optional and only sent if set. Current Claude models and OpenAI reasoning models reject it, so leave it out for those.
- For official OpenAI models, `max_tokens` is sent as `max_completion_tokens`. With a `base_url` it's sent as `max_tokens`, the parameter Ollama documents.
- Check model IDs before use. The OpenAI ID in `config.yaml` is an example I couldn't verify. See [platform.openai.com/docs/models](https://platform.openai.com/docs/models).
- Unknown keys raise an error when a game starts, so typos don't pass silently.
- Shared LLM settings live in `llm_defaults`: `max_retries` (3), `on_failure` (`forfeit` or `random_legal_move`), `timeout` (60s per attempt) and `reasoning` (allow reasoning before the move line).

## How LLM moves work

Each turn the model gets the side to move, the FEN, the full move history in SAN, and the list of legal moves in SAN. It must end its reply with `MOVE: <SAN>`.

The reply is parsed with a regex (the last `MOVE:` line wins; markdown and trailing punctuation are tolerated) and validated with python-chess, which accepts SAN or UCI. If the reply is illegal, unparseable, empty, times out or hits an API error, the model is re-prompted with the error and the legal move list, up to `max_retries` times. After that, `on_failure` applies:

- `forfeit`: the game ends and the opponent wins.
- `random_legal_move`: a random legal move is played. It's marked with an **R** on the scoresheet and a comment in the PGN.

Failed attempts are counted per player and per move. The scoresheet shows them as red superscripts, and the last raw reply (including errors) is under **Last reply**.

## Game endings

Checkmate, stalemate, threefold repetition, the fifty-move rule, insufficient material, the seventy-five-move and fivefold rules, forfeit, and a move cap (`game.max_plies`, default 200 half-moves, declared a draw). Threefold and fifty-move draws are applied when the position actually occurs, not one move early when a player could merely claim it.

## Output

Every game, including aborted ones, is saved to `games/` as a PGN (player names in the headers, termination reason, comments on fallback and forfeit moves) and a JSON log of every turn: SAN, UCI, FEN after, latency, tokens, retries, failed attempts, fallback and raw output. The **Download PGN** and **Download JSON log** links export the current or last game.

Set **Games** above 1 for a series. Colours alternate each game and a win/draw/loss tally shows above the scoresheet.

## Installing Stockfish

- macOS: `brew install stockfish`
- Debian/Ubuntu: `sudo apt install stockfish`. The binary goes to `/usr/games/stockfish`, which often isn't on `PATH`, so either add `/usr/games` to `PATH` or set `path: /usr/games/stockfish` in `config.yaml`.
- Windows: download from [stockfishchess.org](https://stockfishchess.org/download/) and set `path:` to the `.exe`.

`skill_level` (0–20) and `think_time` (seconds per move) are set per player. If the binary isn't found, the player is greyed out in the UI.

## API cost

Each LLM game makes a lot of calls. A full 200-ply game is about **100 calls per LLM side**, and every retry is an extra call. Two things make it more expensive than it looks:

- The prompt grows with the game, because it carries the full move history plus the legal move list. Expect a few hundred input tokens at the start, rising to over a thousand late in a long game.
- On Claude models, thinking tokens are billed as output and usually dominate. `effort` is the main lever.

As an illustration only (your numbers will differ): 100 calls × ~800 input tokens and ~2,000 output tokens is about 80k input and 200k output tokens per side per game. Multiply by the per-token prices on the [Anthropic](https://www.anthropic.com/pricing) or [OpenAI](https://openai.com/api/pricing/) pricing page.

The UI shows token totals per player. Run a short trial first (set `game.max_plies: 40`) to measure real usage before running a series.

## Layout

```
arena/players.py   Player interface, LLM protocol (prompt, parse, retries), Random/Stockfish/Anthropic/OpenAI players
arena/game.py      End detection, game loop, PGN/JSON export, multi-game series
arena/server.py    FastAPI: /api/players, /api/start, /api/control/{pause,resume,abort}, /api/events (SSE), exports
frontend/          Vite + TypeScript single page
config.yaml        Player definitions
```

## Limitations

- One match at a time per server process.
- Pause takes effect after the move in progress, so an LLM mid-reply finishes its turn first.
- API keys stay server-side: `/api/players` returns only names and availability.
- chessground is GPL-3.0. That's fine for personal use, but it matters if you distribute this app.
