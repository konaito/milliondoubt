/* Million Doubt — a dependency-free GitHub Pages client. */

const RANKS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2"];
const SUITS = ["♠", "♥", "♦", "♣"];
const SUIT_NAMES = ["スペード", "ハート", "ダイヤ", "クラブ"];
const JOKER_RANK = 13;
const EIGHT_RANK = 5;
const JACK_RANK = 8;
const TURN_TIME_MS = 60_000;
const ACTION_INCREMENT_MS = 10_000;
const CLOCK_TICK_MS = 250;

const CARDS = Array.from({ length: 54 }, (_, id) => {
  if (id >= 52) return { id, rank: JOKER_RANK, suit: -1, joker: true };
  return { id, rank: Math.floor(id / 4), suit: id % 4, joker: false };
});

const $ = (selector) => document.querySelector(selector);

let webModel = null;
let toastTimer = null;
let cpuTimer = null;
let clockTimer = null;
let state = freshState();

function freshState() {
  return {
    hands: [[], []],
    turn: 0,
    current: null,
    effectiveCurrent: null,
    field: [],
    fieldHidden: new Set(),
    revealed: new Set(),
    lastPlayer: null,
    passes: 0,
    revolution: false,
    jBack: false,
    suitLock: new Set(),
    phase: "play",
    pending: null,
    pendingBefore: null,
    pendingTruth: null,
    penaltySelector: null,
    penaltyLoser: null,
    penaltySelected: new Set(),
    winner: null,
    endReason: null,
    round: 1,
    selected: new Set(),
    hiddenSelected: new Set(),
    clockMs: [TURN_TIME_MS, TURN_TIME_MS],
    turnDeadline: null,
    log: [],
  };
}

function shuffle(items) {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

function rankStrength(rank, revolution, jBack) {
  if (rank === JOKER_RANK) return 13;
  const reversed = Boolean(revolution) !== Boolean(jBack);
  return reversed ? 12 - rank : rank;
}

function claimPower(claim, revolution, jBack) {
  if (claim.kind !== "straight") return rankStrength(claim.high, revolution, jBack);
  let power = -1;
  for (let rank = claim.start; rank < claim.start + claim.length; rank += 1) {
    power = Math.max(power, rankStrength(rank, revolution, jBack));
  }
  return power;
}

function claimKey(claim) {
  return [claim.kind, claim.high, claim.start, claim.length, claim.suit].join(":");
}

function claimOptions(ids, hidden = ids.map(() => false)) {
  if (!ids.length) return [];
  const cards = ids.map((id) => CARDS[id]);
  const fixed = cards.filter((_, index) => !hidden[index]);
  const unknown = cards.length - fixed.length;
  const claims = [];
  const add = (claim) => {
    if (!claims.some((item) => claimKey(item) === claimKey(claim))) claims.push(claim);
  };

  if (cards.length === 1) {
    if (!fixed.length) {
      for (let rank = 0; rank <= JOKER_RANK; rank += 1) add({ kind: "single", high: rank, start: -1, length: 1, suit: -1 });
    } else if (fixed[0].joker) {
      add({ kind: "single", high: JOKER_RANK, start: -1, length: 1, suit: -1 });
    } else {
      add({ kind: "single", high: fixed[0].rank, start: -1, length: 1, suit: -1 });
    }
  }

  if (cards.length >= 2 && cards.length <= 4) {
    const fixedRanks = new Set(fixed.filter((card) => !card.joker).map((card) => card.rank));
    if (fixedRanks.size <= 1) {
      const candidates = fixedRanks.size ? [...fixedRanks] : Array.from({ length: 13 }, (_, rank) => rank);
      candidates.forEach((rank) => add({ kind: "group", high: rank, start: -1, length: cards.length, suit: -1 }));
    }
    if (!unknown && cards.length === 2 && fixed.every((card) => card.joker)) {
      add({ kind: "group", high: JOKER_RANK, start: -1, length: 2, suit: -1 });
    }
  }

  if (cards.length >= 3) {
    for (let suit = 0; suit < 4; suit += 1) {
      for (let start = 0; start <= 13 - cards.length; start += 1) {
        const end = start + cards.length;
        const fixedNonJokers = fixed.filter((card) => !card.joker);
        const inRange = fixedNonJokers.every((card) => card.suit === suit && card.rank >= start && card.rank < end);
        const uniqueRanks = new Set(fixedNonJokers.map((card) => card.rank)).size === fixedNonJokers.length;
        const jokerCount = fixed.filter((card) => card.joker).length;
        if (inRange && uniqueRanks && (unknown || jokerCount <= 2)) {
          add({ kind: "straight", high: end - 1, start, length: cards.length, suit });
        }
      }
    }
  }
  return claims;
}

function allHidden(play) {
  return Boolean(play && play.hidden.length && play.hidden.every(Boolean));
}

function visibleSuits(play) {
  if (!play) return new Set();
  return new Set(play.cardIds
    .filter((_, index) => !play.hidden[index])
    .map((id) => CARDS[id])
    .filter((card) => !card.joker)
    .map((card) => card.suit));
}

function visibleRank(play) {
  if (!play) return null;
  const ranks = play.cardIds
    .filter((_, index) => !play.hidden[index])
    .map((id) => CARDS[id])
    .filter((card) => !card.joker)
    .map((card) => card.rank);
  return ranks.length ? Math.max(...ranks) : null;
}

function legalAgainst(current, action, actual = false) {
  if (!current || allHidden(current)) {
    return claimOptions(action.cardIds, actual ? action.cardIds.map(() => false) : action.hidden).length > 0;
  }
  const referenceClaims = claimOptions(current.cardIds, current.hidden);
  const actionClaims = claimOptions(action.cardIds, actual ? action.cardIds.map(() => false) : action.hidden);
  if (!referenceClaims.length || !actionClaims.length) return false;

  if (state.suitLock.size) {
    const suits = new Set(action.cardIds
      .filter((_, index) => !(actual ? false : action.hidden[index]))
      .map((id) => CARDS[id])
      .filter((card) => !card.joker)
      .map((card) => card.suit));
    if ([...suits].some((suit) => !state.suitLock.has(suit))) return false;
    if (actual && !suits.size) return false;
  }

  return referenceClaims.some((reference) => actionClaims.some((candidate) => (
    reference.kind === candidate.kind
    && reference.length === candidate.length
    && claimPower(candidate, state.revolution, state.jBack) > claimPower(reference, state.revolution, state.jBack)
  )));
}

function actualClaim(play) {
  const claims = claimOptions(play.cardIds, play.cardIds.map(() => false));
  if (!claims.length) return "不正な役";
  const claim = claims.sort((a, b) => claimPower(b, false, false) - claimPower(a, false, false))[0];
  return describeClaim(claim);
}

function describeClaim(claim) {
  if (claim.kind === "single") return claim.high === JOKER_RANK ? "ジョーカー" : RANKS[claim.high];
  if (claim.kind === "group") return `${claim.length}枚の${claim.high === JOKER_RANK ? "ジョーカー" : RANKS[claim.high]}`;
  return `${SUITS[claim.suit]} ${RANKS[claim.start]}〜${RANKS[claim.high]}の階段`;
}

function describePlay(play, includeHidden = false) {
  const names = play.cardIds.map((id, index) => {
    if (play.hidden[index] && !includeHidden) return "裏札";
    return cardLabel(id);
  });
  return names.join(" ");
}

function cardLabel(id) {
  const card = CARDS[id];
  return card.joker ? "JOKER" : `${RANKS[card.rank]}${SUITS[card.suit]}`;
}

function cardMarkup(id, hidden = false, options = {}) {
  const card = CARDS[id];
  const selected = options.selected ? " selected" : "";
  const penalty = options.penalty ? " penalty-card" : "";
  const revealing = options.revealing ? " is-revealing" : "";
  const rank = card.joker ? "★" : RANKS[card.rank];
  const suit = card.joker ? "JOKER" : SUITS[card.suit];
  const red = !card.joker && (card.suit === 1 || card.suit === 2) ? " red" : "";
  return `<button class="card${hidden ? " card-hidden" : ""}${selected}${penalty}${revealing}" type="button" data-card-id="${id}"${options.action ? ` data-card-action="${options.action}"` : ""} aria-label="${hidden ? "裏札" : cardLabel(id)}">` +
    `<span class="card-suit${red}">${suit}</span><span class="card-rank${red}">${rank}</span></button>`;
}

function snapshot() {
  return {
    current: state.current,
    effectiveCurrent: state.effectiveCurrent,
    revolution: state.revolution,
    jBack: state.jBack,
    suitLock: new Set(state.suitLock),
    field: [...state.field],
  };
}

function addLog(message) {
  const now = new Date();
  state.log.unshift({ time: `${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`, message });
  if (state.log.length > 18) state.log.length = 18;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2400);
}

function clearField() {
  state.current = null;
  state.effectiveCurrent = null;
  state.field = [];
  state.fieldHidden = new Set();
  state.revealed = new Set();
  state.passes = 0;
  state.jBack = false;
  state.suitLock = new Set();
}

function formatClock(milliseconds) {
  const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
  return "0" + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
}

function timedHumanPhase() {
  return state.turn === 0 && (
    state.phase === "play"
    || state.phase === "challenge-human"
    || (state.phase === "penalty" && state.penaltySelector === 0)
  );
}

function renderClock() {
  const opponentClock = $("#opponent-clock");
  const humanClock = $("#human-clock");
  const clockChip = $("#clock-chip");
  if (!opponentClock || !humanClock || !clockChip) return;
  opponentClock.textContent = formatClock(state.clockMs[1]);
  humanClock.textContent = formatClock(state.clockMs[0]);
  clockChip.textContent = state.turn === 0
    ? "あなた " + formatClock(state.clockMs[0])
    : "CPU " + formatClock(state.clockMs[1]);
  clockChip.classList.toggle("is-active", timedHumanPhase());
}

function stopClock() {
  window.clearInterval(clockTimer);
  clockTimer = null;
  state.turnDeadline = null;
}

function setTurn(player) {
  window.clearInterval(clockTimer);
  clockTimer = null;
  state.turn = player;
  state.turnDeadline = Date.now() + state.clockMs[player];
}

function updateClock() {
  if (!timedHumanPhase() || state.turnDeadline === null) {
    window.clearInterval(clockTimer);
    clockTimer = null;
    renderClock();
    return;
  }
  state.clockMs[0] = Math.max(0, state.turnDeadline - Date.now());
  renderClock();
  if (state.clockMs[0] === 0) {
    window.clearInterval(clockTimer);
    clockTimer = null;
    finish(1, "timeout");
  }
}

function startClock() {
  window.clearInterval(clockTimer);
  clockTimer = null;
  if (!timedHumanPhase() || state.turnDeadline === null) {
    renderClock();
    return;
  }
  clockTimer = window.setInterval(updateClock, CLOCK_TICK_MS);
  updateClock();
}

function completeActionClock(player) {
  if (state.turn === player && state.turnDeadline !== null) {
    const remaining = Math.max(0, state.turnDeadline - Date.now());
    state.clockMs[player] = Math.min(TURN_TIME_MS, remaining + ACTION_INCREMENT_MS);
  }
  window.clearInterval(clockTimer);
  clockTimer = null;
  state.turnDeadline = null;
  renderClock();
}

function finish(winner, reason) {
  stopClock();
  window.clearTimeout(toastTimer);
  toastTimer = null;
  $("#toast").classList.remove("is-visible");
  state.winner = winner;
  state.endReason = reason;
  state.phase = "terminal";
  state.selected.clear();
  state.hiddenSelected.clear();
  hideModal("challenge-modal");
  hideModal("penalty-modal");
  render();
  const loser = 1 - winner;
  $("#result-title").textContent = winner === 0 ? "あなたの勝ち" : "CPUの勝ち";
  $("#result-copy").textContent = reason === "burst"
    ? `${winner === 0 ? "CPU" : "あなた"}をバーストさせました。`
    : reason === "timeout"
      ? (winner === 0 ? "CPU" : "あなた") + "の時間切れです。"
    : `${winner === 0 ? "あなた" : "CPU"}が手札を先に無くしました。`;
  $("#result-score-value").textContent = `${winner === 0 ? "+" : "−"}${state.hands[loser].length}`;
  $("#result-modal").classList.remove("is-hidden");
}

function checkEnd(owner) {
  if (state.hands[owner].length === 0) {
    finish(owner, "empty_hand");
    return true;
  }
  return false;
}

function acceptPlay(play, previous) {
  const previousSuits = previous.current && !allHidden(previous.current) ? visibleSuits(previous.current) : new Set();
  const currentSuits = visibleSuits(play);
  if (previousSuits.size && currentSuits.size && [...previousSuits].every((suit) => currentSuits.has(suit)) && [...currentSuits].every((suit) => previousSuits.has(suit))) {
    state.suitLock = new Set(currentSuits);
  } else if (!previousSuits.size) {
    state.suitLock = new Set();
  }
  state.current = play;
  state.effectiveCurrent = allHidden(play) ? previous.effectiveCurrent : play;
  state.lastPlayer = play.owner;
  state.passes = 0;
  if (play.cardIds.length >= 4) {
    state.revolution = !state.revolution;
    addLog(state.revolution ? "革命が起きました。" : "革命返しで通常に戻りました。");
  }
  if (play.cardIds.some((id, index) => !play.hidden[index] && CARDS[id].rank === JACK_RANK)) {
    state.jBack = !state.jBack;
    addLog(state.jBack ? "Jバックが発動しました。" : "Jバックが解除されました。");
  }
  const hasEight = play.cardIds.some((id, index) => !play.hidden[index] && CARDS[id].rank === EIGHT_RANK);
  if (hasEight) {
    clearField();
    state.lastPlayer = play.owner;
    setTurn(play.owner);
    addLog(`${play.owner === 0 ? "あなた" : "CPU"}の8切り。もう一度出します。`);
  } else {
    setTurn(1 - play.owner);
  }
  state.phase = "play";
  state.pending = null;
  state.pendingBefore = null;
  state.pendingTruth = null;
  if (!checkEnd(play.owner)) continueTurn();
}

function continueTurn() {
  startClock();
  render();
  if (state.phase === "terminal") return;
  if (state.phase === "play" && state.turn === 1) {
    window.clearTimeout(cpuTimer);
    cpuTimer = window.setTimeout(cpuTurn, 620);
  }
}

function applyPlay(play) {
  completeActionClock(play.owner);
  const previous = snapshot();
  const hand = state.hands[play.owner];
  state.hands[play.owner] = hand.filter((id) => !play.cardIds.includes(id));
  state.field.push(...play.cardIds);
  play.cardIds.forEach((id, index) => {
    if (play.hidden[index]) state.fieldHidden.add(id);
  });
  state.current = play;
  state.lastPlayer = play.owner;
  addLog(`${play.owner === 0 ? "あなた" : "CPU"}が ${describePlay(play)} を出しました。`);
  if (play.hidden.some(Boolean)) {
    state.pending = play;
    state.pendingBefore = previous;
    state.pendingTruth = null;
    state.phase = play.owner === 0 ? "challenge-cpu" : "challenge-human";
    setTurn(1 - play.owner);
    render();
    if (play.owner === 0) {
      window.clearTimeout(cpuTimer);
      cpuTimer = window.setTimeout(cpuChallenge, 700);
    } else {
      showChallengeModal();
      startClock();
    }
  } else {
    acceptPlay(play, previous);
  }
}

function enumerateActions(player) {
  const hand = [...state.hands[player]].sort((a, b) => a - b);
  const combos = [];
  for (let size = 1; size <= hand.length; size += 1) {
    for (const combo of combinations(hand, size)) {
      const actual = claimOptions(combo, combo.map(() => false));
      if (size <= 4 || actual.length || size === hand.length) combos.push(combo);
    }
  }
  const actions = [];
  combos.forEach((combo) => {
    const size = combo.length;
    const masks = new Set([0, (1 << size) - 1]);
    if (size <= 4) for (let mask = 0; mask < (1 << size); mask += 1) masks.add(mask);
    else combo.forEach((_, index) => {
      masks.add(1 << index);
      masks.add(((1 << size) - 1) ^ (1 << index));
    });
    masks.forEach((mask) => {
      const hidden = combo.map((_, index) => Boolean(mask & (1 << index)));
      const play = { cardIds: combo, hidden, owner: player };
      if (legalAgainst(state.effectiveCurrent, play)) actions.push(play);
    });
  });
  if (state.current) actions.push({ pass: true, owner: player, cardIds: [], hidden: [] });
  return actions;
}

function oneHotVector(index, width) {
  const values = Array(width).fill(0);
  if (index >= 0 && index < width) values[index] = 1;
  return values;
}

function observationVector(player) {
  const values = [];
  const own = Array(54).fill(0);
  state.hands[player].forEach((id) => { own[id] = 1; });
  values.push(...own);

  const visibleField = Array(54).fill(0);
  state.field.forEach((id) => { visibleField[id] = 1; });
  if (state.current) {
    state.current.cardIds.forEach((id, index) => {
      if (state.current.hidden[index]) visibleField[id] = 0;
    });
  }
  values.push(...visibleField);
  values.push(...oneHotVector(Math.min(state.hands[1 - player].length, 14), 15));
  values.push(...oneHotVector(Math.min(state.field.length, 14), 15));
  values.push(...oneHotVector(state.current ? state.current.hidden.filter(Boolean).length : 0, 15));
  values.push(...oneHotVector(Math.min(state.current ? state.current.cardIds.length : 0, 14), 15));
  values.push(...oneHotVector(visibleRank(state.current) ?? -1, 14));
  values.push(state.revolution ? 1 : 0, state.jBack ? 1 : 0);
  values.push(...[0, 1, 2, 3].map((suit) => state.suitLock.has(suit) ? 1 : 0));
  values.push(state.turn === player ? 1 : 0, state.lastPlayer === player ? 1 : 0);
  const phaseIndex = { play: 0, "challenge-human": 1, "challenge-cpu": 1, penalty: 2, terminal: 3 }[state.phase] ?? 3;
  values.push(...oneHotVector(phaseIndex, 4));
  return values;
}

function actionVector(action) {
  const values = [];
  const selected = Array(54).fill(0);
  action.cardIds.forEach((id) => { selected[id] = 1; });
  values.push(...selected);
  const hidden = Array(54).fill(0);
  action.cardIds.forEach((id, index) => {
    if (action.hidden[index]) hidden[id] = 1;
  });
  values.push(...hidden);
  const kindIndex = { play: 0, pass: 1, doubt: 2, suru: 3, penalty: 4 }[action.pass ? "pass" : "play"] ?? 0;
  values.push(...oneHotVector(kindIndex, 5));
  values.push(...oneHotVector(Math.min(action.cardIds.length, 14), 15));
  const suits = Array(4).fill(0);
  action.cardIds.forEach((id, index) => {
    const card = CARDS[id];
    if (!action.hidden[index] && !card.joker) suits[card.suit] = 1;
  });
  values.push(...suits);
  const reference = Array(54).fill(0);
  if (state.effectiveCurrent) {
    state.effectiveCurrent.cardIds.forEach((id, index) => {
      if (!state.effectiveCurrent.hidden[index]) reference[id] = 1;
    });
  }
  values.push(...reference);
  return values;
}

class WebPolicy {
  constructor(payload) {
    this.layers = payload.layers;
  }

  linear(values, layer) {
    return layer.weight.map((row, index) => row.reduce((sum, weight, column) => sum + weight * values[column], layer.bias[index]));
  }

  forward(input) {
    let values = input;
    this.layers.forEach((layer) => {
      if (layer.type === "linear") values = this.linear(values, layer);
      else if (layer.type === "layernorm") {
        const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
        const variance = values.reduce((sum, value) => sum + ((value - mean) ** 2), 0) / values.length;
        values = values.map((value, index) => ((value - mean) / Math.sqrt(variance + (layer.eps ?? 1e-5))) * layer.weight[index] + layer.bias[index]);
      } else if (layer.type === "gelu") {
        values = values.map((value) => .5 * value * (1 + Math.tanh(Math.sqrt(2 / Math.PI) * (value + .044715 * value ** 3))));
      }
    });
    return values[0];
  }

  scoreAction(action) {
    return this.forward([...observationVector(1), ...actionVector(action)]);
  }

  chooseAction(currentState, actions) {
    if (!actions.length) return null;
    const player = 1;
    const observation = observationVector(player);
    let best = actions[0];
    let bestLogit = -Infinity;
    actions.forEach((action) => {
      const logit = this.forward([...observation, ...actionVector(action)]);
      if (logit > bestLogit) {
        bestLogit = logit;
        best = action;
      }
    });
    return best;
  }
}

function combinations(items, size) {
  const result = [];
  const walk = (start, picked) => {
    if (picked.length === size) {
      result.push([...picked]);
      return;
    }
    for (let index = start; index <= items.length - (size - picked.length); index += 1) {
      picked.push(items[index]);
      walk(index + 1, picked);
      picked.pop();
    }
  };
  walk(0, []);
  return result;
}

function chooseCpuAction(actions) {
  const playable = actions.filter((action) => !action.pass);
  if (!playable.length) return actions.find((action) => action.pass);

  const scored = playable.map((action) => {
    const actual = legalAgainst(state.effectiveCurrent, action, true);
    const hiddenCount = action.hidden.filter(Boolean).length;
    const handSize = state.hands[1].length;
    const claims = claimOptions(action.cardIds, action.cardIds.map(() => false));
    const power = claims.length ? Math.min(...claims.map((claim) => claimPower(claim, state.revolution, state.jBack))) : -1;
    const referenceClaims = state.effectiveCurrent
      ? claimOptions(state.effectiveCurrent.cardIds, state.effectiveCurrent.hidden)
      : [];
    const referencePower = referenceClaims.length
      ? Math.max(...referenceClaims.map((claim) => claimPower(claim, state.revolution, state.jBack)))
      : -1;
    const removesDifficultCards = action.cardIds.reduce((sum, id) => {
      const card = CARDS[id];
      return sum + (card.joker ? 0 : 12 - card.rank);
    }, 0);
    let score = actual ? 180 : -80;
    score += action.cardIds.length * 18;
    score += removesDifficultCards * .55;
    score -= hiddenCount * 24;
    if (!state.effectiveCurrent) score += action.cardIds.length * 10;
    if (state.effectiveCurrent && power >= 0 && referencePower >= 0) {
      score -= Math.max(0, power - referencePower) * .7;
    }
    if (action.cardIds.length === handSize) {
      score += actual ? 1500 : (state.effectiveCurrent ? 260 : -260);
    }
    if (action.cardIds.some((id, index) => !action.hidden[index] && CARDS[id].rank === EIGHT_RANK)) score += 38;
    if (action.cardIds.some((id, index) => !action.hidden[index] && CARDS[id].rank === JACK_RANK)) score += 4;
    return { action, score };
  });

  const pass = actions.find((action) => action.pass);
  const truthful = scored.filter((item) => legalAgainst(state.effectiveCurrent, item.action, true));
  if (!truthful.length && pass) {
    const emergency = scored.filter((item) => item.action.cardIds.length >= 2 || item.action.cardIds.length === state.hands[1].length);
    if (!emergency.length) return pass;
  }

  scored.sort((a, b) => b.score - a.score);
  const bestScore = scored[0].score;
  const shortlist = scored.filter((item) => bestScore - item.score <= 2).slice(0, 8);
  if (webModel && typeof webModel.scoreAction === "function" && shortlist.length > 1) {
    shortlist.forEach((item) => { item.modelScore = webModel.scoreAction(item.action); });
    shortlist.sort((a, b) => b.modelScore - a.modelScore);
  }
  if (shortlist.length) return shortlist[0].action;

  return pass || scored[0].action;
}

function cpuTurn() {
  if (state.phase !== "play" || state.turn !== 1 || state.winner !== null) return;
  const actions = enumerateActions(1);
  const action = chooseCpuAction(actions);
  if (!action) return;
  if (action.pass) {
    completeActionClock(1);
    const leader = state.lastPlayer;
    clearField();
    setTurn(leader === null ? 0 : leader);
    state.phase = "play";
    addLog("CPUがパス。場が流れました。");
    continueTurn();
    return;
  }
  applyPlay(action);
}

function cpuChallenge() {
  if (state.phase !== "challenge-cpu" || !state.pending) return;
  const hiddenCount = state.pending.hidden.filter(Boolean).length;
  // The CPU only sees the public claim and the number of hidden cards here.
  // Looking at the pending card ids would leak the human's private hand.
  const doubtChance = hiddenCount >= state.pending.cardIds.length ? .28 : hiddenCount >= 2 ? .2 : .08;
  const doubt = hiddenCount > 0 && Math.random() < doubtChance;
  if (doubt) {
    addLog("CPUがダウト。裏札を検めます。");
    resolveDoubt(1);
  } else {
    addLog("CPUがスルー。裏札を信じました。");
    resolveSuru();
  }
}

function resolveSuru() {
  if (!state.pending || !state.pendingBefore) return;
  completeActionClock(state.turn);
  const pending = state.pending;
  const before = state.pendingBefore;
  state.pending = null;
  state.pendingBefore = null;
  state.pendingTruth = null;
  acceptPlay(pending, before);
}

function resolveDoubt(challenger) {
  if (!state.pending || !state.pendingBefore) return;
  const pending = state.pending;
  completeActionClock(challenger);
  const truth = legalAgainst(state.pendingBefore.effectiveCurrent, pending, true);
  pending.cardIds.forEach((id, index) => {
    if (pending.hidden[index]) {
      state.fieldHidden.delete(id);
      state.revealed.add(id);
    }
  });
  const success = !truth;
  const selector = success ? challenger : pending.owner;
  const loser = success ? pending.owner : challenger;
  state.pendingTruth = truth;
  state.penaltySelector = selector;
  state.penaltyLoser = loser;
  setTurn(selector);
  state.phase = "reveal";
  addLog(success ? "ダウト成功。ペナルティを選びます。" : "ダウト失敗。ダウト側がペナルティを受けます。");
  render();
  window.setTimeout(() => {
    if (state.phase !== "reveal" || state.pending !== pending) return;
    state.phase = "penalty";
    setTurn(selector);
    state.penaltySelected = new Set();
    render();
    if (selector === 0) {
      showPenaltyModal();
      startClock();
    } else {
      window.clearTimeout(cpuTimer);
      cpuTimer = window.setTimeout(cpuPenalty, 730);
    }
  }, 420);
}

function cpuPenalty() {
  if (state.phase !== "penalty" || state.penaltySelector !== 1) return;
  // The selector can choose zero cards; giving cards away only strengthens the
  // opponent, so the strategic choice is to take no penalty cards.
  resolvePenalty([]);
}

function resolvePenalty(selected) {
  if (state.phase !== "penalty" || state.penaltyLoser === null) return;
  const selector = state.penaltySelector;
  const loser = state.penaltyLoser;
  completeActionClock(selector);
  const pendingOwner = state.pending ? state.pending.owner : null;
  const unique = [...new Set(selected)].filter((id) => state.field.includes(id));
  state.hands[loser].push(...unique);
  addLog(`${selector === 0 ? "あなた" : "CPU"}が${unique.length ? ` ${unique.map(cardLabel).join("・")}` : " 0枚"}をペナルティ札に選びました。`);
  if (state.hands[loser].length >= 11) {
    finish(selector, "burst");
    return;
  }
  const pendingWasTruthful = state.pendingTruth === true;
  const pending = state.pending;
  const pendingBefore = state.pendingBefore;
  if (pendingWasTruthful && pending && pendingBefore) {
    state.penaltySelector = null;
    state.penaltyLoser = null;
    state.penaltySelected = new Set();
    state.pending = null;
    state.pendingBefore = null;
    state.pendingTruth = null;
    acceptPlay(pending, pendingBefore);
    return;
  }
  if (pendingOwner !== null && state.hands[pendingOwner].length === 0) {
    finish(pendingOwner, "empty_hand");
    return;
  }
  clearField();
  state.lastPlayer = selector;
  state.penaltySelector = null;
  state.penaltyLoser = null;
  state.penaltySelected = new Set();
  state.pending = null;
  state.pendingBefore = null;
  state.pendingTruth = null;
  state.phase = "play";
  setTurn(selector);
  continueTurn();
}

function startGame() {
  window.clearTimeout(cpuTimer);
  window.clearInterval(clockTimer);
  document.body.classList.add("is-game-active");
  window.scrollTo(0, 0);
  state = freshState();
  const deck = shuffle(CARDS.map((card) => card.id));
  state.hands[0] = deck.slice(0, 7).sort((a, b) => a - b);
  state.hands[1] = deck.slice(7, 14).sort((a, b) => a - b);
  const starter = Math.random() < .5 ? 0 : 1;
  setTurn(starter);
  addLog(starter === 0 ? "親決定。あなたの手番です。" : "親決定。CPUの手番です。");
  $("#landing-screen").classList.add("is-hidden");
  $("#game-screen").classList.remove("is-hidden");
  hideModal("result-modal");
  render();
  continueTurn();
}

function goHome() {
  window.clearTimeout(cpuTimer);
  stopClock();
  document.body.classList.remove("is-game-active");
  window.scrollTo(0, 0);
  hideModal("result-modal");
  hideModal("challenge-modal");
  hideModal("penalty-modal");
  $("#game-screen").classList.add("is-hidden");
  $("#landing-screen").classList.remove("is-hidden");
}

function toggleSelectedCard(id) {
  if (state.phase !== "play" || state.turn !== 0) return;
  if (state.selected.has(id)) {
    state.selected.delete(id);
    state.hiddenSelected.delete(id);
  } else {
    state.selected.add(id);
  }
  render();
}

function toggleHiddenSelected() {
  if (!state.selected.size) return;
  const allHidden = [...state.selected].every((id) => state.hiddenSelected.has(id));
  state.selected.forEach((id) => {
    if (allHidden) state.hiddenSelected.delete(id);
    else state.hiddenSelected.add(id);
  });
  render();
}

function humanPlay() {
  if (state.phase !== "play" || state.turn !== 0) return;
  if (!state.selected.size) {
    showToast("まず手札を選択してください。");
    return;
  }
  const cardIds = state.hands[0].filter((id) => state.selected.has(id));
  const play = { cardIds, hidden: cardIds.map((id) => state.hiddenSelected.has(id)), owner: 0 };
  if (!legalAgainst(state.effectiveCurrent, play)) {
    showToast("その役は場に出せません。枚数・強さ・スートを確認してください。");
    return;
  }
  state.selected.clear();
  state.hiddenSelected.clear();
  applyPlay(play);
}

function humanPass() {
  if (state.phase !== "play" || state.turn !== 0 || !state.current) return;
  completeActionClock(0);
  const leader = state.lastPlayer;
  clearField();
  setTurn(leader === null ? 1 : leader);
  state.phase = "play";
  addLog("あなたがパス。場が流れました。");
  continueTurn();
}

function showChallengeModal() {
  if (!state.pending) return;
  $("#challenge-copy").textContent = `CPUが ${state.pending.cardIds.length}枚を出しました。裏札を含みます。`;
  $("#challenge-field").innerHTML = state.pending.cardIds.map((id, index) => cardMarkup(id, state.pending.hidden[index])).join("");
  $("#challenge-modal").classList.remove("is-hidden");
}

function showPenaltyModal() {
  const selectorName = state.penaltySelector === 0 ? "あなた" : "CPU";
  $("#penalty-copy").textContent = `${selectorName}がペナルティ執行者です。場札から相手に渡すカードを選んでください。`;
  $("#penalty-field").innerHTML = state.field.map((id) => cardMarkup(id, state.fieldHidden.has(id) && !state.revealed.has(id), {
    selected: state.penaltySelected.has(id), penalty: true, action: "penalty",
  })).join("");
  $("#penalty-action").textContent = state.penaltySelected.size ? `${state.penaltySelected.size}枚を渡す  →` : "0枚を渡す  →";
  $("#penalty-modal").classList.remove("is-hidden");
}

function hideModal(id) {
  $("#" + id).classList.add("is-hidden");
}

function render() {
  renderClock();
  $("#opponent-count").textContent = state.hands[1].length;
  $("#human-count").textContent = state.hands[0].length;
  $("#opponent-hand").innerHTML = state.hands[1]
    .map(() => '<span class="mini-card-back" aria-hidden="true"></span>')
    .join("");
  $("#field-count").textContent = `場札 ${state.field.length}枚`;
  $("#round-label").textContent = `ROUND ${String(state.round).padStart(2, "0")}`;
  const stateLabel = state.revolution ? "革命" : "通常";
  $("#rule-state").textContent = `${stateLabel}${state.jBack ? "・Jバック" : ""}${state.suitLock.size ? `・${[...state.suitLock].map((suit) => SUITS[suit]).join("")}縛り` : ""}`;

  const turnLabel = state.phase === "challenge-human" ? "判断してください"
    : state.phase === "reveal" ? "公開中"
      : state.phase === "penalty" ? "ペナルティ中"
      : state.turn === 0 ? "あなたの番" : "CPUの番";
  $("#turn-indicator").textContent = turnLabel;
  $("#turn-indicator").classList.toggle("opponent-turn", state.turn === 1 || state.phase !== "play");
  $("#opponent-state").textContent = state.phase === "challenge-human" ? "あなたの判断待ち"
    : state.phase === "reveal" ? "公開中"
      : state.turn === 1 ? "思考中" : "待機中";
  $("#human-state").textContent = state.phase === "challenge-human" ? "ダウトする？"
    : state.phase === "reveal" ? "公開中"
      : state.turn === 0 ? "あなたの番" : "待機中";

  const field = $("#field-area");
  if (!state.field.length) {
    field.innerHTML = `<div class="empty-table" id="empty-table"><span>✦</span><b>場は空です</b><small>好きな役で始められます</small></div>`;
  } else {
    field.innerHTML = state.field.map((id) => cardMarkup(id, state.fieldHidden.has(id) && !state.revealed.has(id), {
      revealing: state.phase === "reveal" && state.revealed.has(id),
    })).join("");
  }
  const currentName = state.effectiveCurrent ? describePlay(state.effectiveCurrent) : null;
  $("#table-message").textContent = state.phase === "challenge-human" ? "裏札を検めるか、スルーして進みます。"
    : state.phase === "reveal" ? "裏札を公開しています。"
    : state.phase === "penalty" ? "ペナルティ札を選択しています。"
      : state.phase === "terminal" ? "対戦終了。結果を確認してください。"
        : state.turn === 0 ? (currentName ? `場は ${currentName}。場より強い役を出してください。` : "あなたの手番です。カードを選んでください。")
          : "CPUが次の一手を考えています。";

  $("#hand-area").innerHTML = state.hands[0].map((id) => cardMarkup(id, state.hiddenSelected.has(id), {
    selected: state.selected.has(id),
    action: "hand",
  })).join("");
  $("#selection-count").textContent = `${state.selected.size}枚選択`;
  const allSelectedHidden = state.selected.size > 0
    && [...state.selected].every((id) => state.hiddenSelected.has(id));
  $("#toggle-hidden").innerHTML = allSelectedHidden
    ? "<span>◉</span> 表に戻す"
    : "<span>◌</span> 裏にする";
  $("#preview-text").textContent = state.selected.size
    ? state.hands[0].filter((id) => state.selected.has(id)).map((id) => `${state.hiddenSelected.has(id) ? "裏 " : ""}${cardLabel(id)}`).join("  ")
    : "カードをタップして選択";
  const humanTurn = state.phase === "play" && state.turn === 0;
  $("#toggle-hidden").disabled = !humanTurn || !state.selected.size;
  $("#play-action").disabled = !humanTurn || !state.selected.size;
  $("#pass-action").disabled = !humanTurn || !state.current;

  $("#log-list").innerHTML = state.log.map((entry) => `<div class="log-entry"><span class="log-time">${entry.time}</span><span>${entry.message}</span></div>`).join("");
}

async function loadWebModel() {
  try {
    const response = await fetch("model.json", { cache: "no-store" });
    if (!response.ok) throw new Error("model not found");
    const candidate = await response.json();
    if (!candidate || candidate.format !== "milliondoubt-web-v1") throw new Error("unsupported model");
    webModel = new WebPolicy(candidate);
    const trainedGames = Number(candidate.episodes);
    $("#landing-model-status").textContent = Number.isFinite(trainedGames) && trainedGames > 0
      ? `Web NNモデルを読み込みました（${trainedGames.toLocaleString("ja-JP")}局）`
      : "Web NNモデルを読み込みました";
    $("#model-chip").textContent = "WEB NN + STRATEGY";
  } catch (_error) {
    $("#landing-model-status").textContent = "ルールベースCPU準備完了";
    $("#model-chip").textContent = "CPU STRATEGY";
  }
}

$("#start-game").addEventListener("click", startGame);
$("#restart-game").addEventListener("click", startGame);
$("#result-restart").addEventListener("click", startGame);
$("#back-home").addEventListener("click", goHome);
$("#open-rules").addEventListener("click", () => $("#rules-drawer").classList.remove("is-hidden"));
$("#close-rules").addEventListener("click", () => $("#rules-drawer").classList.add("is-hidden"));
$("#play-action").addEventListener("click", humanPlay);
$("#pass-action").addEventListener("click", humanPass);
$("#toggle-hidden").addEventListener("click", toggleHiddenSelected);
$("#suru-action").addEventListener("click", () => {
  if (state.phase !== "challenge-human") return;
  hideModal("challenge-modal");
  addLog("あなたがスルー。裏札を信じました。");
  resolveSuru();
});
$("#doubt-action").addEventListener("click", () => {
  if (state.phase !== "challenge-human") return;
  hideModal("challenge-modal");
  addLog(`あなたがダウト。${actualClaim(state.pending)}でした。`);
  resolveDoubt(0);
});
$("#penalty-action").addEventListener("click", () => {
  if (state.phase !== "penalty" || state.penaltySelector !== 0) return;
  hideModal("penalty-modal");
  resolvePenalty([...state.penaltySelected]);
});
$("#hand-area").addEventListener("click", (event) => {
  const card = event.target.closest("[data-card-action='hand']");
  if (card) toggleSelectedCard(Number(card.dataset.cardId));
});
$("#penalty-field").addEventListener("click", (event) => {
  const card = event.target.closest("[data-card-action='penalty']");
  if (!card || state.phase !== "penalty" || state.penaltySelector !== 0) return;
  const id = Number(card.dataset.cardId);
  if (state.penaltySelected.has(id)) state.penaltySelected.delete(id);
  else state.penaltySelected.add(id);
  showPenaltyModal();
});
$("#log-toggle").addEventListener("click", () => {
  $("#log-list").classList.toggle("is-collapsed");
  $("#log-toggle").lastElementChild.textContent = $("#log-list").classList.contains("is-collapsed") ? "開く ＋" : "閉じる −";
});

loadWebModel();
