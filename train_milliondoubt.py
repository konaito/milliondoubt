#!/usr/bin/env python3
"""Self-play trainer and rules smoke test for Million Doubt.

The policy only receives the player's hand and public information.  The
simulator keeps the real cards privately so that hidden information cannot
leak into the neural network input.

This is an executable research baseline, not a claim that a few local
episodes produce a solved game.  For a genuinely strong agent, train for a
large number of self-play episodes, keep checkpoints, and evaluate against
older checkpoints and human game records.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except ImportError:  # The smoke test intentionally works without PyTorch.
    torch = None
    nn = None
    F = None


RANK_NAMES = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2")
SUIT_NAMES = ("S", "H", "D", "C")
NUM_CARDS = 54
JOKER_RANK = 13
EIGHT_RANK = 5
JACK_RANK = 8
MAX_HAND = 14


@dataclass(frozen=True)
class Card:
    card_id: int
    rank: int
    suit: int

    @property
    def is_joker(self) -> bool:
        return self.rank == JOKER_RANK

    def short(self) -> str:
        if self.is_joker:
            return "Jo"
        return RANK_NAMES[self.rank] + SUIT_NAMES[self.suit]


DECK: Tuple[Card, ...] = tuple(
    [Card(rank * 4 + suit, rank, suit) for rank in range(13) for suit in range(4)]
    + [Card(52, JOKER_RANK, -1), Card(53, JOKER_RANK, -1)]
)
CARD_BY_ID = {card.card_id: card for card in DECK}


@dataclass(frozen=True)
class Claim:
    kind: str  # single, group, straight
    high: int
    start: int = -1
    length: int = 1
    suit: int = -1

    def key(self) -> Tuple[object, ...]:
        return (self.kind, self.high, self.start, self.length, self.suit)


@dataclass(frozen=True)
class Play:
    card_ids: Tuple[int, ...]
    hidden: Tuple[bool, ...]
    owner: int

    def cards(self) -> Tuple[Card, ...]:
        return tuple(CARD_BY_ID[card_id] for card_id in self.card_ids)

    def all_hidden(self) -> bool:
        return bool(self.hidden) and all(self.hidden)

    def visible_cards(self) -> Tuple[Card, ...]:
        return tuple(card for card, is_hidden in zip(self.cards(), self.hidden) if not is_hidden)

    def visible_suits(self) -> Set[int]:
        return {card.suit for card in self.visible_cards() if not card.is_joker}


@dataclass(frozen=True)
class Choice:
    kind: str  # play, pass, doubt, suru, penalty
    card_ids: Tuple[int, ...] = ()
    hidden: Tuple[bool, ...] = ()

    @staticmethod
    def play(card_ids: Sequence[int], hidden: Sequence[bool]) -> "Choice":
        return Choice("play", tuple(card_ids), tuple(hidden))

    @staticmethod
    def penalty(card_ids: Iterable[int]) -> "Choice":
        return Choice("penalty", tuple(sorted(card_ids)), ())


def card_set(cards: Sequence[int]) -> Tuple[Card, ...]:
    return tuple(CARD_BY_ID[card_id] for card_id in cards)


def reversed_order(revolution: bool, j_back: bool) -> bool:
    """The baseline treats J-back as a temporary toggle of revolution order."""
    return bool(revolution) ^ bool(j_back)


def rank_strength(rank: int, revolution: bool, j_back: bool) -> int:
    if rank == JOKER_RANK:
        return 13
    return 12 - rank if reversed_order(revolution, j_back) else rank


def claim_power(claim: Claim, revolution: bool, j_back: bool) -> int:
    if claim.kind != "straight":
        return rank_strength(claim.high, revolution, j_back)
    ranks = range(claim.start, claim.start + claim.length)
    return max(rank_strength(rank, revolution, j_back) for rank in ranks)


def _dedupe_claims(claims: Iterable[Claim]) -> Tuple[Claim, ...]:
    seen: Set[Tuple[object, ...]] = set()
    result: List[Claim] = []
    for claim in claims:
        if claim.key() not in seen:
            seen.add(claim.key())
            result.append(claim)
    return tuple(result)


def claim_options(cards: Sequence[int], hidden: Optional[Sequence[bool]] = None) -> Tuple[Claim, ...]:
    """Return claims compatible with visible cards.

    A hidden card is an unknown card for public interpretation. For an actual
    truth check, call this with every hidden flag set to False (all cards
    fixed). A fully hidden set intentionally has many possible claims,
    matching the public rule that it supplies no rank or suit anchor.
    """
    if not cards:
        return ()
    if hidden is None:
        hidden = tuple(False for _ in cards)
    fixed = [card for card, is_hidden in zip(card_set(cards), hidden) if not is_hidden]
    unknown = len(cards) - len(fixed)
    result: List[Claim] = []

    if len(cards) == 1:
        if not fixed:
            result.extend(Claim("single", rank) for rank in range(14))
        elif fixed[0].is_joker:
            result.append(Claim("single", JOKER_RANK))
        else:
            result.append(Claim("single", fixed[0].rank))

    if 2 <= len(cards) <= 4:
        fixed_ranks = {card.rank for card in fixed if not card.is_joker}
        if len(fixed_ranks) <= 1:
            candidate_ranks = fixed_ranks or set(range(13))
            for rank in sorted(candidate_ranks):
                result.append(Claim("group", rank, length=len(cards)))
        if not unknown and len(cards) == 2 and all(card.is_joker for card in fixed):
            result.append(Claim("group", JOKER_RANK, length=2))

    if len(cards) >= 3:
        for suit in range(4):
            for start in range(14 - len(cards)):
                end = start + len(cards)
                fixed_non_jokers = [card for card in fixed if not card.is_joker]
                if all(card.suit == suit and start <= card.rank < end for card in fixed_non_jokers):
                    ranks = [card.rank for card in fixed_non_jokers]
                    if len(ranks) == len(set(ranks)):
                        joker_count = sum(1 for card in fixed if card.is_joker)
                        if unknown or joker_count <= 2:
                            result.append(Claim("straight", end - 1, start, len(cards), suit))

    return _dedupe_claims(result)


def legal_against(
    current: Optional[Play],
    action: Play,
    revolution: bool,
    j_back: bool,
    suit_lock: Set[int],
    actual: bool = False,
) -> bool:
    """Check whether a play can be presented, or is true, against the field."""
    if current is None or current.all_hidden():
        return bool(claim_options(action.card_ids, tuple(False for _ in action.card_ids) if actual else action.hidden))

    reference_claims = claim_options(current.card_ids, current.hidden)
    action_hidden = tuple(False for _ in action.card_ids) if actual else action.hidden
    action_claims = claim_options(action.card_ids, action_hidden)
    if not reference_claims or not action_claims:
        return False

    if suit_lock:
        suit_hidden = tuple(False for _ in action.card_ids) if actual else action.hidden
        visible_suits = {card.suit for card, is_hidden in zip(action.cards(), suit_hidden) if not is_hidden and not card.is_joker}
        if visible_suits and not visible_suits.issubset(suit_lock):
            return False
        if not visible_suits and actual:
            return False

    for reference in reference_claims:
        for candidate in action_claims:
            if reference.kind != candidate.kind:
                continue
            if reference.length != candidate.length:
                continue
            if claim_power(candidate, revolution, j_back) > claim_power(reference, revolution, j_back):
                return True
    return False


def visible_rank(card_ids: Sequence[int], hidden: Sequence[bool]) -> Optional[int]:
    ranks = [card.rank for card, is_hidden in zip(card_set(card_ids), hidden) if not is_hidden and not card.is_joker]
    return max(ranks) if ranks else None


class GameState:
    """Two-player Million Doubt state with private hands and public field."""

    def __init__(self, hands: List[List[int]], first_player: int):
        self.hands = [list(hands[0]), list(hands[1])]
        self.turn = first_player
        self.current: Optional[Play] = None
        self.field_ids: List[int] = []
        self.last_player: Optional[int] = None
        self.passes = 0
        self.revolution = False
        self.j_back = False
        self.suit_lock: Set[int] = set()
        self.phase = "play"
        self.pending: Optional[Play] = None
        self.effective_current: Optional[Play] = None
        self.pending_before: Optional[Tuple[Optional[Play], Optional[Play], bool, bool, Set[int], List[int]]] = None
        self.pending_truth: Optional[bool] = None
        self.penalty_selector: Optional[int] = None
        self.penalty_loser: Optional[int] = None
        self.winner: Optional[int] = None
        self.end_reason: Optional[str] = None

    @classmethod
    def deal(cls, rng: random.Random) -> "GameState":
        ids = list(range(NUM_CARDS))
        rng.shuffle(ids)
        return cls([ids[:7], ids[7:14]], rng.randrange(2))

    def terminal(self) -> bool:
        return self.phase == "terminal"

    def _snapshot(self) -> Tuple[Optional[Play], Optional[Play], bool, bool, Set[int], List[int]]:
        return (
            self.current,
            self.effective_current,
            self.revolution,
            self.j_back,
            set(self.suit_lock),
            list(self.field_ids),
        )

    def _restore_view(self, snapshot: Tuple[Optional[Play], Optional[Play], bool, bool, Set[int], List[int]]) -> None:
        (
            self.current,
            self.effective_current,
            self.revolution,
            self.j_back,
            self.suit_lock,
            self.field_ids,
        ) = snapshot

    def _clear_field(self) -> None:
        self.current = None
        self.effective_current = None
        self.field_ids = []
        self.passes = 0
        self.j_back = False
        self.suit_lock = set()

    def _finish(self, winner: int, reason: str) -> None:
        self.winner = winner
        self.end_reason = reason
        self.phase = "terminal"

    def _check_end(self, owner: int) -> bool:
        if len(self.hands[owner]) == 0:
            self._finish(owner, "empty_hand")
            return True
        return False

    def _accept(
        self,
        play: Play,
        previous: Tuple[Optional[Play], Optional[Play], bool, bool, Set[int], List[int]],
    ) -> None:
        previous_play, previous_effective, previous_revolution, previous_j_back, previous_lock, _ = previous
        previous_suits = previous_play.visible_suits() if previous_play and not previous_play.all_hidden() else set()
        current_suits = play.visible_suits()
        if previous_suits and current_suits and previous_suits == current_suits:
            self.suit_lock = set(current_suits)
        elif not previous_suits:
            self.suit_lock = set()

        self.current = play
        self.effective_current = previous_effective if play.all_hidden() else play
        self.last_player = play.owner
        self.passes = 0
        if len(play.card_ids) >= 4:
            self.revolution = not self.revolution
        if any(card.rank == JACK_RANK for card in play.visible_cards()):
            self.j_back = not self.j_back

        if any(card.rank == EIGHT_RANK for card in play.visible_cards()):
            self._clear_field()
            self.last_player = play.owner
            self.turn = play.owner
        else:
            self.turn = 1 - play.owner
        if not self._check_end(play.owner):
            self.phase = "play"

    def _begin_penalty(self, challenger: int, truth: bool) -> None:
        assert self.pending is not None
        owner = self.pending.owner
        challenge_success = not truth
        winner = challenger if challenge_success else owner
        loser = owner if challenge_success else challenger
        self.penalty_selector = winner
        self.penalty_loser = loser
        self.pending_truth = truth
        self.turn = winner
        self.phase = "penalty"

    def _truth_of_pending(self) -> bool:
        assert self.pending is not None and self.pending_before is not None
        _previous, previous_effective, revolution, j_back, lock, _field = self.pending_before
        return legal_against(previous_effective, self.pending, revolution, j_back, lock, actual=True)

    def apply(self, choice: Choice) -> None:
        if self.terminal():
            raise ValueError("game is already over")

        if self.phase == "play":
            self._apply_play_phase(choice)
        elif self.phase == "challenge":
            if choice.kind not in ("doubt", "suru"):
                raise ValueError("challenge phase accepts doubt or suru")
            assert self.pending is not None and self.pending_before is not None
            if choice.kind == "suru":
                pending = self.pending
                before = self.pending_before
                self.pending = None
                self.pending_before = None
                self.pending_truth = None
                self._accept(pending, before)
            else:
                challenger = self.turn
                truth = self._truth_of_pending()
                self._begin_penalty(challenger, truth)
        elif self.phase == "penalty":
            if choice.kind != "penalty" or self.penalty_selector != self.turn:
                raise ValueError("penalty phase accepts a penalty selection")
            selected = set(choice.card_ids)
            field_set = set(self.field_ids)
            if not selected.issubset(field_set):
                raise ValueError("penalty cards must come from the field")
            assert self.penalty_loser is not None and self.penalty_selector is not None
            pending_owner = self.pending.owner if self.pending is not None else None
            self.hands[self.penalty_loser].extend(sorted(selected))
            winner = self.penalty_selector
            if len(self.hands[self.penalty_loser]) >= 11:
                self._finish(winner, "burst")
                return
            if self.pending_truth is True and self.pending is not None and self.pending_before is not None:
                pending = self.pending
                before = self.pending_before
                self.penalty_selector = None
                self.penalty_loser = None
                self.pending = None
                self.pending_before = None
                self.pending_truth = None
                self._accept(pending, before)
                return
            if pending_owner is not None and len(self.hands[pending_owner]) == 0:
                # A truthful final play can be challenged before its winner is
                # confirmed. Resolve the empty-hand win after the penalty UI.
                self._finish(pending_owner, "empty_hand")
                return
            self._clear_field()
            self.last_player = winner
            self.turn = winner
            self.penalty_selector = None
            self.penalty_loser = None
            self.pending = None
            self.pending_before = None
            self.pending_truth = None
            self.phase = "play"
        else:
            raise ValueError("unknown game phase")

    def _apply_play_phase(self, choice: Choice) -> None:
        player = self.turn
        if choice.kind == "pass":
            if self.current is None:
                raise ValueError("cannot pass as the parent")
            self.passes += 1
            if self.passes >= 1:  # exactly one opponent in the standard game
                leader = self.last_player
                self._clear_field()
                assert leader is not None
                self.turn = leader
            else:
                self.turn = 1 - player
            return
        if choice.kind != "play":
            raise ValueError("play phase accepts play or pass")
        if len(choice.card_ids) == 0 or len(choice.card_ids) != len(choice.hidden):
            raise ValueError("a play needs cards and one visibility flag per card")
        if len(set(choice.card_ids)) != len(choice.card_ids):
            raise ValueError("a card cannot be played twice")
        if any(card_id not in self.hands[player] for card_id in choice.card_ids):
            raise ValueError("played card is not in the player's hand")
        proposed = Play(tuple(choice.card_ids), tuple(choice.hidden), player)
        if not legal_against(self.effective_current, proposed, self.revolution, self.j_back, self.suit_lock):
            raise ValueError("illegal presented play")

        before = self._snapshot()
        self.hands[player] = [card_id for card_id in self.hands[player] if card_id not in choice.card_ids]
        self.field_ids.extend(choice.card_ids)
        self.current = proposed
        self.last_player = player
        if any(choice.hidden):
            self.pending = proposed
            self.pending_before = before
            self.pending_truth = None
            self.phase = "challenge"
            self.turn = 1 - player
        else:
            self._accept(proposed, before)


def initial_actions(state: GameState, player: int, rng: random.Random, max_actions: int = 512) -> List[Choice]:
    """Generate presented plays, including useful bluff patterns."""
    hand = sorted(state.hands[player])
    candidates: List[Choice] = []
    all_combos: List[Tuple[int, ...]] = []
    max_k = len(hand)
    for size in range(1, max_k + 1):
        for combo in itertools.combinations(hand, size):
            actual = claim_options(combo, tuple(False for _ in combo))
            if size <= 4 or actual or size == len(hand):
                all_combos.append(combo)

    for combo in all_combos:
        size = len(combo)
        masks: Set[int] = {0, (1 << size) - 1}
        if size <= 4:
            masks.update(range(1 << size))
        else:
            for i in range(size):
                masks.add(1 << i)
                masks.add(((1 << size) - 1) ^ (1 << i))
        for mask in sorted(masks):
            hidden = tuple(bool(mask & (1 << i)) for i in range(size))
            proposed = Play(combo, hidden, player)
            if legal_against(state.effective_current, proposed, state.revolution, state.j_back, state.suit_lock):
                candidates.append(Choice.play(combo, hidden))

    dedup: Dict[Tuple[object, ...], Choice] = {}
    for choice in candidates:
        dedup[(choice.kind, choice.card_ids, choice.hidden)] = choice
    candidates = list(dedup.values())
    if len(candidates) > max_actions:
        # Keep every one-card move and every all-visible structurally valid move,
        # then sample the rest so long straight and bluff options remain present.
        keep: List[Choice] = [c for c in candidates if len(c.card_ids) == 1]
        rest = [c for c in candidates if c not in keep]
        remaining = max(0, max_actions - len(keep))
        if len(rest) > remaining:
            rest = rng.sample(rest, remaining)
        candidates = keep + rest
    candidates.sort(key=lambda c: (len(c.card_ids), c.card_ids, c.hidden))
    if state.current is not None:
        candidates.append(Choice("pass"))
    return candidates


def challenge_actions() -> List[Choice]:
    return [Choice("doubt"), Choice("suru")]


def penalty_actions(state: GameState, rng: random.Random, max_actions: int = 96) -> List[Choice]:
    field = sorted(set(state.field_ids))
    choices: Dict[Tuple[int, ...], Choice] = {(): Choice.penalty(()) , tuple(field): Choice.penalty(field)}
    for card_id in field:
        choices[(card_id,)] = Choice.penalty((card_id,))
    for pair in itertools.combinations(field, 2):
        choices[pair] = Choice.penalty(pair)
    while len(choices) < min(max_actions, 2 ** min(len(field), 12)):
        subset = tuple(card_id for card_id in field if rng.random() < 0.5)
        choices[subset] = Choice.penalty(subset)
    result = list(choices.values())
    if len(result) > max_actions:
        result = rng.sample(result, max_actions)
    return result


def observation(state: GameState, player: int) -> np.ndarray:
    """Public-information observation from one player's perspective."""
    values: List[float] = []

    own = np.zeros(NUM_CARDS, dtype=np.float32)
    own[state.hands[player]] = 1.0
    values.extend(own.tolist())

    visible_field = np.zeros(NUM_CARDS, dtype=np.float32)
    hidden_count = 0
    for card_id in state.field_ids:
        # Only the latest public set has visibility metadata; older field cards
        # are public after being accepted and therefore treated as visible.
        visible_field[card_id] = 1.0
    if state.current is not None:
        hidden_count = sum(state.current.hidden)
        for card_id, is_hidden in zip(state.current.card_ids, state.current.hidden):
            if is_hidden:
                visible_field[card_id] = 0.0
    values.extend(visible_field.tolist())

    def one_hot(index: int, width: int) -> None:
        block = [0.0] * width
        if 0 <= index < width:
            block[index] = 1.0
        values.extend(block)

    one_hot(min(len(state.hands[1 - player]), MAX_HAND), MAX_HAND + 1)
    one_hot(min(len(state.field_ids), MAX_HAND), MAX_HAND + 1)
    one_hot(hidden_count, MAX_HAND + 1)
    one_hot(min(len(state.current.card_ids) if state.current else 0, MAX_HAND), MAX_HAND + 1)
    current_view = state.effective_current or state.current
    current_rank = visible_rank(current_view.card_ids, current_view.hidden) if current_view else None
    one_hot(current_rank if current_rank is not None else -1, 14)
    values.extend([float(state.revolution), float(state.j_back)])
    lock = [0.0] * 4
    for suit in state.suit_lock:
        lock[suit] = 1.0
    values.extend(lock)
    values.extend([float(state.turn == player), float(state.last_player == player)])
    phase = {"play": 0, "challenge": 1, "penalty": 2, "terminal": 3}.get(state.phase, 3)
    one_hot(phase, 4)
    return np.asarray(values, dtype=np.float32)


OBS_SIZE = len(observation(GameState([list(range(7)), list(range(7, 14))], 0), 0))


def action_features(state: GameState, choice: Choice, player: int) -> np.ndarray:
    values: List[float] = []
    selected = np.zeros(NUM_CARDS, dtype=np.float32)
    selected[list(choice.card_ids)] = 1.0 if choice.card_ids else 0.0
    values.extend(selected.tolist())
    hidden = np.zeros(NUM_CARDS, dtype=np.float32)
    for card_id, is_hidden in zip(choice.card_ids, choice.hidden):
        if is_hidden:
            hidden[card_id] = 1.0
    values.extend(hidden.tolist())

    kinds = {"play": 0, "pass": 1, "doubt": 2, "suru": 3, "penalty": 4}
    kind_block = [0.0] * 5
    kind_block[kinds[choice.kind]] = 1.0
    values.extend(kind_block)

    count_block = [0.0] * (MAX_HAND + 1)
    count_block[min(len(choice.card_ids), MAX_HAND)] = 1.0
    values.extend(count_block)

    suit_block = [0.0] * 4
    for card, is_hidden in zip(card_set(choice.card_ids), choice.hidden):
        if not is_hidden and not card.is_joker:
            suit_block[card.suit] = 1.0
    values.extend(suit_block)

    ref_block = [0.0] * NUM_CARDS
    if state.effective_current is not None:
        for card_id, is_hidden in zip(state.effective_current.card_ids, state.effective_current.hidden):
            if not is_hidden:
                ref_block[card_id] = 1.0
    values.extend(ref_block)
    return np.asarray(values, dtype=np.float32)


ACTION_SIZE = len(action_features(GameState([list(range(7)), list(range(7, 14))], 0), Choice("pass"), 0))


if nn is not None:
    class PolicyValueNet(nn.Module):
        def __init__(self, obs_size: int = OBS_SIZE, action_size: int = ACTION_SIZE) -> None:
            super().__init__()
            width = 384
            self.trunk = nn.Sequential(
                nn.Linear(obs_size + action_size, width),
                nn.LayerNorm(width),
                nn.GELU(),
                nn.Linear(width, width),
                nn.LayerNorm(width),
                nn.GELU(),
                nn.Linear(width, 192),
                nn.GELU(),
            )
            self.logit = nn.Linear(192, 1)
            self.value = nn.Linear(192, 1)

        def forward(self, obs: torch.Tensor, actions: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
            if obs.dim() == 1:
                obs = obs.unsqueeze(0)
            if actions.dim() == 3:
                actions = actions.squeeze(0)
            if obs.shape[0] == 1 and actions.shape[0] > 1:
                obs = obs.expand(actions.shape[0], -1)
            x = torch.cat([obs, actions], dim=-1)
            h = self.trunk(x)
            return self.logit(h).squeeze(-1), self.value(h).squeeze(-1)


@dataclass
class Decision:
    player: int
    obs: np.ndarray
    action_matrix: np.ndarray
    selected_index: int
    teacher_index: int


def strategic_score(state: GameState, choice: Choice, player: int) -> float:
    """Score a legal choice with a small, information-safe strategy teacher.

    The teacher may inspect the acting player's hand while choosing a play, but
    it only sees public information during a challenge. This gives training a
    stable signal without leaking an opponent's hidden cards into observations.
    """
    if state.phase == "challenge":
        hidden_count = sum(state.pending.hidden) if state.pending is not None else 0
        if choice.kind == "doubt":
            return 7.0 if hidden_count == 1 else 14.0
        return 9.0

    if state.phase == "penalty":
        return 24.0 if not choice.card_ids else 24.0 - len(choice.card_ids) * 2.0

    if choice.kind == "pass":
        return -50.0

    play = Play(choice.card_ids, choice.hidden, player)
    truthful = legal_against(
        state.effective_current,
        Play(choice.card_ids, tuple(False for _ in choice.card_ids), player),
        state.revolution,
        state.j_back,
        state.suit_lock,
        actual=True,
    )
    claims = claim_options(choice.card_ids, tuple(False for _ in choice.card_ids))
    power = min((claim_power(claim, state.revolution, state.j_back) for claim in claims), default=-1)
    reference_claims = (
        claim_options(state.effective_current.card_ids, state.effective_current.hidden)
        if state.effective_current is not None
        else ()
    )
    reference_power = max(
        (claim_power(claim, state.revolution, state.j_back) for claim in reference_claims),
        default=-1,
    )
    hidden_count = sum(choice.hidden)
    difficulty = sum(0 if card.is_joker else 12 - card.rank for card in play.cards())
    score = 180.0 if truthful else -80.0
    score += len(choice.card_ids) * 18.0
    score += difficulty * 0.55
    score -= hidden_count * 24.0
    if state.effective_current is None:
        score += len(choice.card_ids) * 10.0
    elif power >= 0 and reference_power >= 0:
        score -= max(0, power - reference_power) * 0.7
    if len(choice.card_ids) == len(state.hands[player]):
        score += 1500.0 if truthful else 260.0
    if any(not hidden and card.rank == EIGHT_RANK for card, hidden in zip(play.cards(), play.hidden)):
        score += 38.0
    if any(not hidden and card.rank == JACK_RANK for card, hidden in zip(play.cards(), play.hidden)):
        score += 4.0
    return score


def strategic_choice_index(state: GameState, choices: Sequence[Choice], player: int) -> int:
    if not choices:
        raise ValueError("no choices available")
    scores = [strategic_score(state, choice, player) for choice in choices]
    truthful = [
        index for index, choice in enumerate(choices)
        if choice.kind == "play" and legal_against(
            state.effective_current,
            Play(choice.card_ids, tuple(False for _ in choice.card_ids), player),
            state.revolution,
            state.j_back,
            state.suit_lock,
            actual=True,
        )
    ]
    if not truthful:
        pass_indices = [index for index, choice in enumerate(choices) if choice.kind == "pass"]
        emergency = [
            index for index, choice in enumerate(choices)
            if choice.kind == "play"
            and (len(choice.card_ids) >= 2 or len(choice.card_ids) == len(state.hands[player]))
        ]
        if pass_indices and not emergency:
            return pass_indices[0]
        if emergency:
            return max(emergency, key=lambda index: scores[index])
    return max(range(len(choices)), key=lambda index: scores[index])


def choose_index(
    model: Optional["PolicyValueNet"],
    state: GameState,
    player: int,
    choices: List[Choice],
    device: Optional["torch.device"],
    rng: random.Random,
    temperature: float,
) -> Tuple[int, Optional[np.ndarray], Optional[np.ndarray]]:
    if not choices:
        raise ValueError("no choices available")
    obs = observation(state, player)
    matrix = np.stack([action_features(state, choice, player) for choice in choices])
    if model is None:
        return rng.randrange(len(choices)), obs, matrix
    assert device is not None
    with torch.no_grad():
        obs_t = torch.from_numpy(obs).to(device)
        action_t = torch.from_numpy(matrix).to(device)
        logits, _values = model(obs_t, action_t)
        probs = torch.softmax(logits / max(temperature, 1e-4), dim=0).cpu().numpy()
    probs = probs / probs.sum()
    index = int(rng.choices(range(len(choices)), weights=probs.tolist(), k=1)[0])
    return index, obs, matrix


def run_episode(
    model: Optional["PolicyValueNet"],
    rng: random.Random,
    device: Optional["torch.device"],
    temperature: float = 1.0,
    max_steps: int = 256,
    record: bool = False,
    teacher_probability: float = 0.7,
) -> Tuple[GameState, List[Decision]]:
    state = GameState.deal(rng)
    decisions: List[Decision] = []
    for _step in range(max_steps):
        if state.terminal():
            break
        player = state.turn
        if state.phase == "play":
            choices = initial_actions(state, player, rng)
        elif state.phase == "challenge":
            choices = challenge_actions()
        elif state.phase == "penalty":
            choices = penalty_actions(state, rng)
        else:
            break
        teacher_index = strategic_choice_index(state, choices, player)
        index, obs, matrix = choose_index(model, state, player, choices, device, rng, temperature)
        if model is not None and rng.random() < teacher_probability:
            index = teacher_index
        if record and obs is not None and matrix is not None:
            decisions.append(Decision(player, obs, matrix, index, teacher_index))
        state.apply(choices[index])
    if not state.terminal():
        # A safety cutoff is recorded as a loss for the player with more cards.
        loser = 0 if len(state.hands[0]) >= len(state.hands[1]) else 1
        state._finish(1 - loser, "step_limit")
    return state, decisions


def terminal_rewards(state: GameState) -> Tuple[float, float]:
    assert state.winner is not None
    loser = 1 - state.winner
    if state.end_reason == "burst":
        magnitude = len(state.hands[loser])
    else:
        magnitude = len(state.hands[loser])
    return (float(magnitude) if state.winner == 0 else -float(magnitude),
            float(magnitude) if state.winner == 1 else -float(magnitude))


class ProgressStore:
    """Write a small atomic status snapshot that can be watched over SSH."""

    def __init__(self, total_episodes: int, device: str, path: Path) -> None:
        self.path = path
        self.data: Dict[str, object] = {
            "status": "idle",
            "total_episodes": total_episodes,
            "episodes_completed": 0,
            "device": device,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def update(self, **fields: object) -> None:
        self.data.update(fields)
        completed = int(self.data.get("episodes_completed", 0) or 0)
        total = max(1, int(self.data.get("total_episodes", 1) or 1))
        self.data["progress_percent"] = round(min(100.0, completed * 100.0 / total), 2)
        self.data["updated_at"] = time.time()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def select_device(requested: str) -> "torch.device":
    if torch is None:
        raise RuntimeError("PyTorch is required for training; run: python3 -m pip install -r requirements.txt")
    if requested == "cpu":
        return torch.device("cpu")
    if requested == "mps" and torch.backends.mps.is_available():
        return torch.device("mps")
    if requested == "cuda" and torch.cuda.is_available():
        return torch.device("cuda")
    if requested == "auto":
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
    return torch.device("cpu")


def train(args: argparse.Namespace) -> None:
    if torch is None or nn is None:
        raise RuntimeError("PyTorch is not installed. Use requirements.txt on the Incertotech Mac.")
    device = select_device(args.device)
    rng = random.Random(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    model = PolicyValueNet().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-5)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = Path(args.status_file)
    progress = ProgressStore(args.episodes, str(device), status_path)
    progress.update(status="running", checkpoint=str(out_path))
    wins = [0, 0]

    update_count = max(1, math.ceil(args.episodes / args.batch_episodes))
    for update in range(update_count):
        batch = min(args.batch_episodes, args.episodes - update * args.batch_episodes)
        if batch <= 0:
            break
        episodes: List[Tuple[GameState, List[Decision]]] = []
        for _ in range(batch):
            episodes.append(run_episode(
                model,
                rng,
                device,
                args.temperature,
                args.max_steps,
                record=True,
                teacher_probability=args.teacher_probability,
            ))
            progress.update(
                update=update + 1,
                episodes_completed=update * args.batch_episodes + len(episodes),
            )

        policy_losses: List[torch.Tensor] = []
        value_losses: List[torch.Tensor] = []
        entropies: List[torch.Tensor] = []
        teacher_losses: List[torch.Tensor] = []
        rewards: List[float] = []
        for state, decisions in episodes:
            rewards_pair = terminal_rewards(state)
            rewards.extend(rewards_pair)
            wins[state.winner if state.winner is not None else 0] += 1
            for decision in decisions:
                obs_t = torch.from_numpy(decision.obs).to(device)
                action_t = torch.from_numpy(decision.action_matrix).to(device)
                logits, values = model(obs_t, action_t)
                distribution = torch.distributions.Categorical(logits=logits / max(args.temperature, 1e-4))
                log_prob = distribution.log_prob(torch.tensor(decision.selected_index, device=device))
                target = torch.tensor(rewards_pair[decision.player], dtype=torch.float32, device=device)
                advantage = target - values[decision.selected_index]
                policy_losses.append(-log_prob * advantage.detach())
                value_losses.append(F.smooth_l1_loss(values[decision.selected_index], target))
                entropies.append(distribution.entropy())
                teacher_losses.append(F.cross_entropy(
                    logits.unsqueeze(0),
                    torch.tensor([decision.teacher_index], dtype=torch.long, device=device),
                ))

        if not policy_losses:
            continue
        loss = (
            torch.stack(policy_losses).mean()
            + args.value_weight * torch.stack(value_losses).mean()
            - args.entropy_weight * torch.stack(entropies).mean()
            + args.teacher_weight * torch.stack(teacher_losses).mean()
        )
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()

        completed = min(args.episodes, (update + 1) * args.batch_episodes)
        winner_rewards = [abs(reward) for reward in rewards]
        mean_winner_reward = float(np.mean(winner_rewards)) if winner_rewards else 0.0
        print(
            json.dumps(
                {
                    "update": update + 1,
                    "episodes": completed,
                    "device": str(device),
                    "loss": float(loss.detach().cpu()),
                    "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
                    "teacher_weight": args.teacher_weight,
                    "wins": wins,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        torch.save(
            {
                "model": model.state_dict(),
                "obs_size": OBS_SIZE,
                "action_size": ACTION_SIZE,
                "episodes": completed,
                "seed": args.seed,
                "rules": "milliondoubt-rulebook-2026-09-12",
                "strategy": "teacher-guided-self-play-v2",
            },
            out_path,
        )
        progress.update(
            status="running",
            update=update + 1,
            episodes_completed=completed,
            last_loss=float(loss.detach().cpu()),
            mean_winner_reward=mean_winner_reward,
            wins=wins,
            checkpoint=str(out_path),
        )

    progress.update(
        status="completed",
        episodes_completed=args.episodes,
        wins=wins,
        checkpoint=str(out_path),
    )


def smoke_test() -> None:
    rng = random.Random(7)
    # The official examples in compact executable form.
    current = Play((CARD_BY_ID[40].card_id,), (False,), 0)  # K♠
    a = Play((CARD_BY_ID[44].card_id,), (False,), 1)  # A♠
    three_bluff = Play((CARD_BY_ID[0].card_id,), (True,), 1)  # 3♠ hidden
    assert legal_against(current, a, False, False, set())
    assert legal_against(current, three_bluff, False, False, set())
    assert not legal_against(current, three_bluff, False, False, set(), actual=True)

    straight = (0, 4, 8)  # 3S, 4S, 5S
    assert any(claim.kind == "straight" for claim in claim_options(straight, (False, False, False)))
    assert any(claim.kind == "group" for claim in claim_options((0, 1, 2), (False, False, True)))

    state = GameState([[0, 1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12, 13]], 0)
    action = Choice.play((0,), (True,))
    state.apply(action)
    assert state.phase == "challenge"
    state.apply(Choice("doubt"))
    assert state.phase == "penalty"
    state.apply(Choice.penalty(()))
    assert state.phase == "play"
    assert len(state.hands[0]) == 6 and len(state.hands[1]) == 7

    # A fully hidden set is a real field card set, but it does not replace the
    # previous set used for the next comparison.
    hidden_state = GameState([[40, 0], [4, 5, 6]], 0)
    hidden_state.apply(Choice.play((40,), (False,)))
    hidden_state.apply(Choice.play((4,), (True,)))
    hidden_state.apply(Choice("suru"))
    assert hidden_state.current is not None and hidden_state.current.all_hidden()
    assert hidden_state.effective_current is not None
    assert hidden_state.effective_current.card_ids == (40,)
    try:
        hidden_state.apply(Choice.play((0,), (False,)))
    except ValueError:
        pass
    else:
        raise AssertionError("a face-up 3 must not beat the preserved K reference")

    # A truthful four-card play that was doubted still establishes a revolution
    # before the final hand check is made.
    revolution_state = GameState([[20], [4, 5, 6, 7]], 1)
    revolution_state.apply(Choice.play((4, 5, 6, 7), (True, True, True, True)))
    revolution_state.apply(Choice("doubt"))
    revolution_state.apply(Choice.penalty(()))
    assert revolution_state.revolution
    assert revolution_state.winner == 1

    # The strategy teacher returns the smallest truthful response and avoids a
    # needless hidden card when a visible response is available.
    strategy_state = GameState([[4, 6], [8, 40]], 0)
    strategy_state.apply(Choice.play((4,), (False,)))  # 4S
    strategy_choices = initial_actions(strategy_state, 1, rng)
    strategy_choice = strategy_choices[strategic_choice_index(strategy_state, strategy_choices, 1)]
    assert strategy_choice == Choice.play((8,), (False,))  # 5S beats 4S

    # Penalty selection is zero-sum: the CPU should not hand cards back to the
    # opponent when it controls the selector.
    penalty_state = GameState([[0], [1]], 1)
    penalty_state.field_ids = [4, 8]
    penalty_state.phase = "penalty"
    penalty_state.turn = 1
    penalty_choices = penalty_actions(penalty_state, rng)
    assert penalty_choices[strategic_choice_index(penalty_state, penalty_choices, 1)].card_ids == ()

    for _ in range(20):
        end, _ = run_episode(None, rng, None, temperature=1.0, max_steps=256, record=False)
        assert end.terminal()
        assert end.winner in (0, 1)
    print(json.dumps({"status": "ok", "obs_size": OBS_SIZE, "action_size": ACTION_SIZE}, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Million Doubt self-play NN trainer")
    parser.add_argument("--smoke", action="store_true", help="run rules and simulator smoke tests")
    parser.add_argument("--episodes", type=int, default=10000)
    parser.add_argument("--batch-episodes", type=int, default=32)
    parser.add_argument("--max-steps", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--value-weight", type=float, default=0.5)
    parser.add_argument("--entropy-weight", type=float, default=0.01)
    parser.add_argument("--teacher-weight", type=float, default=0.6)
    parser.add_argument("--teacher-probability", type=float, default=0.7)
    parser.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    parser.add_argument("--out", default="checkpoints/milliondoubt_policy.pt")
    parser.add_argument("--status-file", default="checkpoints/milliondoubt_status.json")
    parser.add_argument("--seed", type=int, default=20260912)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.smoke:
        smoke_test()
        return 0
    train(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
