"""memebot.live — the autonomous power-law tail-rider (config #1), paper-first.

This package ports the locked strategy (config #1) from the research scripts
(`scripts/stage38_ansem_dependence.py::sim`) into a live, candle-driven state
machine plus the surrounding engine (state store, risk governor, price feed,
signal listener, executor, monitor, orchestrator).

Design principle: the live engine MIRRORS the backtest. Feeding a token's real
candle series through `strategy.TailRider` must reproduce `sim`'s realized
multiple (ANSEM ~= 197.6x). That equivalence is the first correctness gate —
see `tests/test_strategy_equivalence.py`.

Guardrail (never violate): size as a tail bet (small fixed stake), never present
as reliable income, keep the honest caveats visible.
"""
