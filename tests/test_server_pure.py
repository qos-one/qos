from api.server import parse_key, decimals_for, pip_for


def test_parse_key_defaults():
    assert parse_key("EURUSD") == ("FX", "EURUSD")
    assert parse_key("aapl") == ("STK", "AAPL")
    assert parse_key("crypto:btc") == ("CRYPTO", "BTC")


def test_decimals_and_pips():
    assert decimals_for("FX", "USDJPY") == 3
    assert decimals_for("FX", "EURUSD") == 5
    assert pip_for("FX", "USDJPY") == 0.01
