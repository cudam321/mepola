# RESEARCH.md — the full story

This document is the backbone of the project: the question we set out to answer, the
measurement discipline we used, every angle we tested (and every number), the six seductive
false positives we caught along the way, and the one configuration that survived to become
the live system. Everything here is reproducible from the `scripts/` directory against a
corpus of your own channel's calls.

---

## 0. TL;DR

Memecoin returns are a **power law**. The tail is real, enormous, and *measurable* — our
corpus contains a genuine ~700× runner, and the top 1% of tokens carries essentially all of
the aggregate gain. But per-trade expected value for an outside participant who *follows*
calls is **structurally ≤ 1**: by the time a call is public you are, on median, ~2.65× above
the callers' own entry, and no field observable at entry separates the future tail from the
graveyard (every feature we tested ≈ 0.50 rank-AUC).

The entire project is the study of the tension between those two facts:

> **The tail is real. Your seat at the table is late. Every strategy is a negotiation
> between surviving the bleed and still holding the ticket when the tail arrives.**

After ~11 exhaustively-tested angles (all NO-GO under our gate) and a 144-configuration exit
grid, one policy meaningfully beat the naive baselines out-of-sample: a **dip-entry
tail-rider** (config #1). It does *not* clear our strict statistical gate — nothing did — but
it is the best-shaped vehicle for holding tail exposure while cutting the bleed, and it is
what the live system runs, deliberately, at small size. Section 6 gives the honest numbers.

---

## 1. The question and the frame

**Question:** does following a Telegram memecoin call channel make money for the follower?

**Frame:** this is a power-law domain, so the usual metrics lie. Means are dominated by one
or two observations; win rates are irrelevant; a backtest that resamples the tail even once
prints fantasy. We therefore adopted a power-law-native toolkit from the start:

- **Hill alpha** on the return distribution (alpha < 1 ⇒ the mean is undefined — a warning,
  not a feature);
- **drop-top-k** means (what happens to your edge when the best 1–3 tokens are removed);
- **bootstrap CI lower bounds**, never point estimates;
- **fixed-fraction log-growth** (E[log(1 + f(m−1))]) — the Kelly-view of whether a bankroll
  compounds or dies;
- **single-pass chronological bankroll simulation** — no resampling, gas subtracted per
  trade, bankruptcy short-circuits the run.

**The trading unit** is the *first actionable BUY call per mint* — 1,263 first-calls in the
final un-truncated corpus (later refreshed to 1,371). Every dead token stays in the
denominator. Every price series is re-fetched to *today*, so late revivals and slow deaths
are priced, not truncated away.

**The fill model is deliberately unflattering:** enter at the worst (max-high) price over a
~90-second reaction window after the decision moment, plus 1.5% slippage; stops fill with a
−5% haircut; every trade pays fixed round-trip gas ($0.60); a token with no exit liquidity
scores as a total loss.

---

## 2. The measurement bar

A strategy is a **GO** only if an *executable* policy — real latency, real slippage, full
denominator, at a realistic liquidity cap (≤50×) — clears **all four**:

| gate | meaning |
|---|---|
| `ci_lo > 1` | bootstrap 95% CI *lower bound* on the per-trade multiple is above break-even |
| `drop3 > 1` | still profitable after deleting the top-3 winners — no single-token mirages |
| `f2_logG > 0` | positive log-growth at a 2% fixed fraction — survives sequencing |
| `$500 grows` | a single-pass chronological $500 bankroll ends above $500 — no resampling |

Point EV is never gated on. Max-over-policies is never gated on (that's a selection
artifact). Non-executable policies (e.g. "hold to the end of time") are recorded as upper
bounds only. This bar is what killed six spectacular-looking results (section 5) that a
normal backtest would have shipped.

---

## 3. The journey — every angle, every verdict

Chronological. Each angle was pushed until it either cleared the gate or its failure was
understood structurally. None cleared.

### 3.1 Follow the posts (the naive seat)
Buy when the channel posts, manage the exit. **Result:** you enter at a median **2.65×**
above the callers' own entry; every managed-exit policy ≈ **−20% per trade**. The channel's
edge is real *for the channel* — it does not survive the latency to your seat. **NO-GO.**

### 3.2 Copy the channel's smart money at their entry
Parse the channel's own "smart money" stats and copy those wallets. The only +EV policy was
a non-executable `buy_and_die` (hold everything to the end), and its gain was one token with
CI-low < 0. **NO-GO.**

### 3.3 Discover skilled wallets on-chain and copy them
Independently rank on-chain wallets by past returns, copy the top cohort in real time. Top
cohort loses **−17%/trade OOS as a copier** (−14% even when selecting directly on historical
*copier*-EV). Deep-dive: a wallet's big outliers do not repeat — **outlier-picking is a
property of tokens, not a persistent wallet skill.** Confirmed on the power-law re-test:
train-window max 6,316×, OOS max 99.6×, yet OOS copier EV −22% to −38% across *every* wallet
selection × horizon × exit policy. **NO-GO.**

### 3.4 Ride serial deployers' launches
If skilled deployers exist, buy their next launch at t=0. **Result: −77% in-sample, −83%
OOS** — you are buying the slot-0 snipe peak of tokens that die ~99% of the time. The worst
angle tested. **NO-GO.**

### 3.5 Token features / early intensity
Predict the tail from the token's first minutes (buy intensity, holder growth, …). Early
intensity **does** predict the 30-day tail (corr +0.35) — the tail is weakly predictable! —
but the best feature quintile is still −10%/−18% per trade, and the signal breaks OOS. Too
weak against the structural late-entry floor. **NO-GO.**

### 3.6 Channel power-law classifier (artifact #1: lookahead)
Multi-feature classifier on first-hour post-call momentum, in the power-law frame
(Hill-alpha + optimal-f). Looked like a clean GO: OOS top-decile mean 1.63, CI-lo 1.28,
survives liquidity caps. **Caught:** features were computed over [t, t+1h] with entry at
t+60s — lookahead. The honest version (enter *after* the observation window, +3% slip)
collapses to **0.80**. Observing momentum = waiting = entering after the move = the same
late-entry floor. **NO-GO.**

### 3.7 Survivor second-leg (artifacts #2, #3)
Only trade tokens that already *graduated* (survivorship as a risk filter), enter calm,
post-graduation. The filter is the single biggest risk lever we found anywhere — it lifts
the floor from −40% to ~−14%/trade — but never crosses break-even (grad+24h realistic fill:
mean 0.86; best quality-selection decile 0.83). A pullback-buy rule inside this regime
looked *spectacular* (mean 4.4, P(profit) 100%) and was caught twice over: Hill alpha < 1
with 10^16 Monte-Carlo medians (**undefined-mean lottery**, artifact #2) and entry priced at
the close of the candle whose *low* touched the trigger — an unbuyable wick
(**bottom-catching**, artifact #3). Realistic next-candle fill → 0.53. **NO-GO.**

### 3.8 The un-truncated full re-test (closing a real data hole)
A real ~700×-from-entry runner surfaced *after* our corpus cutoff — meaning the earlier
NO-GO had run on truncated data that couldn't see the biggest tail event. This mattered: if
the conclusion flipped with the tail included, everything above was wrong. So
`stage14_untruncated.py` re-priced **all 1,263 first-calls to today**, full denominator,
tail fully included, and re-ran 40+ exit policies. Result: every executable policy still
sub-1 (trail-exit 0.59×; pure hold 0.75× *even uncapped*; 76% total-loss rate;
alpha(hold) = 0.84; drop-top-3 → 0.28; $500 → $0 in both time halves). The tail event was
captured at MFE 696.8× — and every executable policy exits it early or rides the other 99%
to zero. Four adversarial refutation agents attacked the result (denominator completeness,
dead-rate verification, fill fairness, policy coverage) and all failed to break it. The
+EV lives only in perfect-foresight MFE (2.7–3×). **NO-GO, now bulletproof.**

### 3.9 TP/SL grids, momentum, re-entry, flow-confirmation, meta-models, scalping, behavioral patterns
Stages 15–31. Every classical policy family, gated identically. None clears. Individual
write-ups live in the script headers. **NO-GO across the board.**

### 3.10 The early seat (artifacts #5, #6 — and #1 again, compounded)
Re-anchor entry at the smart money's *own* entry time (reconstructed from the channel's
"time since entry" field). Flashed the project's only full-gate GO: mean 1.77×, CI-lo 1.26,
drop3 1.32, $500 → $4,001. Killed by adversarial review as a *compound* artifact:
**lookahead** (the "time since entry" field exists only in a post published ~4h later; 168
of the tokens are known only because the channel later flagged them as winners),
**single-regime** (the field only exists in one posting format → all 176 trades in one
10-day window, zero OOS), and **fill-fragility** (the CI-lo needed 1.5% slip into $75k-MC
pools). The within-sample timing effect is real (2.53× lift) — but the only real-time way to
occupy that seat is on-chain wallet copying, which is angle 3.3 = −17%/trade. **NO-GO.**

### 3.11 Regime gating
Gate trading on the trailing-14-day outcome of prior *resolved* calls (only trade in hot
regimes). Either does nothing (loose threshold) or filters out the tail event itself (tight
threshold) → 0.785. No setting lifts drop3 above 1. **NO-GO — the last untested knob.**

**Meta-conclusion of the journey:** all three seats at this table — the follower, the
perfect copier, the real-time copier — are structurally negative for an outsider. The
power-law tail is real, bigger than a truncated backtest can even see, and it belongs to
whoever is holding *before* the attention event. What remained was a different question:
not "where is the edge?" but **"given no edge, what is the best-shaped vehicle for holding
tail exposure anyway?"** That question has a measurable answer.

---

## 4. The exit-policy grid — finding the best-shaped vehicle

`stage37_grid.py` swept the full 5-parameter exit family over the fresh corpus:
**dip × stop × first-TP × first-sell-fraction × re-entry** — 144 configurations, each
simulated with the pessimistic fill model, uncapped, on 1,371 first-calls.

The winner ("config #1") and its shape:

| parameter | value | why it wins |
|---|---|---|
| entry | **−50% dip** from signal price, ≤48h window, else skip | never chase; the median call bleeds after posting, so the dip entry both halves your basis and *filters out* tokens that never come back to you |
| stop | **−30% from entry** (`sl=0.7`), active only until secured | cuts the losers' bleed fast; because entry was already a −50% dip, the rare tail token has typically bottomed and survives the stop |
| secure | at **3×**, sell 33% — stake recovered — then **remove the stop** | converts the position to house money exactly once |
| ride | sell 25% of remainder at **6× / 12× / 24× / 48×**, then ×3 steps (144×, 432×, …) | harvests the middle of the tail while the moonbag stays exposed to the extreme |
| re-entry | **never** | every re-entry variant tested worse |

A note on `sl` semantics, preserved because it almost shipped wrong: `sl` is the stop
*level* as a fraction of entry (0.7 = stop at 0.7× entry = −30%), not the drawdown. The two
readings give *different configs* (−30% vs −70%) with different OOS results, and the
mislabel was caught only by challenge-and-verify (`verify_sl_semantics.py` — only `sl=0.7`
reproduces #1's OOS mean 1.387 / drop3 0.787 / tail 197.6×). Verify, don't assume.

### Per-token lifecycle (the live state machine)

```
WATCHING ── price ≤ 0.5×signal within 48h ──► ENTERED ── 3× hit ──► SECURED ──► RIDING ──► EXITED
   │                                             │  (sold 33%, stop off)  (25% of rem at
   └── 48h elapses, no dip ──► EXPIRED (skip)    │                        6/12/24/48×, ×3…)
                                                 └── price ≤ 0.7×entry (pre-secure) ──► STOPPED
```

---

## 5. The six artifacts — a field guide to false positives

Every "spectacular" result this project ever produced was one of these. They are the reason
the measurement bar exists. Check any new result against all six before believing it.

1. **Lookahead** — the decision uses data not available at the entry timestamp (a field from
   a later post, a feature computed over the entry window). *Tell:* perfect-foresight MFE
   leaking into an "executable" number.
2. **Undefined-mean lottery** — Hill alpha < 1; the mean is one or two observations wearing
   a distribution as a costume. *Tell:* drop-top-k collapses it; Monte-Carlo medians are
   absurd (10^16).
3. **Bottom-catching** — entry priced at the close of the candle whose *low* touched the
   trigger: you bought an unbuyable wick. *Tell:* dies under a realistic next-candle fill.
4. **Resampling-with-replacement** — a bankroll simulation that re-draws the rare tail many
   times. *Fix:* single-pass, chronological, no replacement.
5. **Single-regime / zero-OOS** — the "signal" exists only in one short window or one
   posting format. *Tell:* check the date span of the selected subset.
6. **Fill-fragility** — a GO whose CI lower bound depends on fantasy slippage into thin
   microcap pools. *Tell:* re-gate under realistic/worse slip and watch it die.

Rule: **spectacular = artifact, until it survives all six.** Never ship a GO from one
script's output — attack it adversarially first. That discipline caught all six of ours.

---

## 6. Config #1 out-of-sample — the honest numbers

Two decisive scripts, fresh corpus, un-truncated pricing, tail event fully included, uncapped.

### 6.1 Tail-dependence (`stage38_ansem_dependence.py`)

OOS (n=345), config #1:

| metric | full OOS | remove the single best token |
|---|---|---|
| per-trade mean | **1.383** | 0.813 |
| drop-top-3 | 0.786 | 0.775 |
| bootstrap ci_lo | 0.770 | 0.757 |
| win rate | 10% | 10% |

`drop3 < 1` and `ci_lo < 1` → config #1 **fails the strict gate** whether the tail token is
in or out. Split the OOS into four equal windows and only the window containing the tail
event is green ($500 → $1,861, mean 3.202); the other three lose (0.771 / 0.782 / 0.785).
This is what a power law looks like from the inside: **the edge is one token.**

### 6.2 Size-fragility (`stage39_window_foresight.py`)

Trading the full OOS straight through (no window-picking, no foresight), $500 start:

| per-trade size | full OOS | full OOS, tail token removed |
|---|---|---|
| 0.25% | $635 | $425 |
| 0.50% | **$717** | $362 |
| 1.00% | **$774** | $261 |
| 2.00% | $663 | $134 |
| 5.00% | $186 | $17 |
| $10 fixed | **$1,815** | $0 |
| $25 fixed | $0 | $0 |

The two dynamics in one table. Downward: sizing up amplifies the bleed faster than the tail
can repay it — the strategy **dies above ~$10–25/trade** on a $500-class bankroll, no matter
what the tail does. Rightward: remove the one tail token and every size loses. The policy's
job is to keep you solvent and exposed until a tail arrives; it cannot manufacture one.

### 6.3 What this means

- Config #1 is **meaningfully better-shaped than naive alternatives**: straight-through OOS
  at 0.5–1% sizing ends +43–55% while the naive follow-the-post seat loses 20% a trade. The
  −30% stop cuts the bleed; the −50% dip entry keeps the winner.
- It is **not a statistical edge**: the gain is tail-concentrated, the CI includes loss, and
  long tail-less stretches are the *expected* regime (win rate ~10%, most positions bleed).
- It is **size-fragile by measurement, not by opinion** — which is why the live system
  enforces a hard cap and ships with a small default stake. Sizing within that envelope is a
  personal risk-tolerance decision; the envelope itself is measured.

---

## 7. The reusable harness (for reproducing all of this)

**`src/memebot/analysis/exit_sim.py`** — `ExitPolicy(name, tp_ladder, stop_mult, trail_pct,
trail_arm_mult, time_stop_h)`; `simulate_exit(series, fill, t, policy)` → realized multiple.
Pessimistic per-candle resolution: the stop (bar low) is checked *before* take-profits (bar
high); ambiguous bars resolve as stops; the unsold remainder liquidates at the last close
(≈ 0 for dead tokens).

**`scripts/stage14_untruncated.py`** (imported as a library by later stages) —
`series_to_today()` (minute candles near entry, coarser to now — the un-truncation),
`entry_fill()` (90s max-high +1.5%), `mean_ci()` (bootstrap), `drop_top()`,
`fixed_f_growth()`, `single_pass_bankroll()` (chronological, gas-deducted, bankruptcy-aware,
**no resampling**), `hill_alpha()`, and the four-part gate in `main.passes`.

**Data:** Jupiter charts API (keyless) for OHLCV via a disk cache (`CachedPriceClient`);
Jupiter lite API for live prices/resolution/safety checks; GeckoTerminal/DexScreener as
fallbacks. **Corpus:** `{channel, title, messages[]}` JSON → parser → `Signal` →
`first_call_per_mint()` = the trading unit.

**Live-vs-research integrity:** the live engine's strategy machine is pinned
floating-point-exact to the research sim by `tests/test_strategy_equivalence.py`, and all 18
shadow-lab challenger strategies are pinned to their research oracles in
`tests/test_shadow.py`. What the research measured is what the engine trades.

---

## 8. Conclusions

1. **The power law is real.** Verified tails to ~700× from a follower's entry price exist in
   a single channel's corpus; the top-1% of tokens carries essentially all aggregate gain.
2. **The tail is not harvestable as an edge by an outside participant.** Eleven angles, three
   seats (follower / perfect copier / real-time copier), one 144-config grid: nothing clears
   a gate that demands the profit survive the removal of three tokens.
3. **The best available vehicle is shape, not edge:** dip entry (never chase), a tight stop
   that dies at 3× (bleed control), a ladder that never fully exits (tail exposure), small
   size (survival). That is config #1, and it is what this repository runs.
4. **Measurement discipline is the product.** The six-artifact catalog and the four-part
   gate caught every false GO this project generated. If you fork this to test your own
   channel, keep the bar. The moment you gate on point EV, the power law will print you a
   beautiful lie.
