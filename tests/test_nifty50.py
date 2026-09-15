from trade_m.nifty50 import FALLBACK_SYMBOLS, Nifty50Service


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body.encode()


def test_official_nifty_csv_is_parsed(monkeypatch) -> None:
    symbols = [f"STOCK{index}" for index in range(50)]
    body = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
        f"Company {index},Industry,{symbol},EQ,INE{index:09d}"
        for index, symbol in enumerate(symbols)
    )
    monkeypatch.setattr(
        "trade_m.nifty50.urlopen", lambda request, timeout: FakeResponse(body)
    )
    result = Nifty50Service().get()
    assert result.source == "NSE Indices"
    assert result.symbols == tuple(symbols)


def test_nifty_fallback_has_exactly_50_symbols(monkeypatch) -> None:
    def fail(request, timeout):
        raise TimeoutError("offline")

    monkeypatch.setattr("trade_m.nifty50.urlopen", fail)
    result = Nifty50Service().get()
    assert result.source == "packaged fallback"
    assert result.symbols == FALLBACK_SYMBOLS
    assert len(result.symbols) == 50
