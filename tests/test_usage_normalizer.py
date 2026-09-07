"""Shared ``usage_evidence`` V1 normalizer tests (MultiNexus #21)."""

import json
import unittest
from decimal import Decimal, localcontext

from multinexus.usage import (
    MAX_COST_MICROUSD,
    MAX_RECORDS,
    MAX_TOKEN_VALUE,
    normalize_usage_evidence,
    unknown_usage_evidence,
)


def _assert_json_clean(test: unittest.TestCase, value) -> None:
    """整个值必须可 json.dumps 且递归无 Decimal 残留。"""
    dumped = json.dumps(value)
    test.assertNotIn("Decimal", dumped)
    json.loads(dumped)

    def walk(node):
        if isinstance(node, Decimal):
            test.fail(f"Decimal leaked into output: {node!r}")
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)


class NormalizeUsageEvidenceTests(unittest.TestCase):
    def test_qoder_complete_record_from_decimal_cost(self):
        evidence = normalize_usage_evidence(
            provider="qoder",
            model_usage={
                "lite": {
                    "inputTokens": 100,
                    "outputTokens": 25,
                    "cacheReadInputTokens": 5,
                    "cacheCreationInputTokens": 2,
                    "costUSD": Decimal("0.000123"),
                }
            },
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["contract_version"], 1)
        record = evidence["records"][0]
        self.assertEqual(record["provider"], "qoder")
        self.assertEqual(record["model"], "lite")
        self.assertEqual(record["input_tokens"], 100)
        self.assertEqual(record["output_tokens"], 25)
        self.assertEqual(record["cache_read_tokens"], 5)
        self.assertEqual(record["cache_write_tokens"], 2)
        self.assertEqual(record["provider_cost_microusd"], 123)
        self.assertEqual(record["source"], "provider_reported")
        self.assertEqual(record["completeness"], "complete")
        _assert_json_clean(self, evidence)

    def test_integer_json_cost_scales_exactly_to_microusd(self):
        evidence = normalize_usage_evidence(
            provider="qoder", model_usage={"m": {"costUSD": 1}}
        )
        record = evidence["records"][0]
        self.assertEqual(record["provider_cost_microusd"], 1_000_000)
        # 只有 cost 已知 -> partial，而不是 complete。
        self.assertEqual(record["completeness"], "partial")
        evidence = normalize_usage_evidence(
            provider="qoder", model_usage={"m": {"costUSD": 0}}
        )
        self.assertEqual(evidence["records"][0]["provider_cost_microusd"], 0)
        self.assertEqual(evidence["records"][0]["completeness"], "partial")

    def test_inexact_or_out_of_range_cost_degrades_to_null(self):
        cases = [
            Decimal("0.0000001"),  # 1e-7 USD -> 0.1 microusd，非整数
            Decimal("1e13"),  # 1e19 microusd 越界 signed 64-bit
            Decimal("-0.001"),
            Decimal("NaN"),
            Decimal("Infinity"),
            -5,
            2**63,  # int 越界
            True,
            0.5,  # float 不经 Decimal 入口，一律拒绝
            "0.5",
            None,
        ]
        for cost in cases:
            with self.subTest(cost=cost):
                evidence = normalize_usage_evidence(
                    provider="qoder", model_usage={"m": {"costUSD": cost}}
                )
                record = evidence["records"][0]
                self.assertIsNone(record["provider_cost_microusd"])
                self.assertEqual(record["completeness"], "partial")
                _assert_json_clean(self, evidence)

    def test_decimal_conversion_does_not_round_through_context(self):
        # More significant digits than the default Decimal context must not be
        # rounded into a different microusd value.
        exact = Decimal("0.1234560000000000000000000000000000000000")
        evidence = normalize_usage_evidence(
            provider="qoder", model_usage={"m": {"costUSD": exact}}
        )
        self.assertEqual(evidence["records"][0]["provider_cost_microusd"], 123456)

        inexact = Decimal("0.1234560000000000000000000000000000000001")
        evidence = normalize_usage_evidence(
            provider="qoder", model_usage={"m": {"costUSD": inexact}}
        )
        self.assertIsNone(evidence["records"][0]["provider_cost_microusd"])

    def test_decimal_boundary_is_independent_of_context_precision(self):
        boundary = Decimal("9223372036854.775807")
        with localcontext() as context:
            context.prec = 6
            evidence = normalize_usage_evidence(
                provider="qoder", model_usage={"m": {"costUSD": boundary}}
            )
        self.assertEqual(
            evidence["records"][0]["provider_cost_microusd"],
            MAX_COST_MICROUSD,
        )

    def test_grok_partial_ignores_model_calls(self):
        evidence = normalize_usage_evidence(
            provider="grok",
            model_usage={
                "kimi-for-coding": {
                    "inputTokens": 3,
                    "outputTokens": 4,
                    "cacheReadInputTokens": 1,
                    "modelCalls": 7,
                }
            },
        )
        record = evidence["records"][0]
        self.assertEqual(record["provider"], "grok")
        self.assertEqual(record["input_tokens"], 3)
        self.assertEqual(record["output_tokens"], 4)
        self.assertEqual(record["cache_read_tokens"], 1)
        # 缺失 cost/cacheWrite -> null；modelCalls 不是 allowlist 字段。
        self.assertIsNone(record["cache_write_tokens"])
        self.assertIsNone(record["provider_cost_microusd"])
        self.assertEqual(record["source"], "provider_reported")
        self.assertEqual(record["completeness"], "partial")
        _assert_json_clean(self, evidence)
        self.assertNotIn("modelCalls", json.dumps(evidence))

    def test_unknown_fields_are_never_forwarded(self):
        secret = "RAW-PROVIDER-SENTINEL"
        evidence = normalize_usage_evidence(
            provider="qoder",
            model_usage={
                "lite": {
                    "inputTokens": 1,
                    "costUSD": Decimal("0.001"),
                    "raw_event": {"secret": secret},
                    "hidden_reasoning": secret,
                }
            },
        )
        dumped = json.dumps(evidence)
        self.assertNotIn(secret, dumped)
        self.assertEqual(
            set(evidence["records"][0]),
            {
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "provider_cost_microusd",
                "source",
                "completeness",
            },
        )

    def test_invalid_numerics_degrade_to_null(self):
        token_cases = [-1, 2**53, True, 1.5, Decimal("1.5"), "5", None]
        for token in token_cases:
            with self.subTest(token=token):
                evidence = normalize_usage_evidence(
                    provider="qoder",
                    model_usage={"m": {"inputTokens": token, "outputTokens": 1}},
                )
                record = evidence["records"][0]
                self.assertIsNone(record["input_tokens"])
                self.assertEqual(record["output_tokens"], 1)
                self.assertEqual(record["completeness"], "partial")
        # 边界值本身合法。
        evidence = normalize_usage_evidence(
            provider="qoder",
            model_usage={"m": {"inputTokens": MAX_TOKEN_VALUE, "outputTokens": 0}},
        )
        self.assertEqual(
            evidence["records"][0]["input_tokens"], MAX_TOKEN_VALUE
        )

    def test_model_key_validation_drops_invalid_records(self):
        valid = {"inputTokens": 1}
        invalid_keys = [
            "",
            "   ",
            " lite",
            "lite ",
            "x" * 257,
            "lite\nx",
            "\x01lite",
        ]
        model_usage = {key: dict(valid) for key in invalid_keys}
        model_usage["ok-model"] = dict(valid)
        evidence = normalize_usage_evidence(provider="qoder", model_usage=model_usage)
        self.assertIsNotNone(evidence)
        self.assertEqual(len(evidence["records"]), 1)
        self.assertEqual(evidence["records"][0]["model"], "ok-model")

    def test_all_invalid_keys_omits_evidence(self):
        evidence = normalize_usage_evidence(
            provider="qoder", model_usage={" bad ": {"inputTokens": 1}}
        )
        self.assertIsNone(evidence)

    def test_missing_or_empty_model_usage_omits_evidence(self):
        for model_usage in (None, {}, [], "not-a-dict", 5):
            with self.subTest(model_usage=model_usage):
                self.assertIsNone(
                    normalize_usage_evidence(
                        provider="qoder", model_usage=model_usage
                    )
                )

    def test_multi_model_top8_sorted_stable(self):
        model_usage = {
            f"m{i:02d}": {"inputTokens": i, "outputTokens": 0}
            for i in reversed(range(10))
        }
        evidence = normalize_usage_evidence(provider="qoder", model_usage=model_usage)
        self.assertEqual(len(evidence["records"]), MAX_RECORDS)
        models = [record["model"] for record in evidence["records"]]
        # 按 (provider, model) canonical sort 后截取前 8 条。
        self.assertEqual(models, ["m00", "m01", "m02", "m03", "m04", "m05", "m06", "m07"])
        self.assertNotIn("m08", models)
        self.assertNotIn("m09", models)

    def test_aggregate_cost_overflow_degrades_to_null(self):
        # 8 条记录各 9e12 USD = 9e18 microusd（单条 <= 2^63-1），
        # 总和 7.2e19 越界 -> 逐条降最大 cost 直至总和可接受。
        per_record = Decimal("9000000000000")
        model_usage = {
            f"m{i}": {
                "inputTokens": 1,
                "outputTokens": 1,
                "cacheReadInputTokens": 1,
                "cacheCreationInputTokens": 1,
                "costUSD": per_record,
            }
            for i in range(8)
        }
        evidence = normalize_usage_evidence(provider="qoder", model_usage=model_usage)
        records = evidence["records"]
        self.assertEqual(len(records), 8)
        total = sum(
            record["provider_cost_microusd"] or 0 for record in records
        )
        self.assertLessEqual(total, MAX_COST_MICROUSD)
        nulled = [
            record for record in records if record["provider_cost_microusd"] is None
        ]
        self.assertGreaterEqual(len(nulled), 1)
        # cost 被降级的记录必须重推 completeness -> partial，不得残留 complete。
        for record in nulled:
            self.assertEqual(record["completeness"], "partial")
        kept = [
            record for record in records if record["provider_cost_microusd"] is not None
        ]
        self.assertTrue(kept)
        for record in kept:
            self.assertEqual(record["completeness"], "complete")
        _assert_json_clean(self, evidence)

    def test_unknown_usage_evidence_zcode_shape(self):
        evidence = unknown_usage_evidence(provider="zcode")
        self.assertEqual(evidence["contract_version"], 1)
        self.assertEqual(len(evidence["records"]), 1)
        record = evidence["records"][0]
        self.assertEqual(record["provider"], "zcode")
        self.assertIsNone(record["model"])
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "provider_cost_microusd",
        ):
            self.assertIsNone(record[field])
        self.assertEqual(record["source"], "unknown")
        self.assertEqual(record["completeness"], "unknown")
        _assert_json_clean(self, evidence)


if __name__ == "__main__":
    unittest.main()
