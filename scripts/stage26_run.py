"""Stage 26 runner: regularized meta-model with time-series CV + OOS EV gate."""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import numpy as np
np.seterr(all="ignore")
from collections import Counter

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score

import stage14_untruncated as S
from stage26_metamodel import build

CAP = 50.0
WIN_THR = 1.5

# ---------------------------------------------------------------- design matrix
LOG_MON = ["current_mc","entry_mc","sm_vol","holding_value","liquidity","dev_migrations",
           "age_h","grad_h","created_h"]
RAW = ["lateness","n_wallets","holding_pct","top_holders","organic_vol","net_pnl"]
SIG_TYPES = ["smartmoney","holding","main","cto","buymore","profit","other"]

def design(rows):
    feats = []
    names = []
    # log-transformed monetary/positive features
    for k in LOG_MON:
        col = []
        for r in rows:
            v = r[k]
            col.append(np.log1p(v) if (v is not None and v >= 0) else np.nan)
        feats.append(col); names.append("log_"+k)
    # lateness: log (ratio, can be <1 but >0)
    col = [np.log(r["lateness"]) if (r["lateness"] and r["lateness"]>0) else np.nan for r in rows]
    feats.append(col); names.append("log_lateness")
    for k in ["n_wallets","holding_pct","top_holders","organic_vol","net_pnl"]:
        feats.append([float(r[k]) if r[k] is not None else np.nan for r in rows]); names.append(k)
    # signal_type one-hot
    for st in SIG_TYPES:
        feats.append([1.0 if r["signal_type"]==st else 0.0 for r in rows]); names.append("sig_"+st)
    X = np.array(feats, dtype=float).T
    return X, names

def gate(mults, times):
    capped = S.cap_mults(mults, CAP)
    mean, lo, hi = S.mean_ci(capped)
    d3 = S.drop_top(capped, 3)
    lg = S.fixed_f_growth(capped, 0.02)
    bank = S.single_pass_bankroll(mults, times, 0.02, cap=CAP, start=500.0)
    passes = (lo > 1.0) and (d3 > 1.0) and (lg > 0.0) and (bank > 500.0)
    return dict(n=len(mults), mean=mean, lo=lo, hi=hi, drop3=d3, logG=lg, bank=bank, passes=passes)

# ---------------------------------------------------------------- models
def make_logit(C):
    return Pipeline([
        ("imp", SimpleImputer(strategy="median", add_indicator=True)),
        ("sc", StandardScaler()),
        ("clf", LogisticRegression(C=C, penalty="l2", class_weight="balanced",
                                   solver="liblinear", max_iter=2000)),
    ])

def make_gbt():
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=200, learning_rate=0.04,
        l2_regularization=2.0, min_samples_leaf=40,
        max_leaf_nodes=8, class_weight="balanced", random_state=0)

def cv_auc(rows, y, X, make_model, n_splits=5):
    tss = TimeSeriesSplit(n_splits=n_splits)
    oos_p = np.full(len(y), np.nan)
    fold_rows = []  # (test_idx_array)
    train_aucs, test_aucs = [], []
    for k,(tr,te) in enumerate(tss.split(X)):
        if len(np.unique(y[tr]))<2:  # need both classes in train
            continue
        m = make_model()
        m.fit(X[tr], y[tr])
        ptr = m.predict_proba(X[tr])[:,1]
        pte = m.predict_proba(X[te])[:,1]
        oos_p[te] = pte
        tra = roc_auc_score(y[tr], ptr)
        tea = roc_auc_score(y[te], pte) if len(np.unique(y[te]))>1 else float("nan")
        train_aucs.append(tra); test_aucs.append(tea)
        fold_rows.append((tr,te))
        print(f"  fold{k}: n_tr={len(tr):4d} n_te={len(te):4d}  train_AUC={tra:.3f}  test_AUC={tea:.3f}"
              f"  test_winrate={y[te].mean():.3f}")
    mask = ~np.isnan(oos_p)
    pooled = roc_auc_score(y[mask], oos_p[mask]) if len(np.unique(y[mask]))>1 else float("nan")
    print(f"  POOLED OOS AUC={pooled:.3f}   mean train_AUC={np.nanmean(train_aucs):.3f}"
          f"   mean test_AUC={np.nanmean(test_aucs):.3f}"
          f"   overfit gap={np.nanmean(train_aucs)-np.nanmean(test_aucs):+.3f}")
    return oos_p, mask, fold_rows, pooled, np.nanmean(train_aucs), np.nanmean(test_aucs)

def select_and_gate(rows, y, X, make_model, mults, times, n_splits=5, label=""):
    """Per-fold: choose top-fraction on TRAIN maximizing train mean-capped multiple
    (>=15 train trades), apply proba threshold to TEST. Aggregate OOS traded set."""
    tss = TimeSeriesSplit(n_splits=n_splits)
    traded_idx = []
    grid = [0.10,0.15,0.20,0.30,0.40,0.50]
    for tr,te in tss.split(X):
        if len(np.unique(y[tr]))<2:
            continue
        m = make_model(); m.fit(X[tr], y[tr])
        ptr = m.predict_proba(X[tr])[:,1]; pte = m.predict_proba(X[te])[:,1]
        best_frac, best_ev = None, -1e9
        for fr in grid:
            thr = np.quantile(ptr, 1-fr)
            sel = ptr >= thr
            if sel.sum() < 15:
                continue
            ev = np.mean(S.cap_mults([mults[i] for i in tr[sel]], CAP))
            if ev > best_ev:
                best_ev, best_frac, best_thr = ev, fr, thr
        if best_frac is None:
            best_thr = np.quantile(ptr, 0.8)
        sel_te = pte >= best_thr
        traded_idx.extend(te[sel_te].tolist())
    traded_idx = sorted(set(traded_idx))
    if not traded_idx:
        print(f"  [{label}] no OOS trades"); return None
    tm = [mults[i] for i in traded_idx]
    tt = [times[i] for i in traded_idx]
    g = gate(tm, tt)
    wr = np.mean([1 if mults[i]>WIN_THR else 0 for i in traded_idx])
    print(f"  [{label}] OOS traded n={g['n']}  winrate={wr:.3f}  mean={g['mean']:.3f}"
          f"  CIlo={g['lo']:.3f} CIhi={g['hi']:.3f}  drop3={g['drop3']:.3f}"
          f"  logG(f=2%)={g['logG']:+.4f}  $500->${g['bank']:.0f}  PASS={g['passes']}")
    return g

if __name__ == "__main__":
    rows = build()
    y = np.array([1 if r["moon"]>WIN_THR else 0 for r in rows])
    mults = np.array([r["moon"] for r in rows])
    times = np.array([r["posted_at"].timestamp() for r in rows])
    X, names = design(rows)
    print(f"n={len(rows)}  n_features={X.shape[1]}  WIN rate={y.mean():.3f}")
    print("baseline (trade ALL): ", end="")
    gall = gate(list(mults), list(times)); print(gall)

    for Cval in [0.02, 0.1, 0.5]:
        print(f"\n=== Logistic L2 (C={Cval}, balanced) ===")
        oos_p,mask,folds,pooled,tra,tea = cv_auc(rows,y,X, lambda: make_logit(Cval))
        select_and_gate(rows,y,X, lambda: make_logit(Cval), mults, times, label=f"logit C={Cval}")

    print(f"\n=== HistGradientBoosting (strong reg) ===")
    oos_p,mask,folds,pooled,tra,tea = cv_auc(rows,y,X, make_gbt)
    select_and_gate(rows,y,X, make_gbt, mults, times, label="gbt")
