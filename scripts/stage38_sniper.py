import csv, json, sys
import numpy as np
from memebot.data.dune import DuneClient
import stage14_untruncated as S

rows={r["mint"]:r for r in csv.DictReader(open("runs/stage_fresh_pertoken.csv"))}
mints=list(rows.keys())
d=DuneClient()
data={}
CH=460
for ci in range(0,len(mints),CH):
    chunk=mints[ci:ci+CH]
    vals=",".join(f"('{m}')" for m in chunk)
    sql=f"""
    WITH mints(mint) AS (VALUES {vals}),
    buys AS (SELECT t.token_bought_mint_address mint,t.block_slot,t.block_time,t.trader_id,t.amount_usd
      FROM dex_solana.trades t JOIN mints m ON m.mint=t.token_bought_mint_address
      WHERE t.block_month>=DATE '2026-01-01' AND t.block_month<DATE '2026-08-01'),
    launch AS (SELECT mint,MIN(block_slot) s0,MIN(block_time) t0 FROM buys GROUP BY mint)
    SELECT b.mint,
      count(distinct case when b.block_slot=l.s0 then b.trader_id end) block0_buyers,
      sum(case when b.block_slot=l.s0 then b.amount_usd else 0 end) block0_vol,
      count(distinct case when b.block_time<=l.t0+interval '60' second then b.trader_id end) b60_buyers
    FROM buys b JOIN launch l ON b.mint=l.mint GROUP BY b.mint"""
    res=d.run_sql(sql, performance="medium")
    for r in res["rows"]: data[r["mint"]]=r
    print(f"  chunk {ci//CH+1}: got {len(res['rows'])} (cum {len(data)}) dp={d.datapoints}",file=sys.stderr)
json.dump(data, open("runs/sniper_data.json","w"))

# join + analyze
recs=[]
for m,r in rows.items():
    if m not in data: continue
    sd=data[m]
    recs.append(dict(mint=m, moon=min(float(r["moon"]),50.0), mfe=float(r["mfe"]),
        b0=float(sd["block0_buyers"] or 0), b0v=float(sd["block0_vol"] or 0), b60=float(sd["b60_buyers"] or 0),
        ts=None))
n=len(recs)
moon=np.array([x["moon"] for x in recs]); mfe=np.array([x["mfe"] for x in recs])
win=(moon>1.5).astype(int); run=(mfe>2).astype(int)
print(f"\nmatched sniper data for {n}/{len(rows)} tokens")
def auc(v,lab):
    v=np.array(v,float); y=np.array(lab,float)
    if y.sum()<5 or len(y)-y.sum()<5: return 0.5
    o=np.argsort(v); rk=np.empty(len(v)); rk[o]=np.arange(1,len(v)+1)
    n1=y.sum();n0=len(y)-n1
    return float((rk[y==1].sum()-n1*(n1+1)/2)/(n1*n0))
print("\nAUC of sniper metrics (does LOW sniper predict winners? AUC<0.5 = low-is-better):")
for k in ("b0","b0v","b60"):
    v=[x[k] for x in recs]
    print(f"  {k:5} AUC vs WIN(moon>1.5)={auc(v,win):.3f}  vs RUN(mfe>2)={auc(v,run):.3f}")
print("\nfilter test: keep only LOW-sniper tokens (exclude high-b60 deciles), re-price EQUAL-WEIGHT book:")
b60=np.array([x["b60"] for x in recs])
for pct in (100,75,50,33,20,10):
    thr=np.percentile(b60,pct) if pct<100 else 1e9
    sel=[x for x in recs if x["b60"]<=thr]
    mm=np.array([x["moon"] for x in sel])
    m,lo,hi=S.mean_ci(list(mm)); d3=S.drop_top(list(mm),3)
    print(f"  keep b60<=p{pct:<3} (n={len(sel):4d}) moon mean={m:.3f} CIlo={lo:.3f} drop3={d3:.3f} win={ (mm>1.5).mean()*100:.0f}%")
