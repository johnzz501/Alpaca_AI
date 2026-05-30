import unittest
from datetime import datetime
from zoneinfo import ZoneInfo


class IntradayScannerTests(unittest.TestCase):
    def test_detects_gap_momentum_and_volume_spike_setup(self):
        import intraday_scanner

        ny = ZoneInfo("America/New_York")
        bars = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30, tzinfo=ny), 103.0, 104.0, 102.8, 103.5, 1000),
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 31, tzinfo=ny), 103.5, 104.1, 103.1, 103.9, 1100),
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 32, tzinfo=ny), 103.9, 104.3, 103.7, 104.2, 1200),
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 33, tzinfo=ny), 104.2, 105.8, 104.1, 105.6, 3600),
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 34, tzinfo=ny), 105.6, 106.5, 105.4, 106.2, 3900),
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 35, tzinfo=ny), 106.2, 107.0, 106.0, 106.8, 4200),
        ]

        setup = intraday_scanner.detect_intraday_setup("TEST", bars, previous_close=100.0, min_avg_dollar_volume=0)

        self.assertIsNotNone(setup)
        self.assertEqual(setup.symbol, "TEST")
        self.assertEqual(setup.entry, 106.8)
        self.assertGreaterEqual(setup.gap_percent, 2.0)
        self.assertGreaterEqual(setup.momentum_percent, 1.2)
        self.assertGreaterEqual(setup.volume_spike, 2.0)
        self.assertGreaterEqual(setup.stop_loss, setup.entry * 0.95)
        self.assertAlmostEqual(setup.reward_risk, 2.0)
        self.assertGreater(setup.atr_percent, 0)
        self.assertGreater(setup.avg_dollar_volume, 0)

    def test_rejects_flat_low_volume_symbol(self):
        import intraday_scanner

        ny = ZoneInfo("America/New_York")
        bars = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30 + index, tzinfo=ny), 100, 100.2, 99.8, 100.0, 1000)
            for index in range(6)
        ]

        setup = intraday_scanner.detect_intraday_setup("FLAT", bars, previous_close=100.0)

        self.assertIsNone(setup)

    def test_rejects_symbol_below_average_dollar_volume_filter(self):
        import intraday_scanner

        ny = ZoneInfo("America/New_York")
        bars = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30 + index, tzinfo=ny), 104, 109, 103.5, 104 + index, 1000)
            for index in range(6)
        ]

        setup = intraday_scanner.detect_intraday_setup(
            "THIN",
            bars,
            previous_close=100.0,
            min_avg_dollar_volume=1_000_000,
        )

        self.assertIsNone(setup)

    def test_uses_atr_based_stop_when_volatility_is_wider_than_fixed_stop(self):
        import intraday_scanner

        ny = ZoneInfo("America/New_York")
        bars = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30 + index, tzinfo=ny), 110, 116, 104, 110 + index, 100000)
            for index in range(6)
        ]

        setup = intraday_scanner.detect_intraday_setup("ATR", bars, previous_close=100.0, min_avg_dollar_volume=0)

        self.assertLess(setup.stop_loss, round(setup.entry * 0.95, 2))

    def test_scan_intraday_setups_sorts_best_opportunities_first(self):
        import intraday_scanner

        ny = ZoneInfo("America/New_York")
        mild = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30 + index, tzinfo=ny), 101, 102, 100.5, 101 + index * 0.3, 1000 + index * 700)
            for index in range(6)
        ]
        strong = [
            intraday_scanner.IntradayBar(datetime(2026, 5, 26, 9, 30 + index, tzinfo=ny), 104, 109, 103.5, 104 + index * 0.9, 1000 + index * 1600)
            for index in range(6)
        ]

        setups = intraday_scanner.rank_intraday_setups(
            [
                intraday_scanner.detect_intraday_setup("MILD", mild, previous_close=100.0, min_avg_dollar_volume=0),
                intraday_scanner.detect_intraday_setup("STRONG", strong, previous_close=100.0, min_avg_dollar_volume=0),
                None,
            ],
            limit=2,
        )

        self.assertEqual([setup.symbol for setup in setups], ["STRONG", "MILD"])

    def test_fetches_intraday_data_with_iex_feed_by_default(self):
        import intraday_scanner
        from alpaca.data.enums import DataFeed

        class FakeClient:
            def __init__(self):
                self.bars_request = None
                self.snapshot_request = None

            def get_stock_bars(self, request):
                self.bars_request = request
                return {"AAPL": []}

            def get_stock_snapshot(self, request):
                self.snapshot_request = request
                return {}

        client = FakeClient()

        intraday_scanner.fetch_intraday_bars(client, ["AAPL"])
        intraday_scanner.fetch_previous_closes(client, ["AAPL"])

        self.assertEqual(client.bars_request.feed, DataFeed.IEX)
        self.assertEqual(client.snapshot_request.feed, DataFeed.IEX)


if __name__ == "__main__":
    unittest.main()
