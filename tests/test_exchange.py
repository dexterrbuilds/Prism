from app.exchange.client import ExchangeClient


def test_configured_symbol_is_normalized_to_linear_futures_contract() -> None:
    assert ExchangeClient._futures_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert ExchangeClient._futures_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"
