"""
CPM bidding and ranking module for CTV and In-App video.

All billing is CPM-based. Ranking strategies optimize for
video-specific metrics: VTR (view-through rate), completion, engagement.
Revenue optimization uses expected revenue = CPM × pVTR × fill_probability.
"""

from __future__ import annotations

import math
import random
from enum import IntEnum

from liteads.common.logger import get_logger
from liteads.schemas.internal import AdCandidate

logger = get_logger(__name__)


class RankingStrategy(IntEnum):
    """Ranking strategy enum for CPM video."""

    CPM = 1               # Pure CPM ranking (bid = eCPM)
    VTR_WEIGHTED = 2      # CPM weighted by predicted VTR
    ENGAGEMENT = 3        # CPM weighted by engagement (CTR + VTR)
    COMPLETION = 4        # CPM weighted by predicted completion rate
    REVENUE_OPTIMIZED = 5 # Expected revenue: CPM × pVTR × quality × pacing


class Bidding:
    """
    CPM bidding and ranking calculator for video ads.

    Since all campaigns are CPM, eCPM equals the bid directly.
    Ranking strategies layer video-specific quality signals on top.
    REVENUE_OPTIMIZED maximises expected revenue per impression by
    combining bid value with predicted fill/completion signals.
    """

    def __init__(
        self,
        strategy: RankingStrategy = RankingStrategy.CPM,
        min_cpm: float = 0.01,
        bid_floor: float = 0.0,
    ):
        self.strategy = strategy
        self.min_cpm = min_cpm
        self.bid_floor = bid_floor

    def calculate_ecpm(self, candidate: AdCandidate) -> float:
        """Calculate eCPM for CPM candidate.

        For CPM billing, eCPM is simply the bid amount.
        """
        return max(candidate.bid, self.min_cpm)

    def calculate_score(self, candidate: AdCandidate) -> float:
        """
        Calculate ranking score based on strategy.

        Strategies:
        - CPM: Pure bid-based (eCPM = bid)
        - VTR_WEIGHTED: eCPM * predicted VTR
        - ENGAGEMENT: eCPM * (VTR + CTR blend)
        - COMPLETION: eCPM * predicted completion rate
        - REVENUE_OPTIMIZED: Expected revenue =
              eCPM × pVTR^0.6 × (1 + pctr_bonus) × placement_mult
        """
        ecpm = self.calculate_ecpm(candidate)
        pvtr = getattr(candidate, "pvtr", None) or 0.70
        pctr = candidate.pctr or 0.005

        if self.strategy == RankingStrategy.CPM:
            score = ecpm

        elif self.strategy == RankingStrategy.VTR_WEIGHTED:
            # Weight CPM by predicted view-through rate
            vtr_factor = max(pvtr, 0.01)
            score = ecpm * vtr_factor

        elif self.strategy == RankingStrategy.ENGAGEMENT:
            # Blend VTR and CTR signals
            engagement = 0.7 * pvtr + 0.3 * min(pctr * 100, 1.0)
            score = ecpm * max(engagement, 0.01)

        elif self.strategy == RankingStrategy.COMPLETION:
            # Prioritize ads with high completion rate
            completion_factor = max(pvtr, 0.01) ** 0.5
            score = ecpm * completion_factor

        elif self.strategy == RankingStrategy.REVENUE_OPTIMIZED:
            # ── Expected-revenue ranking ──────────────────────────
            # Revenue = CPM / 1000 per impression.  We want to rank
            # candidates by the revenue we *expect* to collect, which
            # is proportional to:
            #   bid × P(impression billable)
            # P(billable) ≈ pVTR (view-through is the billing event proxy).
            #
            # Add a small CTR bonus (CTR indicates user engagement,
            # correlated with higher downstream yield for the publisher).
            #
            # Use pVTR^0.6 instead of raw pVTR to dampen the quality
            # signal — this prevents low-bid / high-VTR ads from
            # displacing high-bid ads entirely, keeping auction
            # competitive and CPMs high.
            vtr_signal = max(pvtr, 0.01) ** 0.6
            ctr_bonus = 1.0 + min(pctr * 50, 0.15)  # up to +15%

            # Placement multiplier: pre-roll is more valuable
            placement_mult = _PLACEMENT_REVENUE_MULT.get(
                getattr(candidate, "placement", 1), 1.0
            )

            # Duration efficiency: revenue per second of pod time
            dur = max(candidate.duration or 30, 1)
            duration_efficiency = 30.0 / dur  # normalise to 30s baseline

            score = ecpm * vtr_signal * ctr_bonus * placement_mult * duration_efficiency

        else:
            score = ecpm

        return score

    def rank(
        self,
        candidates: list[AdCandidate],
        apply_ecpm: bool = True,
    ) -> list[AdCandidate]:
        """
        Rank candidates by score (highest first).

        Applies bid-floor filtering before ranking so that candidates
        below the floor never enter the auction — this raises effective
        clearing prices and improves publisher revenue.

        Args:
            candidates: List of candidates to rank
            apply_ecpm: Whether to calculate and apply eCPM

        Returns:
            Sorted list of candidates (highest score first)
        """
        if not candidates:
            return []

        # ── Pre-rank bid-floor enforcement ───────────────────────
        if self.bid_floor > 0:
            candidates = [c for c in candidates if c.bid >= self.bid_floor]
            if not candidates:
                return []

        for candidate in candidates:
            if apply_ecpm:
                candidate.ecpm = self.calculate_ecpm(candidate)
            candidate.score = self.calculate_score(candidate)

        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)

        logger.debug(
            f"Ranked {len(ranked)} CPM video candidates "
            f"(strategy={self.strategy.name})",
            top_score=ranked[0].score if ranked else 0,
        )

        return ranked


# ── Placement revenue multiplier (pre-roll is most valuable) ─────────
_PLACEMENT_REVENUE_MULT: dict[int, float] = {
    1: 1.00,  # pre_roll  — highest attention
    2: 0.95,  # mid_roll  — slight drop
    3: 0.60,  # post_roll — significant drop
}


class SecondPriceAuction:
    """
    Second price auction for CPM video inventory.

    Winner pays ``max(second_highest_bid, bid_floor) + increment``.
    Using the bid floor as the price floor prevents the winner from
    paying near-zero in thin auctions, which is the single biggest
    revenue leak in typical ad-server deployments.

    Supports *soft floors*: when the highest bid is between the soft
    and hard floor, the clearing price is the soft floor itself (the
    buyer gets the impression at a fair minimum rather than losing it).
    """

    def __init__(
        self,
        increment: float = 0.01,
        bid_floor: float = 0.0,
        soft_floor: float | None = None,
    ):
        self.increment = increment
        self.bid_floor = bid_floor
        # soft_floor defaults to 80% of bid_floor when not specified
        self.soft_floor = soft_floor if soft_floor is not None else bid_floor * 0.8

    def run_auction(
        self,
        candidates: list[AdCandidate],
        bid_floor: float | None = None,
    ) -> tuple[AdCandidate | None, float]:
        """
        Run second price auction.

        Args:
            candidates: Ranked candidates (highest score first).
            bid_floor: Override bid floor for this auction.

        Returns:
            Tuple of (winner, clearing_cpm).
            If no candidate exceeds the floor, returns (None, 0.0).
        """
        if not candidates:
            return None, 0.0

        floor = bid_floor if bid_floor is not None else self.bid_floor

        # Filter to candidates at or above floor
        eligible = [c for c in candidates if c.ecpm >= floor]
        if not eligible:
            # Soft-floor fallback: let the top bidder through at soft_floor
            if candidates[0].ecpm >= self.soft_floor > 0:
                winner = candidates[0]
                return winner, self.soft_floor
            return None, 0.0

        winner = eligible[0]

        if len(eligible) == 1:
            # Only one eligible bidder → clearing price = floor + increment
            clearing = max(floor, self.increment)
        else:
            # Standard second-price: pay second-highest bid + increment,
            # but never less than the floor.
            second_price = eligible[1].ecpm
            clearing = max(second_price + self.increment, floor)

        # Never exceed the winner's own bid
        clearing = min(clearing, winner.ecpm)

        return winner, round(clearing, 6)

    def run_multi_winner_auction(
        self,
        candidates: list[AdCandidate],
        num_winners: int = 1,
        bid_floor: float | None = None,
    ) -> list[tuple[AdCandidate, float]]:
        """
        Run a multi-winner generalised second-price (GSP) auction.

        Each winner pays the bid of the next-highest bidder (or the
        floor for the last winner).  This is the standard mechanism
        for ad-pod / multi-slot auctions.

        Returns:
            List of (winner, clearing_price) tuples.
        """
        floor = bid_floor if bid_floor is not None else self.bid_floor
        eligible = [c for c in candidates if c.ecpm >= floor]

        results: list[tuple[AdCandidate, float]] = []

        for i in range(min(num_winners, len(eligible))):
            winner = eligible[i]
            if i + 1 < len(eligible):
                next_price = eligible[i + 1].ecpm
                clearing = max(next_price + self.increment, floor)
            else:
                clearing = max(floor, self.increment)
            clearing = min(clearing, winner.ecpm)
            results.append((winner, round(clearing, 6)))

        return results


class BudgetPacing:
    """
    Budget pacing for smooth ad delivery.

    Ensures budget is spent evenly throughout the day using
    probabilistic throttling rather than hard cut-offs, which
    prevents the common pattern of "budget exhausted by noon"
    that leaves afternoon impressions unfilled.
    """

    def __init__(
        self,
        daily_budget: float,
        hours_remaining: int = 24,
        smoothing_factor: float = 1.2,
    ):
        """
        Initialize budget pacing.

        Args:
            daily_budget: Total daily budget
            hours_remaining: Hours remaining in the day
            smoothing_factor: Factor to adjust pacing (>1 = slightly aggressive)
        """
        self.daily_budget = daily_budget
        self.hours_remaining = max(hours_remaining, 1)
        self.smoothing_factor = smoothing_factor

    def get_hourly_budget(self, spent_today: float) -> float:
        """
        Get recommended hourly budget.

        Args:
            spent_today: Amount spent today so far

        Returns:
            Recommended hourly budget
        """
        remaining_budget = max(0, self.daily_budget - spent_today)
        ideal_hourly = remaining_budget / self.hours_remaining

        # Apply smoothing factor
        return ideal_hourly * self.smoothing_factor

    def should_serve(
        self,
        candidate: AdCandidate,
        spent_this_hour: float,
        hourly_budget: float,
    ) -> bool:
        """
        Determine if ad should be served based on probabilistic pacing.

        Uses a throttle probability that gradually reduces serving
        rate as the hourly budget is consumed, rather than a hard
        cut-off.  Higher-CPM candidates are given priority through
        a bid-weighted probability boost.

        Args:
            candidate: Ad candidate
            spent_this_hour: Amount spent this hour
            hourly_budget: Budget for this hour

        Returns:
            True if ad should be served
        """
        if hourly_budget <= 0:
            return False

        if spent_this_hour >= hourly_budget:
            return False

        remaining_ratio = (hourly_budget - spent_this_hour) / hourly_budget

        # ── Probabilistic throttle ────────────────────────────────
        # Base probability follows a sigmoid curve:
        #   - When 80-100% budget remains → serve ~100%
        #   - When 50% remains → serve ~85%
        #   - When 20% remains → serve ~40%
        #   - When 5% remains → serve ~10%
        # This spreads spend more evenly than a hard 10% cutoff.
        base_prob = 1.0 / (1.0 + math.exp(-10 * (remaining_ratio - 0.3)))

        # Bid-weighted boost: higher-CPM ads get priority when budget
        # is tight, maximising revenue from remaining budget.
        bid_factor = min(candidate.bid / max(self.daily_budget * 0.001, 0.01), 1.5)
        serve_prob = min(base_prob * bid_factor, 1.0)

        return random.random() < serve_prob

    def adjust_bid(
        self,
        bid: float,
        spent_today: float,
        target_spend: float,
    ) -> float:
        """
        Adjust bid based on pacing status.

        Uses a smooth adjustment curve instead of the previous
        discrete 0.8x / 1.2x jumps, which caused bid oscillation.

        Args:
            bid: Original bid
            spent_today: Amount spent today
            target_spend: Target spend by this time

        Returns:
            Adjusted bid (capped at 1.5× and floored at 0.5×).
        """
        if target_spend <= 0:
            return bid

        pacing_ratio = spent_today / target_spend

        # Smooth multiplier centred on 1.0
        # Under-pacing (ratio < 1.0) → multiplier > 1.0 (bid up)
        # Over-pacing  (ratio > 1.0) → multiplier < 1.0 (bid down)
        # Using log scale for smooth adjustments.
        if pacing_ratio <= 0:
            multiplier = 1.5  # Severely under-pacing
        else:
            # Inverse relationship: as pacing_ratio goes up, multiplier goes down
            raw = 1.0 / max(pacing_ratio, 0.01)
            # Dampen the adjustment (sqrt brings it closer to 1.0)
            multiplier = raw ** 0.4

        # Clamp to [0.5, 1.5] to prevent extreme swings
        multiplier = max(0.5, min(1.5, multiplier))

        return round(bid * multiplier, 6)
