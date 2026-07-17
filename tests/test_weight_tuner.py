from datetime import date

import pytest

from src.signals.weight_tuner import (
    MIN_SAMPLES,
    SOURCES,
    WEIGHT_FLOOR,
    SignalOutcome,
    compute_adaptive_weights,
    compute_forward_return,
    compute_source_hit_rates,
    render_scorecard,
)


def make_outcomes(source: str, hits: int, misses: int, score: float = 40.0) -> list[SignalOutcome]:
    outcomes = []
    for _ in range(hits):
        outcomes.append(SignalOutcome(source=source, symbol="AAPL", score=score, forward_return=0.02 if score > 0 else -0.02))
    for _ in range(misses):
        outcomes.append(SignalOutcome(source=source, symbol="AAPL", score=score, forward_return=-0.02 if score > 0 else 0.02))
    return outcomes


class TestForwardReturn:
    def test_basic_forward_return(self):
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]
        assert compute_forward_return(closes, 0, horizon=5) == pytest.approx(0.10)

    def test_not_enough_forward_bars_is_none(self):
        assert compute_forward_return([100.0, 101.0], 0, horizon=5) is None

    def test_negative_index_and_zero_price_are_none(self):
        closes = [0.0, 1, 2, 3, 4, 5, 6]
        assert compute_forward_return(closes, -1, horizon=2) is None
        assert compute_forward_return(closes, 0, horizon=5) is None


class TestHitRates:
    def test_every_source_always_present(self):
        result = compute_source_hit_rates([])
        assert set(result) == set(SOURCES)
        assert all(acc.defaulted and acc.hit_rate == 0.5 for acc in result.values())

    def test_hit_rate_computed_when_enough_samples(self):
        outcomes = make_outcomes("momentum", hits=9, misses=3)
        acc = compute_source_hit_rates(outcomes)["momentum"]
        assert acc.defaulted is False
        assert acc.samples == 12
        assert acc.hit_rate == pytest.approx(0.75)

    def test_below_min_samples_defaults_to_half(self):
        outcomes = make_outcomes("momentum", hits=MIN_SAMPLES - 1, misses=0)
        acc = compute_source_hit_rates(outcomes)["momentum"]
        assert acc.defaulted is True
        assert acc.hit_rate == 0.5

    def test_weak_signals_and_flat_returns_excluded(self):
        outcomes = [
            SignalOutcome(source="momentum", symbol="A", score=5.0, forward_return=0.02),   # |score| < 10
            SignalOutcome(source="momentum", symbol="A", score=40.0, forward_return=0.0),   # flat: direction undefined
        ]
        assert compute_source_hit_rates(outcomes)["momentum"].samples == 0

    def test_bearish_signal_with_falling_price_is_a_hit(self):
        outcomes = make_outcomes("news_sentiment", hits=MIN_SAMPLES, misses=0, score=-40.0)
        acc = compute_source_hit_rates(outcomes)["news_sentiment"]
        assert acc.hit_rate == 1.0

    def test_unknown_source_ignored_not_crash(self):
        outcomes = [SignalOutcome(source="astrology", symbol="A", score=90.0, forward_return=0.5)]
        assert set(compute_source_hit_rates(outcomes)) == set(SOURCES)


class TestAdaptiveWeights:
    def _accuracies(self, hit_rates: dict[str, float]):
        return compute_source_hit_rates(
            [o for source, rate in hit_rates.items() for o in make_outcomes(source, hits=round(rate * 20), misses=20 - round(rate * 20))]
        )

    def test_all_noise_reverts_to_equal_weights(self):
        weights = compute_adaptive_weights(self._accuracies({s: 0.5 for s in SOURCES}))
        assert all(w == pytest.approx(0.25) for w in weights.values())

    def test_better_source_gets_more_weight_and_floor_holds(self):
        weights = compute_adaptive_weights(self._accuracies({"momentum": 0.8, "insider": 0.5, "congressional": 0.5, "news_sentiment": 0.6}))
        assert weights["momentum"] > weights["news_sentiment"] > weights["insider"]
        assert all(w >= WEIGHT_FLOOR - 1e-9 for w in weights.values())
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_dominant_source_cannot_starve_others_below_floor(self):
        weights = compute_adaptive_weights(self._accuracies({"momentum": 1.0, "insider": 0.5, "congressional": 0.5, "news_sentiment": 0.5}))
        assert weights["insider"] == pytest.approx(WEIGHT_FLOOR)
        assert weights["momentum"] == pytest.approx(1.0 - 3 * WEIGHT_FLOOR)

    def test_bad_floor_raises(self):
        accuracies = compute_source_hit_rates([])
        with pytest.raises(ValueError):
            compute_adaptive_weights(accuracies, floor=0.30)
        with pytest.raises(ValueError):
            compute_adaptive_weights(accuracies, floor=0.0)


class TestScorecardRendering:
    def test_scorecard_has_a_row_per_source_and_flags_defaults(self):
        accuracies = compute_source_hit_rates(make_outcomes("momentum", hits=9, misses=3))
        weights = compute_adaptive_weights(accuracies)
        text = render_scorecard(accuracies, weights, as_of=date(2026, 7, 16))
        for source in SOURCES:
            assert f"| {source} |" in text
        assert "default -- too few samples" in text  # the three unsampled sources
        assert "2026-07-16" in text
