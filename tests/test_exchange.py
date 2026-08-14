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
