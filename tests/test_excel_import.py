from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from trade_m.app import build_app
from trade_m.config import Settings
from trade_m.excel_import import StockWorkbookError, parse_stock_workbook


def workbook_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Stocks"
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def settings(tmp_path) -> Settings:
    return Settings(
        api_key="",
        api_secret="",
        redirect_url="http://127.0.0.1:8000/auth/callback",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "app.db",
        candle_finalization_delay_seconds=3,
    )


def test_parser_accepts_optional_columns_and_skips_disabled_rows() -> None:
    content = workbook_bytes(
        [
            ["Trading Symbol", "Percentage", "Exchange", "Enabled"],
            [" reliance ", 1.43, "nse", True],
            ["TCS", 1.25, "", False],
            ["M&M", 2, None, None],
        ]
    )
    parsed = parse_stock_workbook(content)
    assert parsed.skipped == 1
    assert [(item.tradingsymbol, item.percentage, item.exchange) for item in parsed.stocks] == [
        ("RELIANCE", Decimal("1.43"), "NSE"),
        ("M&M", Decimal("2"), "NSE"),
    ]


def test_parser_understands_excel_percentage_format() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Stocks"
    sheet.append(["Trading Symbol", "Percentage"])
    sheet.append(["RELIANCE", 0.0143])
    sheet["B2"].number_format = "0.00%"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    parsed = parse_stock_workbook(output.getvalue())
    assert parsed.stocks[0].percentage == Decimal("1.4300")


def test_parser_reports_all_row_errors_before_importing() -> None:
    content = workbook_bytes(
        [
            ["Trading Symbol", "Percentage", "Exchange", "Enabled"],
            ["RELIANCE", 1.43, "NSE", True],
            ["RELIANCE", 2, "NSE", True],
            ["TCS", 0, "NSE", True],
            ["INFY", 1.2, "NASDAQ", True],
        ]
    )
    try:
        parse_stock_workbook(content)
    except StockWorkbookError as exc:
        assert len(exc.errors) == 3
        assert "appears more than once" in exc.errors[0]
        assert "greater than 0" in exc.errors[1]
        assert "Exchange must be NSE or BSE" in exc.errors[2]
    else:
        raise AssertionError("The invalid workbook should have been rejected")


def test_parser_rejects_non_excel_content() -> None:
    try:
        parse_stock_workbook(b"this is not an Excel workbook")
    except StockWorkbookError as exc:
        assert "not a readable .xlsx workbook" in str(exc)
    else:
        raise AssertionError("Non-Excel content should have been rejected")


def test_excel_upload_creates_rules_and_subscribes(tmp_path) -> None:
    app = build_app(settings(tmp_path))

    class FakeGateway:
        provider = "upstox"
        authenticated = True

        @staticmethod
        def resolve_instrument(exchange, tradingsymbol):
            return {
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "instrument_token": f"{exchange}_EQ|{tradingsymbol}",
            }

        @staticmethod
        def previous_session_close(instrument_token, trading_date):
            return trading_date - timedelta(days=1), Decimal("100")

    class FakeMonitor:
        subscribed: list[str] = []

        def subscribe(self, tokens):
            self.subscribed.extend(tokens)

        @staticmethod
        def stop():
            return None

    gateway = FakeGateway()
    monitor = FakeMonitor()
    app.state.gateways["upstox"] = gateway
    app.state.monitors["upstox"] = monitor
    content = workbook_bytes(
        [
            ["Trading Symbol", "Percentage", "Exchange", "Enabled"],
            ["RELIANCE", 1.43, "NSE", True],
            ["TCS", 1.25, "NSE", True],
            ["INFY", 1.1, "NSE", False],
        ]
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/rules/import",
            data={"provider": "upstox"},
            files={
                "workbook": (
                    "stocks.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert response.status_code == 201
        result = response.json()
        assert result["created"] == 2
        assert result["failed"] == 0
        assert result["skipped"] == 1
        assert monitor.subscribed == ["NSE_EQ|RELIANCE", "NSE_EQ|TCS"]
        rules = client.get("/api/rules").json()
        assert [rule["tradingsymbol"] for rule in rules] == ["RELIANCE", "TCS"]
        assert all(rule["provider"] == "upstox" for rule in rules)


def test_excel_upload_rejects_invalid_workbook_before_broker_calls(tmp_path) -> None:
    app = build_app(settings(tmp_path))

    class FakeGateway:
        provider = "upstox"
        authenticated = True

        @staticmethod
        def resolve_instrument(exchange, tradingsymbol):
            raise AssertionError("Broker lookup must not run for an invalid workbook")

    app.state.gateways["upstox"] = FakeGateway()
    content = workbook_bytes(
        [["Trading Symbol", "Percentage"], ["RELIANCE", 0], ["TCS", 60]]
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/rules/import",
            data={"provider": "upstox"},
            files={"workbook": ("stocks.xlsx", content)},
        )
    assert response.status_code == 400
    assert "Row 2" in response.json()["detail"]
    assert "Row 3" in response.json()["detail"]
