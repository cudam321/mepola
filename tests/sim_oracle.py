"""Golden reference: config #1's `sim`, copied VERBATIM from `scripts/stage37_grid.py::sim`
(identical to `scripts/stage38_ansem_dependence.py::sim`).

This is the oracle the live `TailRider` state machine is pinned against. It is copied
here (rather than imported) because importing the stage scripts drags in a deep chain
(stage14 -> stage4_powerlaw -> stage3_oos -> DuneClient) with import-time side effects.
Config #1 is LOCKED, so this reference is stable. If the canonical `sim` ever changes,
update this copy in lockstep.

Do not edit the body — keep it byte-for-byte equivalent to the script.
"""

from __future__ import annotations

W48 = 48 * 3600


def sim(H, L, C, T, sig, dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None):
    n = len(H)
    if dip == 0:
        start = 0; entry = sig * 1.01
    else:
        start = None
        for j in range(n):
            if T[j] - T[0] > W48: break
            if L[j] <= (1 - dip) * sig: start = j; entry = (1 - dip) * sig * 1.01; break
        if start is None: return None
    legs = []; i = start
    while i < n and len(legs) < 8:
        rem = 1.0; pr = 0.0; ntp = 0; lvl = ftp; sec = False; stp = False; expx = C[-1]; eidx = n - 1
        for j in range(i, n):
            if rem <= 1e-9: eidx = j; break
            if (not sec) and sl > 0 and L[j] <= sl * entry:
                pr += rem * sl * entry * 0.95; rem = 0; stp = True; expx = sl * entry; eidx = j; break
            while rem > 1e-9 and H[j] >= lvl * entry:
                s = min(fsell if ntp == 0 else 0.25 * rem, rem)
                pr += s * lvl * entry * 0.985; rem -= s; ntp += 1
                if ntp == 1: sec = True
                lvl = lvl * 2 if ntp < 5 else lvl * 3
        if rem > 1e-9: pr += rem * C[-1]
        legs.append(pr / entry)
        if not stp or reentry is None: break
        tgt = reentry * expx; k = eidx + 1
        while k < n and H[k] < tgt: k += 1
        if k >= n: break
        entry = tgt * 1.01; i = k
    return legs


def sim_multiple(H, L, C, T, sig):
    """Config #1 has no re-entry -> at most one leg. Returns the realized multiple or None."""
    legs = sim(H, L, C, T, sig)
    return legs[0] if legs else None
