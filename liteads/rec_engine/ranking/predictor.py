"""
Fill rate and VTR prediction module for CPM CTV/In-App video.

Replaces CTR/CVR prediction models with fill rate optimization.
Predicts:
- pVTR: Predicted view-through rate (video completion)
- pFill: Predicted fill rate (likelihood of ad being served/rendered)
- pCTR: Predicted click-through rate (secondary signal)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from liteads.common.logger import get_logger
from liteads.common.utils import Timer
from liteads.schemas.internal import AdCandidate, PredictionResult, UserContext

logger = get_logger(__name__)


class BasePredictor(ABC):
    """Abstract base class for video fill rate / VTR predictors."""

    @abstractmethod
    async def predict(
        self,
        user_context: UserContext,
        candidate: AdCandidate,
    ) -> PredictionResult:
        """Predict fill rate/VTR for a single candidate."""
        pass

    @abstractmethod
    async def predict_batch(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[PredictionResult]:
        """Predict fill rate/VTR for multiple candidates."""
        pass


class StatisticalPredictor(BasePredictor):
    """
    Statistical predictor using historical fill rate data.

    Optimizes for fill rate (ad actually served and rendered on CTV/In-App)
    and VTR (video completion rate) rather than CTR/CVR.
    """

    def __init__(
        self,
        default_ctr: float = 0.005,
        default_cvr: float = 0.001,
        default_vtr: float = 0.70,
        default_fill_rate: float = 0.85,
        smoothing_impressions: int = 1000,
        smoothing_starts: int = 500,
    ):
        self.default_ctr = default_ctr
        self.default_cvr = default_cvr
        self.default_vtr = default_vtr
        self.default_fill_rate = default_fill_rate
        self.smoothing_impressions = smoothing_impressions
        self.smoothing_starts = smoothing_starts

    async def predict(
        self,
        user_context: UserContext,
        candidate: AdCandidate,
    ) -> PredictionResult:
        """Predict using smoothed historical fill rate and VTR."""
        impressions = candidate.metadata.get("impressions", 0)
        starts = candidate.metadata.get("starts", 0)
        completions = candidate.metadata.get("completions", 0)
        clicks = candidate.metadata.get("clicks", 0)

        # Fill rate: impressions that resulted in a video start
        # Smoothed: (starts + prior) / (impressions + prior)
        smoothed_fill = (starts + self.smoothing_starts * self.default_fill_rate) / (
            impressions + self.smoothing_starts
        )

        # VTR: video completions / video starts
        if starts > 0:
            smoothed_vtr = (completions + self.smoothing_starts * self.default_vtr) / (
                starts + self.smoothing_starts
            )
        else:
            smoothed_vtr = self.default_vtr

        # CTR: clicks / impressions (secondary signal for video)
        smoothed_ctr = (clicks + self.smoothing_impressions * self.default_ctr) / (
            impressions + self.smoothing_impressions
        )

        return PredictionResult(
            campaign_id=candidate.campaign_id,
            creative_id=candidate.creative_id,
            pvtr=smoothed_vtr,
            pctr=smoothed_ctr,
            model_version="statistical_fillrate_v1",
            latency_ms=0.1,
        )

    async def predict_batch(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[PredictionResult]:
        """Predict for multiple candidates."""
        results = []
        for candidate in candidates:
            result = await self.predict(user_context, candidate)
            results.append(result)
        return results


class FillRatePredictor(BasePredictor):
    """
    Fill rate optimization predictor.

    Uses environment-specific signals (CTV vs In-App) to predict
    whether an ad will be successfully filled and rendered.
    Factors include:
    - Device compatibility (CTV devices have different render capabilities)
    - Video format/codec support
    - Network conditions
    - Historical fill rate by device OS and app bundle
    """

    def __init__(
        self,
        default_vtr: float = 0.70,
        default_fill_rate: float = 0.85,
        default_ctr: float = 0.005,
    ):
        self.default_vtr = default_vtr
        self.default_fill_rate = default_fill_rate
        self.default_ctr = default_ctr

        # CTV device OS fill rate priors (based on industry data)
        self._device_fill_priors: dict[str, float] = {
            "roku": 0.92,
            "firetv": 0.90,
            "tvos": 0.93,
            "tizen": 0.87,
            "androidtv": 0.88,
            "webos": 0.85,
            "vizio": 0.83,
            "chromecast": 0.86,
        }

        # VTR priors by placement
        self._placement_vtr_priors: dict[str, float] = {
            "pre_roll": 0.75,
            "mid_roll": 0.82,
            "post_roll": 0.45,
        }

    async def predict(
        self,
        user_context: UserContext,
        candidate: AdCandidate,
    ) -> PredictionResult:
        """Predict fill rate and VTR based on environment signals."""
        # Base fill rate from device OS
        os_key = user_context.os.lower() if user_context.os else ""
        from liteads.rec_engine.retrieval.targeting import normalize_ctv_os
        os_family = normalize_ctv_os(os_key)
        base_fill = self._device_fill_priors.get(os_family, self.default_fill_rate)

        # Adjust for MIME type compatibility
        mime = candidate.mime_type
        if mime == "video/mp4":
            fill_adj = 1.0  # Universal support
        elif mime in ("application/x-mpegURL", "application/dash+xml"):
            fill_adj = 0.95  # Good but not universal
        else:
            fill_adj = 0.80  # Less common formats

        # VTR from placement type
        placement_map = {1: "pre_roll", 2: "mid_roll", 3: "post_roll"}
        placement_key = placement_map.get(candidate.placement, "pre_roll")
        vtr = self._placement_vtr_priors.get(placement_key, self.default_vtr)

        # Adjust VTR for skippable ads
        if candidate.skippable:
            vtr *= 0.85  # Skippable ads have lower completion

        # Duration penalty: longer ads have lower VTR
        if candidate.duration > 30:
            vtr *= max(0.6, 1.0 - (candidate.duration - 30) * 0.01)

        predicted_fill = base_fill * fill_adj

        return PredictionResult(
            campaign_id=candidate.campaign_id,
            creative_id=candidate.creative_id,
            pvtr=min(vtr, 1.0),
            pctr=self.default_ctr,
            model_version="fillrate_opt_v1",
            latency_ms=0.05,
        )

    async def predict_batch(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[PredictionResult]:
        """Predict fill rate/VTR for batch."""
        results = []
        for candidate in candidates:
            result = await self.predict(user_context, candidate)
            results.append(result)
        return results


class MLPredictor(BasePredictor):
    """
    ML-based predictor for video fill rate and VTR.

    Uses trained DeepFM models with CTV/In-App video features.
    """

    def __init__(
        self,
        model_path: str | None = None,
        feature_builder_path: str | None = None,
        model_version: str = "v1",
        fallback_vtr: float = 0.70,
        fallback_ctr: float = 0.005,
        device: str = "auto",
    ):
        self.model_path = model_path
        self.feature_builder_path = feature_builder_path
        self.model_version = model_version
        self.fallback_vtr = fallback_vtr
        self.fallback_ctr = fallback_ctr
        self.device = device
        self._model_predictor = None
        self._is_loaded = False

    async def load_model(self) -> None:
        """Load model and feature builder from storage."""
        if self._is_loaded:
            return

        try:
            from liteads.ml_engine.serving import ModelPredictor

            self._model_predictor = ModelPredictor(
                model_path=self.model_path,
                feature_builder_path=self.feature_builder_path,
                device=self.device,
            )
            self._model_predictor.load()
            self._is_loaded = True
            logger.info(f"Loaded ML model version {self.model_version}")
        except Exception as e:
            logger.warning(f"Failed to load ML model: {e}. Using fallback predictions.")
            self._model_predictor = None

    async def predict(
        self,
        user_context: UserContext,
        candidate: AdCandidate,
    ) -> PredictionResult:
        """Predict using ML model."""
        results = await self.predict_batch(user_context, [candidate])
        return results[0]

    async def predict_batch(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[PredictionResult]:
        """Predict for multiple candidates using batch inference."""
        with Timer("ml_prediction") as timer:
            try:
                if not self._is_loaded:
                    await self.load_model()

                features_batch = self._build_features(user_context, candidates)

                if self._model_predictor is not None:
                    ml_results = await self._model_predictor.predict_batch_async(features_batch)

                    results = []
                    for i, candidate in enumerate(candidates):
                        ml_result = ml_results[i]
                        results.append(
                            PredictionResult(
                                campaign_id=candidate.campaign_id,
                                creative_id=candidate.creative_id,
                                pvtr=ml_result.pctr,  # Model predicts VTR as primary
                                pctr=getattr(ml_result, "pcvr", self.fallback_ctr),
                                model_version=ml_result.model_version or self.model_version,
                                latency_ms=ml_result.latency_ms,
                            )
                        )
                    return results
                else:
                    return [
                        PredictionResult(
                            campaign_id=c.campaign_id,
                            creative_id=c.creative_id,
                            pvtr=self.fallback_vtr,
                            pctr=self.fallback_ctr,
                            model_version="fallback",
                            latency_ms=timer.elapsed_ms / len(candidates),
                        )
                        for c in candidates
                    ]

            except Exception as e:
                logger.error(f"ML prediction failed: {e}")
                return [
                    PredictionResult(
                        campaign_id=c.campaign_id,
                        creative_id=c.creative_id,
                        pvtr=self.fallback_vtr,
                        pctr=self.fallback_ctr,
                        model_version="fallback",
                        latency_ms=timer.elapsed_ms / len(candidates) if timer.elapsed_ms else 0,
                    )
                    for c in candidates
                ]

    def _build_features(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[dict[str, Any]]:
        """Build feature dictionaries for CTV/InApp video prediction."""
        features = []

        for candidate in candidates:
            feature_dict = {
                # Environment features
                "environment": user_context.environment,
                "device_type": user_context.device_type,
                "device_os": user_context.os or "unknown",
                "device_brand": user_context.device_brand or "unknown",
                # Geo features
                "geo_country": user_context.country or "unknown",
                "geo_dma": user_context.dma or "unknown",
                # App/content features
                "app_bundle": user_context.app_bundle or "unknown",
                "app_name": user_context.app_name or "unknown",
                "content_genre": user_context.content_genre or "unknown",
                # Video features
                "video_duration": candidate.duration,
                "video_placement": candidate.placement,
                "video_skippable": 1 if candidate.skippable else 0,
                "video_mime_type": candidate.mime_type,
                "video_bitrate": candidate.bitrate or 0,
                "video_width": candidate.width,
                "video_height": candidate.height,
                # Ad features
                "campaign_id": str(candidate.campaign_id),
                "creative_id": str(candidate.creative_id),
                "advertiser_id": str(candidate.advertiser_id),
                "bid_amount": candidate.bid,
                "creative_type": "ctv_video" if candidate.creative_type == 1 else "inapp_video",
                # Historical stats
                "ad_impressions_7d": candidate.metadata.get("impressions", 0),
                "ad_starts_7d": candidate.metadata.get("starts", 0),
                "ad_completions_7d": candidate.metadata.get("completions", 0),
                "ad_fill_rate_7d": candidate.metadata.get("fill_rate", self.fallback_vtr),
                "ad_vtr_7d": candidate.pvtr or self.fallback_vtr,
                # Context features
                "slot_id": user_context.custom_features.get("slot_id", "default"),
                "request_hour": user_context.custom_features.get("hour", 12),
                "request_day_of_week": user_context.custom_features.get("day_of_week", 0),
                "is_weekend": user_context.custom_features.get("is_weekend", 0),
                "is_prime_time": user_context.custom_features.get("is_prime_time", 0),
            }
            features.append(feature_dict)

        return features


class EnsemblePredictor(BasePredictor):
    """
    Ensemble predictor combining fill rate + VTR predictors.

    Supports weighted averaging of predictions from multiple models.
    """

    def __init__(
        self,
        predictors: list[tuple[BasePredictor, float]],
    ):
        self.predictors = predictors
        total_weight = sum(w for _, w in predictors)
        self.weights = [w / total_weight for _, w in predictors]

    async def predict(
        self,
        user_context: UserContext,
        candidate: AdCandidate,
    ) -> PredictionResult:
        """Predict using weighted ensemble."""
        results = await self.predict_batch(user_context, [candidate])
        return results[0]

    async def predict_batch(
        self,
        user_context: UserContext,
        candidates: list[AdCandidate],
    ) -> list[PredictionResult]:
        """Predict using weighted ensemble for batch."""
        all_predictions: list[list[PredictionResult]] = []

        for predictor, _ in self.predictors:
            preds = await predictor.predict_batch(user_context, candidates)
            all_predictions.append(preds)

        results = []
        for i, candidate in enumerate(candidates):
            weighted_vtr = sum(
                all_predictions[j][i].pvtr * self.weights[j]
                for j in range(len(self.predictors))
            )
            weighted_ctr = sum(
                all_predictions[j][i].pctr * self.weights[j]
                for j in range(len(self.predictors))
            )

            results.append(
                PredictionResult(
                    campaign_id=candidate.campaign_id,
                    creative_id=candidate.creative_id,
                    pvtr=weighted_vtr,
                    pctr=weighted_ctr,
                    model_version="ensemble",
                    latency_ms=max(p[i].latency_ms for p in all_predictions),
                )
            )

        return results
