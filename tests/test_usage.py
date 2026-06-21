"""Token/cost rollup: rates, cache multipliers, unknown-model handling."""
import unittest

from fixtures import cd, make_session, assistant_usage

M = 1_000_000


class TestUsage(unittest.TestCase):
    def test_input_output_at_list_price(self):
        # opus-4-8 = $5/MTok in, $25/MTok out.
        s = make_session([assistant_usage("claude-opus-4-8", input=M, output=M)])
        self.assertEqual(s.tok_in, M)
        self.assertEqual(s.tok_out, M)
        self.assertAlmostEqual(s.cost_usd, 5.0 + 25.0, places=6)
        self.assertTrue(s.cost_complete)

    def test_cache_read_multiplier(self):
        # cache read ≈ 0.1× input rate → $0.50 for 1M opus cache-read tokens.
        s = make_session([assistant_usage("claude-opus-4-8", cache_read=M)])
        self.assertAlmostEqual(s.cost_usd, 0.5, places=6)
        self.assertEqual(s.tok_cache_read, M)

    def test_cache_write_multiplier(self):
        # cache write ≈ 1.25× input rate → $6.25 for 1M opus cache-write tokens.
        s = make_session([assistant_usage("claude-opus-4-8", cache_write=M)])
        self.assertAlmostEqual(s.cost_usd, 6.25, places=6)

    def test_sonnet_rate(self):
        s = make_session([assistant_usage("claude-sonnet-4-6", input=M, output=M)])
        self.assertAlmostEqual(s.cost_usd, 3.0 + 15.0, places=6)

    def test_unknown_model_marks_incomplete_but_counts_tokens(self):
        s = make_session([assistant_usage("claude-future-9", input=M, output=M)])
        self.assertFalse(s.cost_complete)
        self.assertEqual(s.cost_usd, 0.0)   # no price → no cost added
        self.assertEqual(s.tok_in, M)       # tokens still tallied

    def test_synthetic_model_skipped_without_marking_incomplete(self):
        s = make_session([assistant_usage("<synthetic>", input=M)])
        self.assertTrue(s.cost_complete)
        self.assertEqual(s.cost_usd, 0.0)

    def test_multiple_models_sum(self):
        s = make_session([
            assistant_usage("claude-opus-4-8", input=M),
            assistant_usage("claude-haiku-4-5", input=M),
        ])
        # opus $5 + haiku $1 = $6
        self.assertAlmostEqual(s.cost_usd, 6.0, places=6)


if __name__ == "__main__":
    unittest.main()
