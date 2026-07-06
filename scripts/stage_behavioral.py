#!/usr/bin/env python3
"""Behavioral / cadence meta-features on @your_channel (fresh corpus).

Mine the MESSAGE STREAM's behavior (not token stats), all observable at entry:
  time-of-day / day-of-week; first-after-quiet-gap vs spam-burst; recent call
  cadence (calls/hour); signal-TYPE + preceding type; whether the token was
  teased before its first BUY.

Test each feature's AUC vs WIN, build an OOS selection from the best, run the
full traded-set gate on it. Strict no-lookahead: every feature uses only
messages with (posted_at,id) <= the call being scored.
"""
from __future__ import annotations
import csv, sys
from datetime import timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa
import stage14_untruncated as S  # noqa: mean_ci, drop_top, fixed_f_growth, single_pass_bankroll, cap_mults

CAP = 50.0

# ---------- signal type from header (reliable; text-scan fallback) ----------
def header(t):
    for line in t.splitlines():
        line = line.strip()
        if line:
            return line
    return ""

def stype(t):
    h = header(t).upper()
    if "PROFIT" in h: return "profit"
    if "BUY MORE" in h: return "buymore"
    if "CTO" in h: return "cto"
    if "HOLDING" in h: return "holding"
    if "MAIN" in h: return "main"
    if "VOLUME" in h: return "volume"
    if "SMART MONEY ALERT" in h: return "sm_generic"
    T = t.upper()
    if "MAIN SIGNAL" in T: return "main"
    if "VOLUME SIGNAL" in T: return "volume"
    if "HOLDING SIGNAL" in T: return "holding"
    return "compact"  # the later "Token: #X / Grad:" format

def is_compact(t):
    return header(t).startswith("Token:")

# ---------- load ----------
sigs = load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))
sigs_sorted = sorted(sigs, key=lambda s: (s.posted_at, s.message_id))
# stream arrays for fast windowed counts (ALL messages)
stream_ts = np.array([s.posted_at.timestamp() for s in sigs_sorted])

fc = sorted(first_call_per_mint(sigs), key=lambda s: (s.posted_at, s.message_id))
fc_ts = np.array([s.posted_at.timestamp() for s in fc])

# outcomes
out = {}
with open(ROOT / "runs" / "stage_fresh_pertoken.csv") as f:
    for r in csv.DictReader(f):
        out[r["mint"]] = (float(r["moon"]), float(r["hold"]), float(r["mfe"]))

# prev-message type lookup: for each first-call, the type of the msg immediately before it
def count_before(ts_arr, t, hours):
    lo = t - hours * 3600.0
    # messages strictly before t within window
    return int(np.sum((ts_arr >= lo) & (ts_arr < t)))

rows = []
for i, s in enumerate(fc):
    t = s.posted_at.timestamp()
    dt = s.posted_at.astimezone(timezone.utc)
    # previous message (any) before this call
    prev_ts = stream_ts[stream_ts < t]
    gap_prev_msg = (t - prev_ts.max()) / 60.0 if len(prev_ts) else 1e9
    # previous FIRST-CALL before this one
    pf = fc_ts[fc_ts < t]
    gap_prev_fc = (t - pf.max()) / 60.0 if len(pf) else 1e9
    # cadence: messages (all) in prior windows
    m1 = count_before(stream_ts, t, 1)
    m6 = count_before(stream_ts, t, 6)
    m24 = count_before(stream_ts, t, 24)
    # first-calls in prior windows
    f1 = count_before(fc_ts, t, 1)
    f6 = count_before(fc_ts, t, 6)
    f24 = count_before(fc_ts, t, 24)
    # spam burst: messages in the last 10 minutes
    burst10 = count_before(stream_ts, t, 10.0 / 60.0)
    # prev message type
    idx_before = np.where(stream_ts < t)[0]
    prev_type = stype(sigs_sorted[idx_before[-1]].raw_text) if len(idx_before) else "none"
    mo, ho, mf = out.get(s.mint, (0.0, 0.0, 0.0))
    rows.append(dict(
        mint=s.mint, t=t, i=i,
        hour=dt.hour, dow=dt.weekday(), weekend=1 if dt.weekday() >= 5 else 0,
        gap_prev_msg=gap_prev_msg, gap_prev_fc=gap_prev_fc,
        msgs_1h=m1, msgs_6h=m6, msgs_24h=m24,
        fc_1h=f1, fc_6h=f6, fc_24h=f24,
        burst10=burst10,
        first_after_gap=1 if gap_prev_msg > 60 else 0,
        stype=stype(s.raw_text), prev_type=prev_type,
        compact=1 if is_compact(s.raw_text) else 0,
        moon=mo, hold=ho, mfe=mf,
    ))

print(f"built {len(rows)} first-call rows")

# ---------- AUC helper (Mann-Whitney) ----------
def auc(x, y):
    x = np.asarray(x, float); y = np.asarray(y, int)
    pos = x[y == 1]; neg = x[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(x); ranks = np.empty(len(x)); ranks[order] = np.arange(1, len(x) + 1)
    # tie-correct via average ranks
    from scipy.stats import rankdata
    r = rankdata(x)
    rpos = r[y == 1].sum()
    a = (rpos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))
    return float(a)

NUMERIC = ["hour", "dow", "weekend", "gap_prev_msg", "gap_prev_fc",
           "msgs_1h", "msgs_6h", "msgs_24h", "fc_1h", "fc_6h", "fc_24h",
           "burst10", "first_after_gap", "compact"]

def win_label(rws, thr=1.0, key="moon"):
    return np.array([1 if r[key] > thr else 0 for r in rws], int)

def report_auc(rws, tag, win_thr=1.0):
    y = win_label(rws, win_thr, "moon")
    print(f"\n--- AUC vs WIN(moon>{win_thr})  [{tag}]  n={len(rws)}  base win-rate={y.mean():.3f} ---")
    res = {}
    for feat in NUMERIC:
        x = np.array([r[feat] for r in rws], float)
        a = auc(x, y)
        res[feat] = a
        print(f"   {feat:16s} AUC={a:.3f}  |dev|={abs(a-0.5):.3f}")
    return res

print("=" * 90)
print("FULL-SAMPLE AUCs (in-sample discovery)")
res_full = report_auc(rows, "full", 1.0)

# categorical: per-type win-rate and mean moon
def cat_report(rws, key):
    from collections import defaultdict
    d = defaultdict(list)
    for r in rws:
        d[r[key]].append(r)
    print(f"\n--- {key}: win-rate(moon>1) & mean moon (cap {CAP}x) ---")
    for k, v in sorted(d.items(), key=lambda kv: -len(kv[1])):
        mo = S.cap_mults([r["moon"] for r in v], CAP)
        wr = np.mean([1 if r["moon"] > 1 else 0 for r in v])
        print(f"   {k:12s} n={len(v):4d}  win={wr:.3f}  mean_moon={np.mean(mo):.3f}  median={np.median([r['moon'] for r in v]):.3f}")

cat_report(rows, "stype")
cat_report(rows, "prev_type")

# within-compact AUCs (removes the format regime confound)
comp = [r for r in rows if r["compact"] == 1]
print("=" * 90)
print("WITHIN-COMPACT AUCs (format held constant; the honest cadence test)")
res_comp = report_auc(comp, "compact-only", 1.0)

# ---------- GATE ----------
def gate(mults, times, cap=CAP, tag=""):
    mults = list(mults); times = np.asarray(times, float)
    cm = S.cap_mults(mults, cap)
    m, lo, hi = S.mean_ci(cm)
    d1 = S.drop_top(cm, 1); d3 = S.drop_top(cm, 3)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, times, f=0.02, cap=float("inf"))
    passed = (lo > 1 and d3 > 1 and g2 > 0 and bank > 500)
    print(f"   [{tag}] n={len(cm)} mean={m:.3f} CI=({lo:.3f},{hi:.3f}) drop1={d1:.3f} drop3={d3:.3f} "
          f"f2logG={g2:+.4f} $500->{bank:.0f}  {'PASS' if passed else 'FAIL'}")
    return dict(n=len(cm), mean=m, ci_lo=lo, ci_hi=hi, drop1=d1, drop3=d3, f2_logG=g2, bank=bank, passed=passed)

print("=" * 90)
print("BASELINE gate on the FULL traded set (no selection), cap 50x:")
gate([r["moon"] for r in rows], [r["t"] for r in rows], CAP, "ALL moon")
gate([r["hold"] for r in rows], [r["t"] for r in rows], CAP, "ALL hold")

# ---------- OOS selection ----------
# Rank features by |AUC-0.5| on TRAIN; select on test by the top feature's train-optimal
# direction+threshold. Also try a top-3 logistic combo. Two split regimes:
#   (A) time 70/30 (standard gate split; but late=100% compact -> format confound)
#   (B) within-compact temporal 60/40 (regime held constant -> the honest OOS)

def fit_threshold_select(train, test, feat, frac=0.5):
    """Pick direction on train (which tail wins more), keep the winning `frac` on test."""
    xt = np.array([r[feat] for r in train], float)
    yt = win_label(train)
    a = auc(xt, yt)  # a>0.5 => higher feat -> more wins
    hi_dir = a >= 0.5
    xs = np.array([r[feat] for r in test], float)
    thr = np.quantile([r[feat] for r in train], 1 - frac if hi_dir else frac)
    if hi_dir:
        keep = [r for r in test if r[feat] >= thr]
    else:
        keep = [r for r in test if r[feat] <= thr]
    return keep, a, hi_dir, thr

def logistic_combo_select(train, test, feats, frac=0.3):
    Xtr = np.array([[r[f] for f in feats] for r in train], float)
    ytr = win_label(train)
    mu = Xtr.mean(0); sd = Xtr.std(0) + 1e-9
    Xz = (Xtr - mu) / sd
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(max_iter=1000)
    clf.fit(Xz, ytr)
    Xte = (np.array([[r[f] for f in feats] for r in test], float) - mu) / sd
    p = clf.predict_proba(Xte)[:, 1]
    ptr = clf.predict_proba(Xz)[:, 1]
    thr = np.quantile(ptr, 1 - frac)
    keep = [r for r, pp in zip(test, p) if pp >= thr]
    return keep, clf

def run_oos(train, test, label):
    print(f"\n===== OOS SELECTION [{label}]  train={len(train)} test={len(test)} =====")
    # rank features by |AUC-0.5| on train
    yt = win_label(train)
    ranked = sorted(NUMERIC, key=lambda ff: -abs(auc([r[ff] for r in train], yt) + 0 - 0.5))
    ranked = [f for f in ranked if not np.isnan(auc([r[f] for r in train], yt))]
    print("   train top features by |AUC-0.5|:",
          [(f, round(auc([r[x] for r in train], yt), 3)) for f, x in [(f, f) for f in ranked[:5]]])
    print("   TEST-set baseline (no selection):")
    gate([r["moon"] for r in test], [r["t"] for r in test], CAP, "test ALL")
    for feat in ranked[:3]:
        for frac in (0.5, 0.3):
            keep, a, hidir, thr = fit_threshold_select(train, test, feat, frac)
            if len(keep) >= 10:
                gate([r["moon"] for r in keep], [r["t"] for r in keep], CAP,
                     f"top{int(frac*100)}% by {feat} ({'hi' if hidir else 'lo'})")
    # logistic combo of top-4 numeric (drop 'compact' to avoid regime leak)
    feats = [f for f in ranked if f != "compact"][:4]
    for frac in (0.5, 0.3):
        try:
            keep, clf = logistic_combo_select(train, test, feats, frac)
            if len(keep) >= 10:
                gate([r["moon"] for r in keep], [r["t"] for r in keep], CAP,
                     f"logit top{int(frac*100)}% {feats}")
        except Exception as e:
            print("   logit failed:", e)

# (A) standard time 70/30
n = len(rows); cut = int(n * 0.7)
run_oos(rows[:cut], rows[cut:], "TIME 70/30 (late=100% compact: format confound)")

# (B) within-compact temporal 60/40 (regime constant)
nc = len(comp); cc = int(nc * 0.6)
run_oos(comp[:cc], comp[cc:], "WITHIN-COMPACT 60/40 (regime held constant)")

# (C) k-fold-ish: 3 sequential compact folds to check single-regime robustness
print("\n===== 3-FOLD (within-compact) best-single-feature top30%, gate on each held-out fold =====")
folds = np.array_split(np.arange(nc), 3)
for k in range(3):
    test_idx = folds[k]; train_idx = np.concatenate([folds[j] for j in range(3) if j != k])
    tr = [comp[j] for j in train_idx]; te = [comp[j] for j in test_idx]
    yt = win_label(tr)
    ranked = sorted([f for f in NUMERIC if f != "compact"],
                    key=lambda ff: -abs(auc([r[ff] for r in tr], yt) - 0.5))
    feat = ranked[0]
    keep, a, hidir, thr = fit_threshold_select(tr, te, feat, 0.3)
    if len(keep) >= 10:
        gate([r["moon"] for r in keep], [r["t"] for r in keep], CAP, f"fold{k} top30% {feat}")
