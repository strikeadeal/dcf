import { Chessground } from 'chessground';
import type { Key } from 'chessground/types';
import 'chessground/assets/chessground.base.css';
import 'chessground/assets/chessground.cburnett.css';
import './style.css';

type Color = 'white' | 'black';
type Status = 'idle' | 'running' | 'paused' | 'finished' | 'aborted';
interface Stats { moves: number; illegal: number; avg_latency: number; tokens: number }
interface PlayedMove { san: string; illegal: number; fallback: string | null }
interface Turn { ply: number; player: string; raw: string }
interface Series { game: number; of: number; a: string; b: string; a_wins: number; draws: number; b_wins: number }
interface State {
  status: Status;
  white?: string;
  black?: string;
  fen?: string;
  turn?: Color;
  check?: boolean;
  last_move?: [Key, Key] | null;
  moves?: PlayedMove[];
  last?: Turn | null;
  stats?: Record<Color, Stats>;
  result?: string | null;
  reason?: string | null;
  series?: Series;
}
interface PlayerOption { id: string; name: string; unavailable: string | null }

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const el = (tag: string, cls = '', text = '') => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  node.textContent = text;
  return node;
};

const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const cg = Chessground($('board'), {
  viewOnly: true,
  coordinates: true,
  animation: { enabled: !reduceMotion, duration: 250 },
});

// Player cards: same markup for both sides, filled by render().
const STAT_LABELS: [keyof Stats, string][] = [
  ['moves', 'Moves'],
  ['illegal', 'Failed attempts'],
  ['avg_latency', 'Avg latency'],
  ['tokens', 'Tokens'],
];
for (const color of ['white', 'black'] as Color[]) {
  const card = $(`card-${color}`);
  const who = el('div', 'who');
  who.append(el('span', `chip ${color}`), el('span', 'name', color === 'white' ? 'White' : 'Black'), el('span', 'state'));
  const stats = el('dl', 'stats');
  for (const [key, label] of STAT_LABELS) {
    const cell = el('div');
    cell.append(el('dt', '', label), el('dd', `stat-${key}`, '0'));
    stats.append(cell);
  }
  card.append(who, stats);
}

function showError(err: unknown): void {
  const box = $('error');
  box.textContent = err instanceof Error ? err.message : String(err);
  box.hidden = false;
}

async function post(path: string, body?: object): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail ?? res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
}

async function loadPlayers(): Promise<void> {
  const players: PlayerOption[] = await (await fetch('/api/players')).json();
  for (const id of ['white', 'black']) {
    $<HTMLSelectElement>(id).replaceChildren(
      ...players.map((p) => {
        const opt = new Option(p.unavailable ? `${p.name} (${p.unavailable})` : p.name, p.id);
        opt.disabled = !!p.unavailable;
        return opt;
      }),
    );
  }
}

let events: EventSource | undefined;
function connect(): void {
  events?.close();
  events = new EventSource('/api/events');
  events.onmessage = (e) => {
    const state: State = JSON.parse(e.data);
    render(state);
    if (state.status !== 'running' && state.status !== 'paused') events?.close();
  };
}

function setControls(status: Status): void {
  const live = status === 'running' || status === 'paused';
  for (const id of ['white', 'black', 'games', 'delay', 'start']) $<HTMLInputElement>(id).disabled = live;
  $<HTMLButtonElement>('pause').disabled = status !== 'running';
  $<HTMLButtonElement>('resume').disabled = status !== 'paused';
  $<HTMLButtonElement>('abort').disabled = !live;
}

function resultText(s: State): string {
  if (s.result === '1-0') return `1–0 · ${s.white} wins`;
  if (s.result === '0-1') return `0–1 · ${s.black} wins`;
  if (s.result === '1/2-1/2') return '½–½ · Draw';
  return 'Aborted';
}

function renderMoves(moves: PlayedMove[]): void {
  const list = $('moves');
  const rows: HTMLElement[] = [];
  for (let i = 0; i < moves.length; i += 2) {
    const row = el('li');
    row.append(el('span', 'n', `${i / 2 + 1}`));
    for (const move of [moves[i], moves[i + 1]]) {
      const cell = el('span', 'san', move?.san ?? '');
      if (move?.fallback === 'random') {
        const mark = el('sup', 'mark fb', 'R');
        mark.title = `Random fallback after ${move.illegal} failed attempts`;
        cell.append(mark);
      } else if (move?.illegal) {
        const mark = el('sup', 'mark', `${move.illegal}`);
        mark.title = `${move.illegal} failed attempt${move.illegal > 1 ? 's' : ''} before this move`;
        cell.append(mark);
      }
      row.append(cell);
    }
    rows.push(row);
  }
  list.replaceChildren(...(rows.length ? rows : [el('li', 'empty', 'Waiting for the first move.')]));
  list.scrollTop = list.scrollHeight;
}

function render(s: State): void {
  setControls(s.status);
  document.body.dataset.status = s.status;
  if (!s.fen || !s.turn) return; // idle: nothing played yet

  const over = !!s.result;
  cg.set({
    fen: s.fen.split(' ')[0],
    turnColor: s.turn,
    lastMove: s.last_move ?? undefined,
    check: s.check ? s.turn : false,
  });

  for (const color of ['white', 'black'] as Color[]) {
    const card = $(`card-${color}`);
    const toMove = !over && s.turn === color;
    card.classList.toggle('active', toMove);
    card.querySelector('.name')!.textContent = s[color] ?? color;
    card.querySelector('.state')!.textContent = toMove ? (s.status === 'paused' ? 'paused' : 'thinking') : '';
    const stats = s.stats?.[color];
    for (const [key] of STAT_LABELS) {
      const value = stats?.[key] ?? 0;
      const cell = card.querySelector(`.stat-${key}`)!;
      cell.textContent = key === 'avg_latency' ? `${value.toFixed(2)}s` : value.toLocaleString('en-AU');
      cell.classList.toggle('bad', key === 'illegal' && value > 0);
    }
  }

  renderMoves(s.moves ?? []);
  $('turn').textContent = over ? 'Game over' : s.turn === 'white' ? `White (${s.white})` : `Black (${s.black})`;
  $('fen').textContent = s.fen;
  $('raw-who').textContent = s.last ? `· ply ${s.last.ply}, ${s.last.player}` : '';
  $('raw').textContent = s.last?.raw || 'No moves yet.';
  $('exports').hidden = false;

  $('banner').hidden = !over;
  $('banner-result').textContent = over ? resultText(s) : '';
  $('banner-reason').textContent = s.result === '*' ? '' : (s.reason ?? '');

  const series = s.series;
  $('series').hidden = !series || series.of < 2;
  if (series) {
    $('series').textContent =
      `Game ${series.game} of ${series.of} · ${series.a} ${series.a_wins} · draws ${series.draws} · ${series.b} ${series.b_wins}`;
  }
}

$('setup').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('error').hidden = true;
  const delay = $<HTMLInputElement>('delay').value;
  try {
    await post('/api/start', {
      white: $<HTMLSelectElement>('white').value,
      black: $<HTMLSelectElement>('black').value,
      games: Number($<HTMLInputElement>('games').value) || 1,
      move_delay: delay === '' ? null : Number(delay),
    });
    connect();
  } catch (err) {
    showError(err);
  }
});
$('banner').addEventListener('click', () => ($('banner').hidden = true)); // reveal the final position
for (const action of ['pause', 'resume', 'abort']) {
  $(action).addEventListener('click', () => post(`/api/control/${action}`).catch(showError));
}

setControls('idle');
loadPlayers().catch(showError);
connect();
