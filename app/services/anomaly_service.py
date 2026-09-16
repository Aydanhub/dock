"""The model, behind a small typed interface.

Everything specific to scikit-learn lives in this file. The endpoints know
only `score` and `score_many`, which is what makes the swap described in the
README a one-file change: point `_fit` at a pickled artifact or a model
registry and nothing upstream moves.
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from app.core.config import get_settings
from app.core.telemetry import tracer

logger = logging.getLogger(__name__)

FEATURE_ORDER = ("amount", "hour_of_day", "merchant_risk_score", "is_new_device")

# Waking hours carry most of the weight; late night gets very little — this is
# what makes a 3am transaction unusual to the model.
_RAW_HOUR_WEIGHTS = np.array(
    [
        1, 1, 1, 1, 1, 1,  # 00-05
        3, 5, 7, 8, 8, 8,  # 06-11
        8, 8, 8, 8, 8, 8,  # 12-17
        7, 6, 5, 4, 3, 2,  # 18-23
    ],
    dtype=float,
)
_HOUR_WEIGHTS = _RAW_HOUR_WEIGHTS / _RAW_HOUR_WEIGHTS.sum()


@dataclass(frozen=True)
class ScoringResult:
    request_id: str
    is_anomalous: bool
    anomaly_score: float
    model_version: str
    latency_ms: float


@dataclass(frozen=True)
class ModelCard:
    """Everything needed to say which model produced a given decision.

    Surfaced on `/readyz`, stamped onto every response, and attached as a
    metric label. When a score looks wrong three weeks from now, this is what
    turns "the model said so" into a reproducible answer.
    """

    version: str
    algorithm: str
    sklearn_version: str
    feature_order: tuple[str, ...]
    random_seed: int
    contamination: float
    n_estimators: int
    training_samples: int
    trained_at: str
    training_duration_ms: float


def _generate_synthetic_normal_data(n: int, rng: np.random.Generator) -> np.ndarray:
    """Synthesises n "normal" transaction-like rows to train against.

    There is no real dataset here on purpose: the template is about the
    plumbing around a model — train once, serve, trace, evaluate, ship — not
    about the model. Replace this function with a loader for real historical
    data and nothing else in the service changes.
    """
    amount = rng.lognormal(mean=3.5, sigma=0.6, size=n)
    hour_of_day = rng.choice(24, size=n, p=_HOUR_WEIGHTS)
    merchant_risk_score = rng.beta(2, 8, size=n)
    is_new_device = rng.choice([0.0, 1.0], size=n, p=[0.95, 0.05])
    return np.column_stack([amount, hour_of_day, merchant_risk_score, is_new_device])


class AnomalyService:
    """Wraps a scikit-learn IsolationForest behind a small, typed interface.

    Training happens once, at construction (called from the lifespan hook in
    `app.main`), so request latency only ever pays for inference.
    """

    def __init__(
        self,
        random_seed: int | None = None,
        contamination: float | None = None,
        training_samples: int | None = None,
        n_estimators: int | None = None,
    ) -> None:
        settings = get_settings()
        self._random_seed = random_seed if random_seed is not None else settings.model_random_seed
        self._contamination = (
            contamination if contamination is not None else settings.model_contamination
        )
        self._training_samples = (
            training_samples if training_samples is not None else settings.model_training_samples
        )
        self._n_estimators = (
            n_estimators if n_estimators is not None else settings.model_n_estimators
        )

        self._scaler = StandardScaler()
        self._model = IsolationForest(
            contamination=self._contamination,
            random_state=self._random_seed,
            n_estimators=self._n_estimators,
        )
        self._ready = False
        self._card = self._fit()

    # --- lifecycle --------------------------------------------------------

    def _fit(self) -> ModelCard:
        started = time.perf_counter()
        rng = np.random.default_rng(self._random_seed)
        data = _generate_synthetic_normal_data(self._training_samples, rng)
        scaled = self._scaler.fit_transform(data)
        self._model.fit(scaled)
        duration_ms = (time.perf_counter() - started) * 1000
        self._ready = True

        card = ModelCard(
            version=self._compute_version(),
            algorithm="IsolationForest",
            sklearn_version=sklearn.__version__,
            feature_order=FEATURE_ORDER,
            random_seed=self._random_seed,
            contamination=self._contamination,
            n_estimators=self._n_estimators,
            training_samples=self._training_samples,
            trained_at=datetime.now(tz=UTC).isoformat(),
            training_duration_ms=round(duration_ms, 3),
        )
        logger.info(
            "model trained",
            extra={
                "model_version": card.version,
                "training_duration_ms": card.training_duration_ms,
                "training_samples": card.training_samples,
            },
        )
        return card

    def _compute_version(self) -> str:
        """A deterministic fingerprint of everything that changes the model's output.

        Same inputs, same library version, same id — so two replicas that
        report different `model_version` values are genuinely running
        different models, and a version that changes after a dependency bump
        is telling the truth about the decisions changing too.
        """
        material = "|".join(
            [
                "IsolationForest",
                sklearn.__version__,
                np.__version__,
                ",".join(FEATURE_ORDER),
                str(self._random_seed),
                f"{self._contamination:.6f}",
                str(self._n_estimators),
                str(self._training_samples),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    @property
    def card(self) -> ModelCard:
        return self._card

    @property
    def model_version(self) -> str:
        return self._card.version

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --- inference --------------------------------------------------------

    def _to_matrix(self, events: list[dict[str, Any]]) -> np.ndarray:
        return np.array(
            [[float(event[name]) for name in FEATURE_ORDER] for event in events], dtype=float
        )

    def score(self, features: dict[str, Any]) -> ScoringResult:
        """Scores one event."""
        return self.score_many([features])[0]

    def score_many(self, events: list[dict[str, Any]]) -> list[ScoringResult]:
        """Scores a batch in a single model call.

        The per-call overhead of scikit-learn's `predict`/`decision_function`
        dominates the per-row cost at this size, so scoring a batch of 100 in
        one call is close to the cost of scoring one — which is why the batch
        endpoint exists at all rather than looping over `score`.

        The span wraps the model call only, so a trace separates inference
        time from request parsing and serialisation.
        """
        if not events:
            return []

        with tracer.start_as_current_span("anomaly_service.score") as span:
            span.set_attribute("anomaly.batch_size", len(events))
            span.set_attribute("anomaly.model_version", self._card.version)

            started = time.perf_counter()
            matrix = self._to_matrix(events)
            scaled = self._scaler.transform(matrix)

            # decision_function: higher = more normal. The sign is flipped so
            # that higher = more anomalous, which reads the way a caller
            # expects an "anomaly score" to read.
            raw_scores = -self._model.decision_function(scaled)
            predictions = self._model.predict(scaled)
            elapsed_ms = (time.perf_counter() - started) * 1000
            per_event_ms = elapsed_ms / len(events)

            span.set_attribute("anomaly.duration_ms", round(elapsed_ms, 3))
            span.set_attribute("anomaly.anomalous_count", int((predictions == -1).sum()))

            return [
                ScoringResult(
                    request_id=str(uuid.uuid4()),
                    is_anomalous=bool(prediction == -1),
                    anomaly_score=round(float(score), 4),
                    model_version=self._card.version,
                    latency_ms=round(per_event_ms, 3),
                )
                for score, prediction in zip(raw_scores, predictions, strict=True)
            ]
