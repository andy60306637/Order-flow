import json
import os
import tempfile
import unittest

from strategies.wick_reversal_v5 import WickReversalV5Strategy


class TestWickReversalV5Regime(unittest.TestCase):
    def test_band_params_override_legacy_regime(self):
        strategy = WickReversalV5Strategy()
        strategy.enable_regime_mode = True
        strategy.regime_band_size = 10_000.0
        strategy.regime_band_floor = 0.0
        strategy.b6_long_k0_vol_gate = 777.0

        self.assertEqual(strategy._rp("long_k0_vol_gate", 65_000.0), 777.0)

    def test_missing_band_params_fall_back_to_legacy_regime(self):
        strategy = WickReversalV5Strategy()
        strategy.enable_regime_mode = True
        strategy.regime_band_size = 10_000.0
        strategy.regime_band_floor = 0.0

        self.assertEqual(strategy._rp("long_k0_vol_gate", 65_000.0), strategy.r1_long_k0_vol_gate)
        self.assertEqual(strategy._rp("short_k0_vol_gate", 92_000.0), strategy.r2_short_k0_vol_gate)

    def test_zero_band_size_uses_legacy_regime_only(self):
        strategy = WickReversalV5Strategy()
        strategy.enable_regime_mode = True
        strategy.regime_band_size = 0.0
        strategy.b6_long_k0_vol_gate = 777.0

        self.assertEqual(strategy._rp("long_k0_vol_gate", 65_000.0), strategy.r1_long_k0_vol_gate)

    def test_load_band_params_json_from_combined_params(self):
        strategy = WickReversalV5Strategy()
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "combined_params": {
                            "enable_regime_mode": True,
                            "regime_band_size": 10_000.0,
                            "regime_band_floor": 0.0,
                            "b5_long_k0_vol_gate": 1337.0,
                        }
                    },
                    f,
                )
            applied = strategy.load_band_params_json(path)
            self.assertEqual(applied["b5_long_k0_vol_gate"], 1337.0)
            self.assertEqual(strategy._rp("long_k0_vol_gate", 55_000.0), 1337.0)
        finally:
            os.remove(path)

    def test_dump_and_reload_band_params_json(self):
        strategy = WickReversalV5Strategy()
        strategy.enable_regime_mode = True
        strategy.regime_band_size = 10_000.0
        strategy.regime_band_floor = 0.0
        strategy.b6_long_k0_vol_gate = 1666.0

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            payload = strategy.dump_band_params_json(path)
            self.assertIn("b6_long", payload["accepted_bands"])

            loaded = WickReversalV5Strategy()
            loaded.load_band_params_json(path)
            self.assertEqual(loaded._rp("long_k0_vol_gate", 65_000.0), 1666.0)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
