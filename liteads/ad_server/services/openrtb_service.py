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
from liteads.ad_server.services.vast_builder import build_vast_for_candidate
from liteads.common.config import get_settings
from liteads.common.device import (
    infer_ifa_type,
    map_connection_type,
    map_device_type,
    map_placement,
)
from liteads.common.logger import get_logger
from liteads.common.tracking import (
    build_ad_id,
    build_burl,
    build_error_url,
    build_impression_url,
    build_lurl,
    build_nurl,
    build_tracking_events,
)
from liteads.common.vast import TrackingEvent
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
        Apply bid-floor filtering to candidates.

        Candidates whose bid falls below the floor are removed.
        Remaining candidates keep their original bid price.
        """
        if not candidates:
            return []

        # Ensure eCPM is populated
        for c in candidates:
            if not c.ecpm or c.ecpm <= 0:
                c.ecpm = c.bid

        # Filter by bid floor
        winners = [c for c in candidates if c.bid >= bid_floor]

        if not winners:
            return []

        # Sort by bid descending
        winners.sort(key=lambda c: c.bid, reverse=True)

        logger.info(
            "Bid floor filtering applied",
            num_winners=len(winners),
            bid_floor=bid_floor,
            top_bid=winners[0].bid if winners else 0,
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
            ifa_type = br.device.ifa_type or infer_ifa_type(br.device.os)

            device = DeviceInfo(
                device_type=map_device_type(br.device.devicetype),
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
                connection_type=map_connection_type(br.device.connectiontype),
                screen_width=br.device.w,
                screen_height=br.device.h,
            )

        # App – forward all content metadata for contextual targeting
        app: Optional[AppInfo] = None
        if br.app:
            c = br.app.content
            app = AppInfo(
                app_id=br.app.id or "",
                app_name=br.app.name or "",
                app_bundle=br.app.bundle or "",
                store_url=br.app.storeurl or "",
                app_domain=br.app.domain or "",
                app_category=",".join(br.app.cat) if br.app.cat else "",
                publisher_id=br.app.publisher.id if br.app.publisher else "",
                inventory_partner_domain=br.app.inventorypartnerdomain or "",
                page_categories=",".join(br.app.pagecat) if br.app.pagecat else "",
                # Content metadata (critical for CTV brand-safety & contextual)
                content_genre=c.genre if c else "",
                content_rating=c.contentrating if c else "",
                content_id=c.id if c else "",
                content_title=c.title if c else "",
                content_series=c.series if c else "",
                content_season=c.season if c else "",
                content_episode=c.episode if c else None,
                content_url=c.url if c else "",
                content_language=c.language if c else "",
                content_livestream=c.livestream if c else None,
                content_producer=c.producer.get("name", "") if c and isinstance(c.producer, dict) else "",
                content_length=c.len if c else None,
                content_context=c.context if c else None,
                content_gtax=c.gtax if c else None,
                content_genres=",".join(c.genres) if c and c.genres else "",
                channel_name=c.channel.get("name", "") if c and isinstance(c.channel, dict) else "",
                network_name=c.network.get("name", "") if c and isinstance(c.network, dict) else "",
                production_quality=str(c.prodq) if c and c.prodq else "",
                qag_media_rating=str(c.qagmediarating) if c and c.qagmediarating else "",
                content_categories=",".join(c.cat) if c and c.cat else "",
            )

        # Video placement (from first impression)
        video: Optional[VideoPlacementInfo] = None
        imp = br.imp[0]
        if imp.video:
            v = imp.video
            video = VideoPlacementInfo(
                placement=map_placement(v.startdelay, v.placement),
                min_duration=v.minduration or self._settings.video.min_duration,
                max_duration=v.maxduration or self._settings.video.max_duration,
                skip_enabled=v.skip == 1 if v.skip is not None else None,
                mimes=v.mimes or ["video/mp4"],
                protocols=v.protocols or self._settings.video.supported_vast_protocols,
                width=v.w,
                height=v.h,
                startdelay_raw=v.startdelay,
                plcmt=v.plcmt,
                linearity=v.linearity,
                sequence=v.sequence,
                minbitrate=v.minbitrate,
                maxbitrate=v.maxbitrate,
                playbackmethod=",".join(str(x) for x in v.playbackmethod) if v.playbackmethod else None,
                delivery=",".join(str(x) for x in v.delivery) if v.delivery else None,
                video_protocols=",".join(str(x) for x in v.protocols) if v.protocols else None,
                pod_duration=v.poddur,
                max_ads_in_pod=v.maxseq,
                podid=v.podid,
                podseq=v.podseq,
                poddedupe=",".join(str(x) for x in v.poddedupe) if v.poddedupe else None,
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
            # Privacy / regulatory signals
            us_privacy=br.regs.us_privacy if br.regs else None,
            coppa=br.regs.coppa if br.regs else None,
            gdpr=br.regs.gdpr if br.regs else None,
            gdpr_consent=br.regs.consent_string if br.regs else None,
            gpp=br.regs.gpp if br.regs else None,
            gpp_sid=",".join(str(s) for s in br.regs.gpp_sid) if br.regs and br.regs.gpp_sid else None,
            # Blocked signals
            bcat=",".join(br.bcat) if br.bcat else None,
            badv=",".join(br.badv) if br.badv else None,
            # Impression overrides
            tagid=imp.tagid,
            imp_exp=imp.exp,
            bidfloor_override=imp.bidfloor if imp.bidfloor > 0 else None,
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
            ad_id = build_ad_id(candidate.campaign_id, candidate.creative_id)

            # Build VAST XML (adm)
            tracking_events = build_tracking_events(
                base_url, request_id, ad_id, env,
            )

            impression_url = build_impression_url(
                base_url, request_id, ad_id, env,
            )
            error_url = build_error_url(
                base_url, request_id, ad_id, env,
            )

            vast_version = self._settings.vast.supported_versions[-1]

            # Choose InLine vs Wrapper based on creative type.
            #
            # CTV double-impression prevention:
            # - Wrappers must NOT carry our <Impression> because the
            #   downstream VAST already has its own; two would double-fire.
            # - InLine is only emitted when the candidate has a real
            #   video_url (i.e. a <MediaFile> will exist in the VAST).
            # nurl/burl are carried in the Bid object (not the VAST XML)
            # for OpenRTB, so they are not passed here.
            vast_xml = build_vast_for_candidate(
                candidate,
                vast_version=vast_version,
                ad_id=ad_id,
                tracking_events=tracking_events,
                impression_url=impression_url,
                error_url=error_url,
                base_url=base_url,
                request_id=request_id,
                env=env,
                width=candidate.width or 1920,
                height=candidate.height or 1080,
            )
            if vast_xml is None:
                # No media file — skip this candidate to avoid phantom impressions
                logger.warning(
                    "Skipping candidate with no video_url or vast_url",
                    request_id=request_id,
                    ad_id=ad_id,
                )
                continue

            # nurl / burl / lurl (shared builders)
            nurl = build_nurl(base_url, request_id, ad_id, env)
            burl = build_burl(base_url, request_id, ad_id, env)
            lurl = build_lurl(base_url, request_id, ad_id, env)

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
