"""
Tests for the shared VAST pipeline helpers:
  - ``common.tracking.build_ad_id``
  - ``ad_server.services.vast_builder.build_vast_for_candidate``
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from liteads.common.tracking import build_ad_id
from liteads.ad_server.services.vast_builder import build_vast_for_candidate
from liteads.common.vast import TrackingEvent


# ---------------------------------------------------------------------------
# build_ad_id
# ---------------------------------------------------------------------------

def test_build_ad_id_basic() -> None:
    """build_ad_id returns the standard tracking identifier format."""
    assert build_ad_id(1, 42) == "ad_1_42"


def test_build_ad_id_zero_values() -> None:
    """build_ad_id works with zero IDs (demand candidates)."""
    assert build_ad_id(0, 0) == "ad_0_0"


def test_build_ad_id_large_values() -> None:
    """build_ad_id handles large integer IDs."""
    assert build_ad_id(99999, 123456) == "ad_99999_123456"


# ---------------------------------------------------------------------------
# build_vast_for_candidate
# ---------------------------------------------------------------------------

def _make_candidate(**kwargs: object) -> MagicMock:
    """Create a minimal AdCandidate mock for testing."""
    c = MagicMock()
    c.campaign_id = kwargs.get("campaign_id", 1)
    c.creative_id = kwargs.get("creative_id", 10)
    c.title = kwargs.get("title", "Test Ad")
    c.bid = kwargs.get("bid", 5.0)
    c.duration = kwargs.get("duration", 30)
    c.video_url = kwargs.get("video_url", None)
    c.vast_url = kwargs.get("vast_url", None)
    c.mime_type = kwargs.get("mime_type", "video/mp4")
    c.bitrate = kwargs.get("bitrate", 2500)
    c.landing_url = kwargs.get("landing_url", None)
    c.skippable = kwargs.get("skippable", False)
    c.skip_after = kwargs.get("skip_after", None)
    c.companion_image_url = kwargs.get("companion_image_url", None)
    return c


_COMMON_KWARGS = dict(
    vast_version="4.0",
    ad_id="ad_1_10",
    tracking_events=[TrackingEvent(event="start", url="https://track.example.com/start")],
    impression_url="https://track.example.com/imp",
    error_url="https://track.example.com/err",
    base_url="https://ads.example.com",
    request_id="req123",
    env="ctv",
    width=1920,
    height=1080,
)


def test_build_vast_for_candidate_inline() -> None:
    """Returns InLine VAST XML when candidate has a video_url."""
    candidate = _make_candidate(video_url="https://cdn.example.com/video.mp4")
    result = build_vast_for_candidate(candidate, **_COMMON_KWARGS)

    assert result is not None
    assert "<VAST" in result
    assert "<InLine>" in result
    assert "https://cdn.example.com/video.mp4" in result


def test_build_vast_for_candidate_wrapper() -> None:
    """Returns Wrapper VAST XML when candidate has a vast_url."""
    candidate = _make_candidate(vast_url="https://dsp.example.com/vast?id=abc")
    result = build_vast_for_candidate(candidate, **_COMMON_KWARGS)

    assert result is not None
    assert "<VAST" in result
    assert "<Wrapper" in result
    assert "https://dsp.example.com/vast?id=abc" in result
    # Wrapper must NOT include our <Impression> to prevent double-fire
    assert "<Impression>" not in result


def test_build_vast_for_candidate_no_media() -> None:
    """Returns None when candidate has neither video_url nor vast_url."""
    candidate = _make_candidate()
    result = build_vast_for_candidate(candidate, **_COMMON_KWARGS)

    assert result is None


def test_build_vast_for_candidate_inline_with_nurl_burl() -> None:
    """VAST builder accepts nurl/burl for InLine candidates without error."""
    candidate = _make_candidate(video_url="https://cdn.example.com/video.mp4")
    result = build_vast_for_candidate(
        candidate,
        **_COMMON_KWARGS,
        nurl="https://ads.example.com/api/v1/event/win?req=req123",
        burl="https://ads.example.com/api/v1/event/billing?req=req123",
    )

    assert result is not None
    assert "<InLine>" in result


def test_build_vast_for_candidate_wrapper_with_nurl_burl() -> None:
    """nurl and burl are embedded in Wrapper VAST XML when provided."""
    candidate = _make_candidate(vast_url="https://dsp.example.com/vast?id=abc")
    result = build_vast_for_candidate(
        candidate,
        **_COMMON_KWARGS,
        nurl="https://ads.example.com/api/v1/event/win?req=req123",
        burl="https://ads.example.com/api/v1/event/billing?req=req123",
    )

    assert result is not None
    assert "<Wrapper" in result


def test_build_vast_for_candidate_vast_url_takes_precedence() -> None:
    """When both vast_url and video_url are set, Wrapper is preferred."""
    candidate = _make_candidate(
        vast_url="https://dsp.example.com/vast?id=abc",
        video_url="https://cdn.example.com/video.mp4",
    )
    result = build_vast_for_candidate(candidate, **_COMMON_KWARGS)

    assert result is not None
    assert "<Wrapper" in result
    assert "<InLine>" not in result
