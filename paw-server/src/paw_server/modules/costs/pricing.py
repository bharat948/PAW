"""Exact, versioned standard text pricing. Reserve a model's entire context bound."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

MILLION = Decimal(1_000_000)
UNIT = Decimal("0.000000001")


class CostError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Rate:
    version: str
    model: str
    input_rate: Decimal
    output_rate: Decimal
    cached_rate: Decimal
    context: int
    max_output: int
    served_models: tuple[str, ...]

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Rate":
        if any(
            r.get(k) is None
            for k in (
                "cached_input_per_mtok",
                "context_tokens",
                "max_output_tokens",
                "served_models",
            )
        ):
            raise CostError("pricing_unavailable")
        return cls(
            r["version"],
            r["model"],
            Decimal(r["input_per_mtok"]),
            Decimal(r["output_per_mtok"]),
            Decimal(r["cached_input_per_mtok"]),
            r["context_tokens"],
            r["max_output_tokens"],
            tuple(r["served_models"]),
        )

    def cost(self, inputs: int, outputs: int, cached: int = 0) -> Decimal:
        if any(type(n) is not int or n < 0 for n in (inputs, outputs, cached)) or cached > inputs:
            raise CostError("invalid_usage")
        return (
            (
                self.input_rate * (inputs - cached)
                + self.cached_rate * cached
                + self.output_rate * outputs
            )
            / MILLION
        ).quantize(UNIT, rounding=ROUND_CEILING)

    def reserve(self, output: int) -> Decimal:
        if type(output) is not int or not 1 <= output <= self.max_output:
            raise CostError("unsupported_output_bound")
        return self.cost(self.context, output)
