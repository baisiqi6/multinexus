"""Bounded managed ``usage_evidence`` V1 producer normalizer (MultiNexus #21).

共享、纯函数、bounded：把 provider 已公开的 usage 字段归一化成 Coordinate #12
的 ``usage_evidence`` V1 contract。只做 allowlist 与单位转换，不累计、不持有
policy、不复制 Coordinate 的 DB/validation authority。

Contract V1（与 Coordinate ``usage_evidence.py`` 逐字段一致）：

    {
      "contract_version": 1,
      "records": [
        {
          "provider": "qoder",
          "model": "lite",
          "input_tokens": 0,
          "output_tokens": 0,
          "cache_read_tokens": 0,
          "cache_write_tokens": 0,
          "provider_cost_microusd": 0,
          "source": "provider_reported",
          "completeness": "complete"
        }
      ]
    }

不变量：

- 只识别 camelCase allowlist：``inputTokens`` / ``outputTokens`` /
  ``cacheReadInputTokens`` / ``cacheCreationInputTokens`` / ``costUSD``；
  Grok 的 ``modelCalls`` 等其余字段一律不读。
- ``costUSD`` 只接受 JSON integer（int）或 Decimal（stdout 入口必须使用
  ``json.loads(..., parse_float=Decimal)`` 的产物），精确转整数 microusd；
  输出只含 int/str/None/容器，Decimal 绝不残留。
- 数值非法/负数/bool/float/越界一律降为 ``null``；model key 非法整条丢弃，
  不劣化为 ``model=null``。
- 合法 records 按 ``(provider, model or "")`` 稳定排序、最多 8 条（JSON
  object key 唯一性天然保证同一 model 不重复）；
  aggregate cost 超 signed 64-bit 时把最大 cost 降 ``null``（并重推
  completeness）直至总和可接受，保证 Coordinate typed validator 不会拒绝。
- Qoder/Grok 无合法 records 时返回 ``None``，调用方应省略 ``usage_evidence``；
  ZCode 走 ``unknown_usage_evidence`` 生成显式 unknown 单条。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

CONTRACT_VERSION = 1
MAX_RECORDS = 8
MAX_TOKEN_VALUE = 2**53 - 1
MAX_COST_MICROUSD = 2**63 - 1
MAX_MODEL_LEN = 256

_MICROUSD_SCALE = 6
_MAX_COST_USD_ADJUSTED = 12  # floor(log10((2**63 - 1) / 1_000_000))

# camelCase allowlist: provider field -> contract field.
_TOKEN_FIELDS: dict[str, str] = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_tokens",
    "cacheCreationInputTokens": "cache_write_tokens",
}
_COST_FIELD = "costUSD"

_NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "provider_cost_microusd",
)


def _valid_model_key(value: Any) -> str | None:
    """Coordinate model 谓词：非空、无首尾空白、<=256、无控制字符。

    非法 key 返回 ``None``，调用方整条丢弃该 record，绝不劣化为
    ``model=null``。
    """
    if not isinstance(value, str) or isinstance(value, bool):
        return None
    if value != value.strip() or not value:
        return None
    if len(value) > MAX_MODEL_LEN:
        return None
    if any(ord(ch) < 32 for ch in value):
        return None
    return value


def _to_token(value: Any) -> int | None:
    """token 只接受非负 int 且 <= 2^53-1；float/bool/Decimal/str 一律降 null。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_TOKEN_VALUE:
        return None
    return value


def _to_cost_microusd(value: Any) -> int | None:
    """USD -> 整数 microusd 精确转换；只接受 int（JSON integer）或 Decimal。

    Decimal 转换直接读取 decimal tuple，不经过 context-bound arithmetic；结果
    非整数（多于 6 位有效小数）、负数、非有限值或越界 signed 64-bit 一律降
    ``null``。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        micro = value * 10**6
        return micro if micro <= MAX_COST_MICROUSD else None
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0:
            return None
        if value.is_zero():
            return 0
        # Context-free magnitude guard: values above 10^13 USD can never fit
        # signed 64-bit microusd. The exact boundary remains the final pure-int
        # comparison below.
        if value.adjusted() > _MAX_COST_USD_ADJUSTED:
            return None

        _, digits, exponent = value.as_tuple()
        scale_exponent = exponent + _MICROUSD_SCALE
        if scale_exponent >= 0:
            micro_digits = (*digits, *((0,) * scale_exponent))
        else:
            fractional_digits = -scale_exponent
            if fractional_digits > len(digits):
                return None
            trailing = digits[-fractional_digits:]
            if any(digit != 0 for digit in trailing):
                return None
            micro_digits = digits[:-fractional_digits]
        micro_int = int("".join(str(digit) for digit in micro_digits) or "0")
        return micro_int if micro_int <= MAX_COST_MICROUSD else None
    return None


def _derive_completeness(*, source: str, numerics: tuple[int | None, ...]) -> str:
    """Coordinate authority 的 null-pattern 推导在 producer 侧的同一文字镜像。

    五个数值字段全部非空 -> ``complete``；``source=unknown`` 且全部为空 ->
    ``unknown``；其余合法组合 -> ``partial``。
    """
    all_known = all(value is not None for value in numerics)
    all_null = all(value is None for value in numerics)
    if all_known:
        return "complete"
    if source == "unknown" and all_null:
        return "unknown"
    return "partial"


def _degrade_aggregate_cost(records: list[dict[str, Any]]) -> None:
    """aggregate cost 超 signed 64-bit 时，把最大 cost 降 null 直至总和可接受。

    cost 降级会改变 null pattern，因此每个受影响 record 的 completeness 都要
    按 Coordinate 规则重推（complete -> partial），不允许残留过期标签。
    """
    while True:
        total = 0
        for record in records:
            cost = record["provider_cost_microusd"]
            if cost is not None:
                total += cost
        if total <= MAX_COST_MICROUSD:
            return
        heaviest = max(
            (r for r in records if r["provider_cost_microusd"] is not None),
            key=lambda r: r["provider_cost_microusd"],
        )
        heaviest["provider_cost_microusd"] = None
        heaviest["completeness"] = _derive_completeness(
            source=heaviest["source"],
            numerics=tuple(heaviest[field] for field in _NUMERIC_FIELDS),
        )


def normalize_usage_evidence(*, provider: str, model_usage: Any) -> dict[str, Any] | None:
    """把 provider stdout JSON 的 ``modelUsage`` 归一化为 bounded V1 block。

    ``provider`` 由调用方传入硬编码常量（``"qoder"`` / ``"grok"``）。
    返回 ``None`` 表示无合法 records（``modelUsage`` 缺失/为空或全部 key
    非法），调用方应省略 ``usage_evidence``；否则返回完整 V1 block，只含
    int/str/None/容器，可直接 JSON 序列化。
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    records: list[dict[str, Any]] = []
    for model_key, raw in model_usage.items():
        model = _valid_model_key(model_key)
        if model is None:
            continue  # 非法 model key：整条丢弃，不改成 null
        usage = raw if isinstance(raw, dict) else {}
        numerics: dict[str, int | None] = {
            field: _to_token(usage.get(camel))
            for camel, field in _TOKEN_FIELDS.items()
        }
        numerics["provider_cost_microusd"] = _to_cost_microusd(usage.get(_COST_FIELD))
        records.append(
            {
                "provider": provider,
                "model": model,
                **numerics,
                "source": "provider_reported",
                "completeness": _derive_completeness(
                    source="provider_reported",
                    numerics=tuple(numerics.values()),
                ),
            }
        )
    if not records:
        return None
    records.sort(key=lambda r: (r["provider"], r["model"] or ""))
    records = records[:MAX_RECORDS]
    _degrade_aggregate_cost(records)
    return {"contract_version": CONTRACT_VERSION, "records": records}


def unknown_usage_evidence(*, provider: str) -> dict[str, Any]:
    """无 usage contract 的 provider（ZCode）的显式 unknown 单条记录。

    ``source=unknown`` 时五个数值字段必须全部为 ``null``（Coordinate 约束），
    ``completeness=unknown`` 由同一推导规则得出。
    """
    numerics: tuple[int | None, ...] = (None,) * len(_NUMERIC_FIELDS)
    return {
        "contract_version": CONTRACT_VERSION,
        "records": [
            {
                "provider": provider,
                "model": None,
                **{field: None for field in _NUMERIC_FIELDS},
                "source": "unknown",
                "completeness": _derive_completeness(
                    source="unknown", numerics=numerics
                ),
            }
        ],
    }
