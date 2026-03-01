"""
OpenRTB 2.6 Service – CPM CTV & In-App Video Only.

Converts OpenRTB bid requests into the internal LiteAds pipeline format,
runs the recommendation engine, and converts results back into OpenRTB
bid responses with VAST XML markup, nurl, and burl.
"""

from __future__ import annotations

from typing import Optional

from liteads.ad_server.services.ad_service import AdService
from liteads.ad_server.services.pod_service import PodBuilder, PodConfig
from liteads.common.config import get_settings
from liteads.common.logger import get_logger
from liteads.common.vast import TrackingEvent, build_vast_xml, build_vast_wrapper_xml
from liteads.rec_engine.ranking.bidding import SecondPriceAuction
from liteads.schemas.openrtb import (
    Bid,
    BidRequest,
    BidResponse,
    NoBidReason,
    SeatBid,
)
from liteads.schemas.request import (
    AdRequest,
    AppInfo,
    DeviceInfo,
    VideoPlacementInfo,
)
from liteads.schemas.internal import AdCandidate

logger = get_logger(__name__)


class OpenRTBService:
    """
    Translates OpenRTB 2.6 ←→ internal LiteAds pipeline.

    Flow:
        1. Receive OpenRTB BidRequest
        2. Translate to internal AdRequest
        3. Run AdService pipeline (retrieval → filter → predict → rank)
        4. Build VAST XML for each winning creative
        5. Return OpenRTB BidResponse with nurl / burl / adm
    """

    def __init__(self, ad_service: AdService):
        self._ad_service = ad_service
        self._settings = get_settings()
        self._pod_builder = PodBuilder()
        self._auction = SecondPriceAuction(
            increment=0.01,
            bid_floor=0.0,  # will be overridden per-request
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_bid_request(self, bid_request: BidRequest) -> Optional[BidResponse]:
        """
        Process an OpenRTB bid request and return a bid response.

        Supports single-impression and pod (multi-impression) requests.
        Pod requests use competitive separation (no duplicate adomains/
        categories within the same pod) and duration fitting.

        Applies a second-price auction so that bid responses contain
        clearing prices rather than first-price bids, which increases
        buyer confidence and long-term yield.

        Returns ``None`` when there is no fill (caller should return HTTP 204).
        """
        try:
            internal_request = self._to_internal_request(bid_request)
            request_id = internal_request.request_id or bid_request.id

            # Determine if this is a pod request
            is_pod = self._is_pod_request(bid_request)

            # Request more candidates for pods so we have enough after separation
            if is_pod:
                internal_request.num_ads = max(
                    internal_request.num_ads * 3, 12,
                )

            candidates = await self._ad_service.serve_ads(
                request=internal_request,
                request_id=request_id,
            )

            if not candidates:
                logger.info(
                    "No fill for OpenRTB request",
                    request_id=bid_request.id,
                    is_pod=is_pod,
                )
                return None

            # ── Derive bid floor from the first impression ────────
            imp0 = bid_request.imp[0]
            bid_floor = imp0.bidfloor if imp0.bidfloor and imp0.bidfloor > 0 else 0.0

            # Apply pod construction with competitive separation
            if is_pod:
                candidates = self._apply_pod_construction(
                    bid_request, candidates,
                )
                if not candidates:
                    return None

            # ── Second-price auction ──────────────────────────────
            # Apply second-price clearing to each candidate so the
            # bid response price reflects what the winner would
            # actually pay, rather than first-price (their full bid).
            # This is critical for revenue: exchanges penalise SSPs
            # that consistently return first-price bids in a
            # purportedly second-price auction.
            candidates = self._apply_auction_pricing(
                candidates, bid_floor=bid_floor,
            )

            if not candidates:
                return None

            return self._to_bid_response(bid_request, candidates, request_id)

        except Exception:
            logger.exception("Error processing OpenRTB bid request", request_id=bid_request.id)
            return BidResponse(
                id=bid_request.id,
                nbr=NoBidReason.TECHNICAL_ERROR,
            )

    def _is_pod_request(self, br: BidRequest) -> bool:
        """Detect if bid request is for an ad pod."""
        if len(br.imp) > 1:
            return True
        imp = br.imp[0]
        if imp.video:
            if imp.video.poddur and imp.video.poddur > 0:
                return True
            if imp.video.maxseq and imp.video.maxseq > 1:
                return True
            if imp.video.podid:
                return True
        return False

    def _apply_pod_construction(
        self, br: BidRequest, candidates: list[AdCandidate],
    ) -> list[AdCandidate]:
        """Apply pod construction with competitive separation."""
        imp = br.imp[0]
        v = imp.video

        pod_duration = 120  # default
        max_ads = len(br.imp) if len(br.imp) > 1 else 4

        if v:
            if v.poddur and v.poddur > 0:
                pod_duration = v.poddur
            if v.maxseq and v.maxseq > 0:
                max_ads = v.maxseq

        # Map OpenRTB poddedupe signals to config
        dedup_signals = [1, 3]  # default: creative + adomain
        if v and v.poddedupe:
            dedup_signals = list(v.poddedupe)

        config = PodConfig(
            pod_id=v.podid if v else "",
            pod_duration=pod_duration,
            max_ads=max_ads,
            enforce_competitive_separation=True,
            dedup_signals=dedup_signals,
            allow_partial_fill=True,
        )

        builder = PodBuilder(config)
        result = builder.build_pod(candidates, pod_duration, max_ads)

        logger.info(
            "Pod construction completed",
            pod_id=config.pod_id,
            fill_rate=result.fill_rate,
            filled=result.fill_count,
            total_slots=result.max_slots,
            revenue=result.total_revenue,
        )

        return builder.get_filled_candidates(result)

    def _apply_auction_pricing(
        self,
        candidates: list[AdCandidate],
        bid_floor: float = 0.0,
    ) -> list[AdCandidate]:
        """
        Apply second-price auction clearing to candidates.

        For single-winner auctions the winner pays second-highest + ε.
        For multi-winner (pod) auctions a generalised second-price (GSP)
        mechanism is used where each winner pays the next winner's bid.

        The clearing price is stored back into ``candidate.bid`` so that
        the bid response reflects the actual expected payment rather than
        the full first-price bid. This is standard practice for SSPs.
        """
        if not candidates:
            return []

        # Ensure eCPM is populated
        for c in candidates:
            if not c.ecpm or c.ecpm <= 0:
                c.ecpm = c.bid

        if len(candidates) == 1:
            # Single candidate: clearing at floor + increment
            winner, clearing = self._auction.run_auction(
                candidates, bid_floor=bid_floor,
            )
            if winner is None:
                return []
            winner.bid = clearing
            return [winner]

        # Multi-winner GSP auction
        results = self._auction.run_multi_winner_auction(
            candidates,
            num_winners=len(candidates),
            bid_floor=bid_floor,
        )

        if not results:
            return []

        winners = []
        for winner, clearing in results:
            winner.bid = clearing
            winners.append(winner)

        logger.info(
            "Auction pricing applied",
            num_winners=len(winners),
            bid_floor=bid_floor,
            top_clearing=winners[0].bid if winners else 0,
        )

        return winners

    # ------------------------------------------------------------------
    # OpenRTB → Internal
    # ------------------------------------------------------------------

    def _to_internal_request(self, br: BidRequest) -> AdRequest:
        """Convert OpenRTB BidRequest to internal AdRequest."""
        env = br.environment  # "ctv" or "inapp"

        # Device
        device: Optional[DeviceInfo] = None
        if br.device:
            # Prefer ext.ifa_type if available (e.g. Roku sends {"ifa_type":"rida"})
            ifa_type = br.device.ifa_type or self._infer_ifa_type(br.device.os)

            device = DeviceInfo(
                device_type=self._map_device_type(br.device.devicetype),
                os=(br.device.os or "").lower().replace(" ", ""),
                os_version=br.device.osv or "",
                make=(br.device.make or "").strip(),
                model=(br.device.model or "").strip(),
                ifa=br.device.ifa,
                ifa_type=ifa_type,
                lmt=(br.device.lmt == 1 or br.device.dnt == 1)
                    if (br.device.lmt is not None or br.device.dnt is not None) else None,
                ip=br.device.ip,
                ua=br.device.ua,
                language=br.device.language,
                connection_type=self._map_connection_type(br.device.connectiontype),
                screen_width=br.device.w,
                screen_height=br.device.h,
            )

        # App
        app: Optional[AppInfo] = None
        if br.app:
            genre = ""
            rating = ""
            content_id = ""
            if br.app.content:
                genre = br.app.content.genre or ""
                rating = br.app.content.contentrating or ""
                content_id = br.app.content.id or ""
            app = AppInfo(
                app_id=br.app.id or "",
                app_name=br.app.name or "",
                app_bundle=br.app.bundle or "",
                store_url=br.app.storeurl or "",
                content_genre=genre,
                content_rating=rating,
                content_id=content_id,
            )

        # Video placement (from first impression)
        video: Optional[VideoPlacementInfo] = None
        imp = br.imp[0]
        if imp.video:
            v = imp.video
            video = VideoPlacementInfo(
                placement=self._map_placement(v.startdelay, v.placement),
                min_duration=v.minduration or self._settings.video.min_duration,
                max_duration=v.maxduration or self._settings.video.max_duration,
                skip_enabled=v.skip == 1 if v.skip is not None else None,
                mimes=v.mimes or ["video/mp4"],
                protocols=v.protocols or self._settings.video.supported_vast_protocols,
                width=v.w,
                height=v.h,
                pod_duration=v.poddur,
                max_ads_in_pod=v.maxseq,
            )

        # Geo
        geo_country = ""
        geo_region = ""
        geo_dma = ""
        if br.device and br.device.geo:
            geo_country = br.device.geo.country or ""
            geo_region = br.device.geo.region or ""
            geo_dma = br.device.geo.metro or ""

        return AdRequest(
            request_id=br.id,
            slot_id=imp.tagid or "default",
            environment=env,
            user_id=br.user.id if br.user else None,
            device=device,
            app=app,
            video=video,
            geo_country=geo_country,
            geo_region=geo_region,
            geo_dma=geo_dma,
            num_ads=len(br.imp),
            bid_floor=imp.bidfloor if imp.bidfloor > 0 else None,
        )

    # ------------------------------------------------------------------
    # Internal → OpenRTB
    # ------------------------------------------------------------------

    def _to_bid_response(
        self,
        br: BidRequest,
        candidates: list[AdCandidate],
        request_id: str,
    ) -> BidResponse:
        """Convert internal AdCandidates to OpenRTB BidResponse."""
        bids: list[Bid] = []

        base_url = self._settings.vast.tracking_base_url or ""
        env = br.environment

        for idx, candidate in enumerate(candidates):
            imp = br.imp[idx] if idx < len(br.imp) else br.imp[0]
            ad_id = f"ad_{candidate.campaign_id}_{candidate.creative_id}"

            # Build VAST XML (adm)
            tracking_events = self._build_tracking_events(
                base_url, request_id, ad_id, env,
            )

            impression_url = (
                f"{base_url}/api/v1/event/track?"
                f"type=impression&req={request_id}&ad={ad_id}&env={env}"
            )
            error_url = (
                f"{base_url}/api/v1/event/track?"
                f"type=error&req={request_id}&ad={ad_id}&env={env}"
            )

            vast_version = self._settings.vast.supported_versions[-1]

            # Choose InLine vs Wrapper based on creative type:
            # - If the creative has a vast_url, generate a Wrapper that
            #   redirects the player to the external VAST tag while
            #   injecting our own tracking pixels.
            # - Otherwise, generate a full InLine with the video media.
            if candidate.vast_url:
                # Wrapper – external VAST tag (e.g. demand/DSP response)
                click_tracking_url = (
                    f"{base_url}/api/v1/event/track?"
                    f"type=click&req={request_id}&ad={ad_id}&env={env}"
                )
                vast_xml = build_vast_wrapper_xml(
                    version=vast_version,
                    ad_id=ad_id,
                    creative_id=str(candidate.creative_id),
                    vast_tag_uri=candidate.vast_url,
                    ad_title=candidate.title or "Video Ad",
                    impression_urls=[impression_url],
                    error_urls=[error_url],
                    tracking_events=tracking_events,
                    click_tracking=[click_tracking_url],
                    price=round(candidate.bid, 4),
                )
            else:
                # InLine – direct video creative
                vast_xml = build_vast_xml(
                    version=vast_version,
                    ad_id=ad_id,
                    creative_id=str(candidate.creative_id),
                    ad_title=candidate.title or "Video Ad",
                    duration=candidate.duration or 30,
                    video_url=candidate.video_url,
                    video_mime=candidate.mime_type or "video/mp4",
                    bitrate=candidate.bitrate or 2500,
                    width=candidate.width,
                    height=candidate.height,
                    click_through=candidate.landing_url,
                    skip_offset=candidate.skip_after if candidate.skippable else None,
                    impression_urls=[impression_url],
                    error_urls=[error_url],
                    tracking_events=tracking_events,
                    companion_image_url=candidate.companion_image_url,
                    price=round(candidate.bid, 4),
                )

            # nurl / burl with ${AUCTION_PRICE} macro
            nurl = (
                f"{base_url}/api/v1/event/win?"
                f"req={request_id}&ad={ad_id}"
                f"&price=${{AUCTION_PRICE}}&env={env}"
            )
            burl = (
                f"{base_url}/api/v1/event/billing?"
                f"req={request_id}&ad={ad_id}"
                f"&price=${{AUCTION_PRICE}}&env={env}"
            )
            # lurl with ${AUCTION_LOSS} and ${AUCTION_PRICE} macros
            lurl = (
                f"{base_url}/api/v1/event/loss?"
                f"req={request_id}&ad={ad_id}"
                f"&price=${{AUCTION_PRICE}}&loss=${{AUCTION_LOSS}}&env={env}"
            )

            # Populate adomain from candidate metadata (required by most exchanges)
            adomain: list[str] = candidate.metadata.get("adomain", []) if candidate.metadata else []
            # Populate IAB content categories
            cat: list[str] = candidate.metadata.get("cat", []) if candidate.metadata else []

            bid = Bid(
                id=f"bid-{request_id}-{idx}",
                impid=imp.id,
                price=round(candidate.bid, 4),
                nurl=nurl,
                burl=burl,
                lurl=lurl,
                adm=vast_xml,
                adid=ad_id,
                adomain=adomain,
                cid=str(candidate.campaign_id),
                crid=str(candidate.creative_id),
                cat=cat,
                dur=candidate.duration,
                mtype=2,  # 2 = video
                protocol=self._vast_version_to_protocol(vast_version),
                w=candidate.width,
                h=candidate.height,
            )
            bids.append(bid)

        if not bids:
            return BidResponse(id=br.id, nbr=NoBidReason.UNKNOWN_ERROR)

        return BidResponse(
            id=br.id,
            bidid=f"bidresp-{request_id}",
            seatbid=[SeatBid(bid=bids, seat=self._settings.openrtb.seat_id)],
            cur=br.cur[0] if br.cur else "USD",
        )

    # ------------------------------------------------------------------
    # Tracking events builder
    # ------------------------------------------------------------------

    def _build_tracking_events(
        self, base_url: str, request_id: str, ad_id: str, env: str,
    ) -> list[TrackingEvent]:
        """Build VAST tracking event list."""
        events = [
            "start", "firstQuartile", "midpoint", "thirdQuartile",
            "complete", "mute", "unmute", "pause", "resume",
            "skip", "fullscreen", "exitFullscreen",
            "close", "acceptInvitation",
        ]
        result: list[TrackingEvent] = []
        for event_name in events:
            url = (
                f"{base_url}/api/v1/event/track?"
                f"type={event_name}&req={request_id}&ad={ad_id}&env={env}"
            )
            result.append(TrackingEvent(event=event_name, url=url))
        return result

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_device_type(device_type: Optional[int]) -> str:
        """Map IAB device type to internal device_type string."""
        mapping = {
            1: "mobile",
            2: "pc",
            3: "ctv",               # Connected TV
            4: "phone",
            5: "tablet",
            6: "connected_device",
            7: "set_top_box",       # STB treated as CTV
        }
        return mapping.get(device_type or 0, "unknown")

    @staticmethod
    def _infer_ifa_type(os: Optional[str]) -> str:
        """Infer IFA type from OS string or device make."""
        if not os:
            return "unknown"
        os_lower = os.lower()
        if "roku" in os_lower:
            return "rida"
        if "fire" in os_lower or "amazon" in os_lower:
            return "afai"
        if "tvos" in os_lower or "apple" in os_lower:
            return "idfa"
        if "ios" in os_lower:
            return "idfa"
        if "tizen" in os_lower or "samsung" in os_lower:
            return "tifa"
        if "webos" in os_lower or "lg" in os_lower:
            return "lgudid"
        if "android" in os_lower:
            return "gaid"
        if "vizio" in os_lower:
            return "vida"
        if "chromecast" in os_lower:
            return "gaid"
        if "playstation" in os_lower:
            return "unknown"
        if "xbox" in os_lower:
            return "unknown"
        return "unknown"

    @staticmethod
    def _map_connection_type(conn_type: Optional[int]) -> str:
        """Map IAB connection type."""
        if conn_type is None:
            return "unknown"
        mapping = {
            1: "ethernet",
            2: "wifi",
            3: "cellular_unknown",
            4: "2g",
            5: "3g",
            6: "4g",
            7: "5g",
        }
        return mapping.get(conn_type, "unknown")

    @staticmethod
    def _map_placement(
        start_delay: Optional[int],
        placement: Optional[int],
    ) -> str:
        """Map OpenRTB start delay / placement to internal placement string."""
        if start_delay is not None:
            if start_delay == 0:
                return "pre_roll"
            if start_delay > 0 or start_delay == -1:
                return "mid_roll"
            if start_delay == -2:
                return "post_roll"
        # Fallback on placement
        if placement and placement == 1:
            return "pre_roll"   # In-stream defaults to pre-roll
        return "pre_roll"

    @staticmethod
    def _vast_version_to_protocol(version: str) -> int:
        """Convert VAST version string to OpenRTB protocol enum."""
        mapping = {
            "2.0": 2,
            "3.0": 3,
            "4.0": 6,
            "4.1": 7,
            "4.2": 8,
        }
        return mapping.get(version, 6)
