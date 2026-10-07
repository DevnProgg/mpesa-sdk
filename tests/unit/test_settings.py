import pytest

import MpesaSDK as mp
from MpesaSDK.errors import ConfigError
from MpesaSDK.retry import RetryPolicy
from MpesaSDK.settings import MpesaConfig, RetryStrategy, parse_config

BASE = {"market": mp.markets.LESOTHO, "api-key": "k", "public-key": "p"}


def test_hyphenated_dict_from_the_public_api_is_parsed():
    cfg = parse_config(
        {
            **BASE,
            "live": True,
            "concurrency": True,
            "max-workers": 3,
            "retry-strategy": {"retry-count": 3, "back-off": 5.0, "jitter": 2},
        }
    )
    assert cfg.live and cfg.environment == "openapi"
    assert cfg.max_workers == 3 and cfg.concurrency
    assert cfg.retry == RetryStrategy(retry_count=3, back_off=5.0, jitter=2)


def test_defaults_are_sandbox_and_inline():
    cfg = parse_config(BASE)
    assert not cfg.live and cfg.environment == "sandbox" and not cfg.concurrency


def test_underscore_keys_also_work():
    assert parse_config({"market": "LESOTHO", "api_key": "k", "public_key": "p"}).max_workers == 4


@pytest.mark.parametrize("market", ["lesotho", "vodacomLES", "LES", mp.markets.LESOTHO])
def test_market_resolution(market):
    assert parse_config({**BASE, "market": market}).market is mp.markets.LESOTHO


def test_unknown_market():
    with pytest.raises(ConfigError, match="Unknown market"):
        parse_config({**BASE, "market": "narnia"})


def test_typos_fail_loudly():
    with pytest.raises(ConfigError, match="Unknown provider option 'api-ky'"):
        parse_config({**BASE, "api-ky": "x"})
    with pytest.raises(ConfigError, match="Unknown retry-strategy option"):
        parse_config({**BASE, "retry-strategy": {"retries": 3}})


@pytest.mark.parametrize("missing", ["market", "api-key", "public-key"])
def test_required_options(missing):
    raw = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match="Missing required"):
        parse_config(raw)


@pytest.mark.parametrize(
    "bad",
    [{"max-workers": 0}, {"request-timeout": 0}, {"api-key": "  "}, {"audit-handler": 5}],
)
def test_invalid_values(bad):
    with pytest.raises(ConfigError):
        parse_config({**BASE, **bad})


def test_invalid_retry_values():
    with pytest.raises(ConfigError):
        RetryStrategy(retry_count=-1)
    with pytest.raises(ConfigError):
        RetryStrategy(back_off=-1)


def test_secrets_never_appear_in_repr():
    text = repr(parse_config({**BASE, "api-key": "SECRET-KEY", "public-key": "SECRET-PUB"}))
    assert "SECRET" not in text


def test_config_object_passes_through():
    cfg = parse_config(BASE)
    assert parse_config(cfg) is cfg and isinstance(cfg, MpesaConfig)


class TestRetryPolicy:
    def test_exponential_with_cap(self):
        policy = RetryPolicy(RetryStrategy(5, 5.0, 0.0, max_back_off=30.0), rng=lambda: 0.0)
        assert [policy.delay(n) for n in (1, 2, 3, 4, 5)] == [5.0, 10.0, 20.0, 30.0, 30.0]

    def test_jitter_is_added_within_bounds(self):
        assert RetryPolicy(RetryStrategy(3, 5.0, 2.0), rng=lambda: 1.0).delay(1) == 7.0
        assert RetryPolicy(RetryStrategy(3, 5.0, 2.0), rng=lambda: 0.5).delay(1) == 6.0

    def test_should_retry_counts_retries_not_attempts(self):
        policy = RetryPolicy(RetryStrategy(retry_count=2))
        assert [policy.should_retry(n) for n in (0, 1, 2)] == [True, True, False]

    def test_zero_retries_never_retries(self):
        assert not RetryPolicy(RetryStrategy(retry_count=0)).should_retry(0)

    def test_delay_is_one_based(self):
        with pytest.raises(ValueError):
            RetryPolicy(RetryStrategy()).delay(0)
