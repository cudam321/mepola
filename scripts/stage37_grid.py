import numpy as np, itertools, sys, datetime as dt
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
import stage14_untruncated as S
ANSEM="9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
W48=48*3600

def sim(H,L,C,T,sig,dip,sl,ftp,fsell,reentry):
    n=len(H)
    if dip==0: start=0; entry=sig*1.01
    else:
        start=None
        for j in range(n):
            if T[j]-T[0]>W48: break
            if L[j]<=(1-dip)*sig: start=j; entry=(1-dip)*sig*1.01; break
        if start is None: return None
    legs=[]; i=start
    while i<n and len(legs)<8:
        rem=1.0;pr=0.0;ntp=0;lvl=ftp;sec=False;stp=False;expx=C[-1];eidx=n-1
        for j in range(i,n):
            if rem<=1e-9: eidx=j;break
            if (not sec) and sl>0 and L[j]<=sl*entry:
                pr+=rem*sl*entry*0.95;rem=0;stp=True;expx=sl*entry;eidx=j;break
            while rem>1e-9 and H[j]>=lvl*entry:
                s=min(fsell if ntp==0 else 0.25*rem,rem)
                pr+=s*lvl*entry*0.985;rem-=s;ntp+=1
                if ntp==1: sec=True
                lvl=lvl*2 if ntp<5 else lvl*3
        if rem>1e-9: pr+=rem*C[-1]
        legs.append(pr/entry)
        if not stp or reentry is None: break
        tgt=reentry*expx;k=eidx+1
        while k<n and H[k]<tgt: k+=1
        if k>=n: break
        entry=tgt*1.01;i=k
    return legs

calls=sorted([s for s in first_call_per_mint(load_corpus_json("runs/your_channel_fresh.json")) if s.mint],key=lambda s:s.posted_at)
client=CachedPriceClient(JupiterChartsClient(min_interval=0.4),"data_cache/jupiter_untrunc")
toks=[]
for s in calls:
    try: ser=S.series_to_today(client,s.mint,s.posted_at)
    except Exception: ser=None
    if not ser or not ser.candles: continue
    cds=[c for c in ser.candles if c.ts>=s.posted_at]
    if not cds or cds[0].open<=0: continue
    H=np.array([c.high for c in cds]);L=np.array([c.low for c in cds]);C=np.array([c.close for c in cds]);Tt=np.array([c.ts.timestamp() for c in cds])
    toks.append((s.mint,H,L,C,Tt,cds[0].open,s.posted_at.timestamp()))
print(f"loaded {len(toks)} tokens",file=sys.stderr)
dates=sorted(t[6] for t in toks); cut=dates[int(len(dates)*0.7)]

grid=list(itertools.product([0,0.3,0.5],[0,0.3,0.5,0.7],[1.5,2.0,3.0],[0.33,0.5],[3.0,None]))
def stats(v):
    a=np.array(v)
    if len(a)<10: return None
    m=a.mean(); b=np.sort(a); d3=b[:-3].mean() if len(b)>3 else np.nan
    return m,d3
rows=[]
for dip,sl,ftp,fsell,re in grid:
    tr=[];oo=[];ttimes=[];otimes=[];am=None
    for mint,H,L,C,Tt,sig,ts in toks:
        Lg=sim(H,L,C,Tt,sig,dip,sl,ftp,fsell,re)
        if not Lg: continue
        if ts<cut: tr.extend(Lg)
        else:
            oo.extend(Lg); otimes.extend([ts]*len(Lg))
        if mint==ANSEM: am=float(np.mean(Lg))
    st=stats(tr); so=stats(oo)
    if st and so:
        bank=S.single_pass_bankroll(list(np.array(oo)),np.asarray(otimes),0.02,float("inf"))
        rows.append(dict(cfg=(dip,sl,ftp,fsell,re),trmean=st[0],trd3=st[1],oomean=so[0],ood3=so[1],bank=bank,ansem=am,noo=len(oo)))
# pick best by TRAIN drop3 (robust, no OOS peeking), report OOS
rows_by_train=sorted(rows,key=lambda r:r["trd3"],reverse=True)
print("\n=== TOP 6 configs chosen by TRAIN drop3 (Jan-Apr), then their OOS (May-Jul) result ===")
print(f"{'cfg (dip,sl,ftp,fsell,re)':34} | {'TRAINmean':>9} {'TRd3':>6} | {'OOSmean':>8} {'OOSd3':>6} {'OOS$500':>8} {'ANSEM':>7}")
for r in rows_by_train[:6]:
    print(f"{str(r['cfg']):34} | {r['trmean']:>9.3f} {r['trd3']:>6.3f} | {r['oomean']:>8.3f} {r['ood3']:>6.3f} {r['bank']:>8.0f} {(r['ansem'] or 0):>6.1f}x")
print("\n=== for context: TOP 6 by OOS mean (this is OOS-peeking / overfit-prone) ===")
for r in sorted(rows,key=lambda r:r["oomean"],reverse=True)[:6]:
    print(f"{str(r['cfg']):34} | {'':>9} {'':>6} | {r['oomean']:>8.3f} {r['ood3']:>6.3f} {r['bank']:>8.0f} {(r['ansem'] or 0):>6.1f}x")
best=max(rows,key=lambda r:r["ood3"])
print(f"\nabsolute best by OOS-drop3 (most robust): cfg={best['cfg']}  OOSmean={best['oomean']:.3f} OOSd3={best['ood3']:.3f} $500->{best['bank']:.0f}")
