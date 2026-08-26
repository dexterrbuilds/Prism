import pytest

from app.config import Settings
from app.exchange.client import ExchangeClient


def test_configured_symbol_is_normalized_to_linear_futures_contract() -> None:
    assert ExchangeClient._futures_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert ExchangeClient._futures_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_exchange_loads_only_linear_futures_metadata(monkeypatch) -> None:
    monkeypatch.setenv("EXCHANGE", "binance")
    client = ExchangeClient(Settings.from_env())
    assert client._client.options["fetchMarkets"]["types"] == ["linear"]
    assert client._client.options["fetchCurrencies"] is False


@pytest.mark.asyncio
async def test_batch_price_snapshot_normalizes_futures_symbols(monkeypatch) -> None:
    client = ExchangeClient(Settings.from_env())

    async def fake_fetch_tickers(symbols: list[str]) -> dict[str, dict[str, float]]:
        assert symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        return {
            "BTC/USDT:USDT": {"last": 60_000.0},
            "ETH/USDT:USDT": {"close": 2_500.0},
        }

    monkeypatch.setattr(client._client, "fetch_tickers", fake_fetch_tickers)
    prices = await client.fetch_prices(("BTC/USDT", "ETH/USDT"))
    assert prices == {"BTC/USDT": 60_000.0, "ETH/USDT": 2_500.0}
    await client.close()
