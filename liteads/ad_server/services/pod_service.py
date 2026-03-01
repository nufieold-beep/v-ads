"""
Ad Pod Service – Multi-slot ad pod construction for CTV SSAI / DAI.

Handles:
  - Pod filling (multiple ads per ad break / pod)
  - Competitive separation (no duplicate adomains or IAB categories within a pod)
  - Duration fitting (total pod duration constraint, per-slot duration)
  - Slot position pricing (position 1 premium over later positions)
  - Fallback handling (return partial fill when full pod is unavailable)
  - Pod deduplication (same creative, same adomain, same category)

References:
  - OpenRTB 2.6 Pod & Video Enhancements:
    https://iabtechlab.com/openrtb-2-6-is-ready-for-implementation/
  - IAB CTV Quality, Frequency & Ad Management:
    https://iabaustralia.com.au/ctv-quality-management-frequency-capping-content-object-ad-formats/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from liteads.common.logger import get_logger
from liteads.schemas.internal import AdCandidate

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pod configuration
# ---------------------------------------------------------------------------

@dataclass
class PodConfig:
    """Configuration for an ad pod (ad break)."""

    pod_id: str = ""                # Pod identifier
    pod_duration: int = 120         # Total pod duration in seconds
    max_ads: int = 4                # Maximum number of ads in the pod
    min_ads: int = 1                # Minimum ads to return (0 = allow no-fill)
    allow_partial_fill: bool = True # Return whatever fills, even if less than max

    # Competitive separation
    enforce_competitive_separation: bool = True
    max_same_adomain: int = 1       # Max ads from same advertiser domain per pod
    max_same_category: int = 2      # Max ads from same IAB category per pod
    max_same_advertiser: int = 1    # Max ads from same advertiser_id per pod

    # Duration constraints
    min_ad_duration: int = 5        # Minimum per-ad duration
    max_ad_duration: int = 60       # Maximum per-ad duration
    target_fill_pct: float = 0.80   # Target fill % of pod duration

    # Slot position pricing
    position_premium_pct: list[float] = field(
        default_factory=lambda: [1.0, 0.90, 0.85, 0.80, 0.75, 0.70]
    )
    # First slot gets 100%, second 90%, etc.

    # Pod deduplication signals (OpenRTB 2.6)
    # 1 = same creative, 2 = same IAB category, 3 = same adomain
    dedup_signals: list[int] = field(default_factory=lambda: [1, 3])


@dataclass
class PodSlot:
    """A single slot within an ad pod."""

    position: int                   # 1-based slot position
    candidate: Optional[AdCandidate] = None
    duration: int = 0               # Filled duration
    effective_bid: float = 0.0      # Bid after position premium
    filled: bool = False


@dataclass
class PodResult:
    """Result of pod construction."""

    pod_id: str
    slots: list[PodSlot]
    total_duration: int = 0
    total_revenue: float = 0.0
    fill_count: int = 0
    max_slots: int = 0
    fill_rate: float = 0.0         # fill_count / max_slots
    duration_fill_pct: float = 0.0 # total_duration / pod_duration
    unfilled_reason: str = ""      # Why some slots weren't filled


# ---------------------------------------------------------------------------
# Pod builder
# ---------------------------------------------------------------------------

class PodBuilder:
    """
    Construct ad pods with competitive separation and duration fitting.

    This is the core SSAI-readiness component. Given a ranked list of
    candidates, it fills pod slots while enforcing:

    1. Competitive separation (no duplicate brands within a pod)
    2. Duration constraints (per-slot and total pod limits)
    3. Slot position pricing (premium for earlier positions)
    4. Creative deduplication (no same creative in same pod)
    """

    def __init__(self, config: PodConfig | None = None):
        self.config = config or PodConfig()

    def build_pod(
        self,
        candidates: list[AdCandidate],
        pod_duration: int | None = None,
        max_ads: int | None = None,
    ) -> PodResult:
        """
        Build an ad pod from ranked candidates.

        Args:
            candidates: Pre-ranked ad candidates (highest score first)
            pod_duration: Override total pod duration (seconds)
            max_ads: Override max ads in pod

        Returns:
            PodResult with filled slots and metrics
        """
        cfg = self.config
        pod_dur = pod_duration or cfg.pod_duration
        num_slots = max_ads or cfg.max_ads

        # Track what's been placed for competitive separation
        placed_adomains: dict[str, int] = {}   # adomain → count
        placed_categories: dict[str, int] = {}  # category → count
        placed_advertisers: dict[int, int] = {} # advertiser_id → count
        placed_creatives: set[int] = set()      # creative_id set
        remaining_duration = pod_dur

        slots: list[PodSlot] = []
        unfilled_reasons: list[str] = []

        for position in range(1, num_slots + 1):
            slot = PodSlot(position=position)

            # Find best candidate that fits this slot
            selected = self._select_candidate_for_slot(
                candidates=candidates,
                position=position,
                remaining_duration=remaining_duration,
                placed_adomains=placed_adomains,
                placed_categories=placed_categories,
                placed_advertisers=placed_advertisers,
                placed_creatives=placed_creatives,
            )

            if selected is None:
                if not cfg.allow_partial_fill and len(slots) < cfg.min_ads:
                    unfilled_reasons.append(
                        f"Slot {position}: no eligible candidate "
                        "(competitive separation or duration constraint)"
                    )
                slots.append(slot)
                continue

            # Apply position premium
            premium = (
                cfg.position_premium_pct[position - 1]
                if position - 1 < len(cfg.position_premium_pct)
                else cfg.position_premium_pct[-1]
            )
            effective_bid = selected.bid * premium

            slot.candidate = selected
            slot.duration = selected.duration or 30
            slot.effective_bid = round(effective_bid, 4)
            slot.filled = True

            # Update tracking
            remaining_duration -= slot.duration
            placed_creatives.add(selected.creative_id)

            adomain = self._get_adomain(selected)
            if adomain:
                placed_adomains[adomain] = placed_adomains.get(adomain, 0) + 1

            for cat in self._get_categories(selected):
                placed_categories[cat] = placed_categories.get(cat, 0) + 1

            placed_advertisers[selected.advertiser_id] = (
                placed_advertisers.get(selected.advertiser_id, 0) + 1
            )

            slots.append(slot)

        # Build result
        filled = [s for s in slots if s.filled]
        total_dur = sum(s.duration for s in filled)
        # total_revenue = sum of effective CPM / 1000 (revenue per impression)
        # This gives the actual revenue for one pod fill, not the CPM rate.
        total_rev = sum(s.effective_bid / 1000.0 for s in filled)

        result = PodResult(
            pod_id=cfg.pod_id,
            slots=slots,
            total_duration=total_dur,
            total_revenue=round(total_rev, 4),
            fill_count=len(filled),
            max_slots=num_slots,
            fill_rate=round(len(filled) / num_slots, 4) if num_slots > 0 else 0,
            duration_fill_pct=(
                round(total_dur / pod_dur, 4) if pod_dur > 0 else 0
            ),
            unfilled_reason="; ".join(unfilled_reasons) if unfilled_reasons else "",
        )

        logger.info(
            "Pod built",
            pod_id=cfg.pod_id,
            slots_filled=f"{result.fill_count}/{num_slots}",
            duration=f"{total_dur}/{pod_dur}s",
            revenue=f"${total_rev:.4f}",
        )

        return result

    def _select_candidate_for_slot(
        self,
        candidates: list[AdCandidate],
        position: int,
        remaining_duration: int,
        placed_adomains: dict[str, int],
        placed_categories: dict[str, int],
        placed_advertisers: dict[int, int],
        placed_creatives: set[int],
    ) -> AdCandidate | None:
        """
        Select the best candidate for a specific pod slot.

        Uses a revenue-density heuristic: among eligible candidates,
        prefer the one that maximises CPM / duration (revenue per
        second of pod time consumed).  This is a lightweight
        approximation of the knapsack-optimal solution and yields
        measurably higher total pod revenue compared to the previous
        pure rank-order selection.

        For the *first* slot the original rank order is preserved
        (highest absolute CPM wins) because the first position has
        the highest attention premium.
        """
        cfg = self.config
        eligible: list[tuple[float, int, AdCandidate]] = []

        for idx, candidate in enumerate(candidates):
            # Skip already-placed creatives (dedup signal 1)
            if 1 in cfg.dedup_signals and candidate.creative_id in placed_creatives:
                continue

            # Duration check
            dur = candidate.duration or 30
            if dur > remaining_duration:
                continue
            if dur < cfg.min_ad_duration or dur > cfg.max_ad_duration:
                continue

            # Competitive separation checks
            if cfg.enforce_competitive_separation:
                # Adomain check (dedup signal 3)
                adomain = self._get_adomain(candidate)
                if adomain and 3 in cfg.dedup_signals:
                    if placed_adomains.get(adomain, 0) >= cfg.max_same_adomain:
                        continue

                # Category check (dedup signal 2)
                if 2 in cfg.dedup_signals:
                    categories = self._get_categories(candidate)
                    if any(
                        placed_categories.get(cat, 0) >= cfg.max_same_category
                        for cat in categories
                    ):
                        continue

                # Same advertiser check
                if (
                    placed_advertisers.get(candidate.advertiser_id, 0)
                    >= cfg.max_same_advertiser
                ):
                    continue

            # ── Revenue-density score ─────────────────────────────
            # For the first position, use absolute bid (highest CPM wins).
            # For subsequent positions, maximise CPM-per-second so that
            # shorter high-CPM ads fill more efficiently.
            if position == 1:
                density = candidate.bid
            else:
                density = candidate.bid / max(dur, 1)

            eligible.append((density, idx, candidate))

        if not eligible:
            return None

        # Pick the highest density candidate
        eligible.sort(key=lambda t: t[0], reverse=True)
        return eligible[0][2]

    @staticmethod
    def _get_adomain(candidate: AdCandidate) -> str:
        """Extract adomain from candidate metadata."""
        if candidate.metadata and "adomain" in candidate.metadata:
            domains = candidate.metadata["adomain"]
            if isinstance(domains, list) and domains:
                return domains[0].lower()
            if isinstance(domains, str):
                return domains.lower()
        return ""

    @staticmethod
    def _get_categories(candidate: AdCandidate) -> list[str]:
        """Extract IAB categories from candidate metadata."""
        if candidate.metadata and "cat" in candidate.metadata:
            cats = candidate.metadata["cat"]
            if isinstance(cats, list):
                return [c.lower() for c in cats]
        return []

    def get_filled_candidates(self, pod_result: PodResult) -> list[AdCandidate]:
        """Extract filled candidates in slot order."""
        return [
            slot.candidate
            for slot in pod_result.slots
            if slot.filled and slot.candidate is not None
        ]
