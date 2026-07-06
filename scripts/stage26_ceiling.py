"""Ceiling diagnostic: is there ANY threshold on the OOS predictions that passes
the gate? Sweep proba thresholds on the pooled OOS logistic predictions.
This is deliberately optimistic (threshold picked with hindsight) — if even this
ceiling fails, the NO-GO is robust."""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import numpy as np; np.seterr(all="ignore")
from sklearn.model_selection import TimeSeriesSplit
import stage14_untruncated as S
from stage26_metamodel import build
from stage26_run import design, make_logit, make_gbt, gate, CAP, WIN_THR

rows = build()
y = np.array([1 if r["moon"]>WIN_THR else 0 for r in rows])
mults = np.array([r["moon"] for r in rows])
times = np.array([r["posted_at"].timestamp() for r in rows])
X,names = design(rows)

def pooled_oos(make_model, n_splits=5):
    tss = TimeSeriesSplit(n_splits=n_splits)
    p = np.full(len(y), np.nan)
    for tr,te in tss.split(X):
        if len(np.unique(y[tr]))<2: continue
        m = make_model(); m.fit(X[tr],y[tr])
        p[te] = m.predict_proba(X[te])[:,1]
    return p

for label, mk in [("logit C=0.02", lambda: make_logit(0.02)), ("gbt", make_gbt)]:
    p = pooled_oos(mk)
    mask = ~np.isnan(p)
    idx = np.where(mask)[0]
    print(f"\n=== CEILING sweep [{label}]  (OOS n scored={mask.sum()}) ===")
    print(f"{'top%':>6} {'n':>5} {'winrate':>8} {'mean':>7} {'CIlo':>7} {'drop3':>7} {'logG':>9} {'$500':>8} {'PASS':>5}")
    for frac in [0.05,0.10,0.15,0.20,0.30,0.50,1.0]:
        thr = np.quantile(p[mask], 1-frac)
        sel = idx[p[idx]>=thr]
        if len(sel)<5:
            print(f"{frac*100:6.0f} {len(sel):5d}  (too few)"); continue
        tm = [mults[i] for i in sel]; tt=[times[i] for i in sel]
        g = gate(tm,tt)
        wr = np.mean([1 if mults[i]>WIN_THR else 0 for i in sel])
        print(f"{frac*100:6.0f} {g['n']:5d} {wr:8.3f} {g['mean']:7.3f} {g['lo']:7.3f} {g['drop3']:7.3f} {g['logG']:+9.4f} {g['bank']:8.0f} {str(g['passes']):>5}")
