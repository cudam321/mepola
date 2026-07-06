#!/usr/bin/env python3
"""Export the dashboard as a FULLY FUNCTIONAL single-file HTML prototype.

Takes the built frontend (dist/) and welds it to an embedded, in-file API: the real JS
bundle runs, but fetch()/WebSocket are shimmed to serve a baked-in data snapshot (from
the populated demo DB) — so the token viewer opens, tabs switch, modals work, charts
render live via ECharts. Opens from file:// anywhere. Built for design handoff
(claude.ai/design) and design review.

    PYTHONPATH=src:. uv run python scripts/export_prototype.py \
        [--db /tmp/demo_state.db] [--out /tmp/mepola-prototype.html]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dashboard import data  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402

DIST = ROOT / "dashboard" / "frontend" / "dist"
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def build_payload(db_path: str) -> dict:
    st = data.open_state(db_path)
    snap = data.snapshot(st)

    # token details: every live mint + the biggest seed winners (hero click targets)
    details: dict[str, dict] = {}
    live_mints = [p["mint"] for p in snap["positions"]]
    hist_mints = [r["mint"] for r in snap["history"]["rows"]]
    top_seed = [p["mint"] for p in sorted(snap["hero"], key=lambda x: -abs(x["pnl"]))[:10]]
    for mint in dict.fromkeys(live_mints + hist_mints + top_seed + [ANSEM]):
        d = data.token_detail(st, mint)
        if d:
            details[mint] = d

    history = {
        "live": data.trade_history(st, "live", 200),
        "seed": data.trade_history(st, "seed", 200),
        "all": data.trade_history(st, "all", 200),
    }

    # real candles for ANSEM (the showcase token) — 1h since call, decimated
    candles: dict[str, dict] = {}
    try:
        pos = details.get(ANSEM, {}).get("position") or {}
        sig_at = datetime.fromisoformat(pos["signal_at"]) if pos.get("signal_at") else None
        if sig_at:
            jc = JupiterChartsClient(min_interval=0.4)
            end = min(sig_at + timedelta(days=45), datetime.now(timezone.utc))
            cds = jc.fetch_candles(ANSEM, "1_HOUR", sig_at - timedelta(hours=2), end, candles=1000)
            cds = [c for c in cds if sig_at - timedelta(hours=2) <= c.ts <= end]
            step = max(1, len(cds) // 400)
            rows = [[c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume]
                    for c in cds[::step]]
            entry = pos.get("entry_price")
            call = pos.get("signal_price")
            candles[ANSEM] = {
                "mint": ANSEM, "interval": "1h",
                "from": rows[0][0] if rows else None, "to": rows[-1][0] if rows else None,
                "candles": rows,
                "levels": {
                    "call": call, "entry_gate": 0.5 * call if call else None,
                    "entry": entry, "stop": None,
                    "rungs": ([{"mult": m, "price": m * entry} for m in (3, 6, 12, 24, 48)]
                              if entry else []),
                },
            }
            print(f"embedded {len(rows)} ANSEM candles", file=sys.stderr)
    except Exception as e:  # candles are a bonus — the empty state is also real
        print(f"ANSEM candles skipped: {e}", file=sys.stderr)

    control = {
        "editable": {
            "kill_switch": {"value": "off"},
            "ctl_stake_usd": {"value": 3.0, "min": 0.5, "max": 10.0, "default": 3.0},
            "ctl_max_concurrent": {"value": 25, "min": 1, "max": 100, "default": 25},
            "ctl_total_deployed_cap_usd": {"value": 200.0, "min": 10, "max": 1000, "default": 200.0},
            "ctl_daily_loss_cap_usd": {"value": 50.0, "min": 5, "max": 500, "default": 50.0},
        },
        "locked": {"dip_trigger": 0.5, "stop_level_mult": 0.7,
                   "tp1": "3x sell 33% then remove stop", "ladder": "6/12/24/48x (x2) then x3",
                   "reentry": False,
                   "note": "locked by research — editing these invalidates the backtest equivalence"},
        "mode": "paper",
        "mode_note": "live arming is CLI-gated (MEMEBOT_LIVE_ARMED), never a UI toggle",
    }
    st.close()
    return {"snapshot": snap, "details": details, "history": history,
            "candles": candles, "control": control}


SHIM = """
<script>
// ---- MEPOLA functional prototype shim: in-file API + fake live feed ----
const __PROTO__ = __DATA__;
(function () {
  const J = (obj, status = 200) => Promise.resolve(new Response(
    JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } }));
  const synthDetail = (mint) => {
    const p = (__PROTO__.snapshot.hero || []).find((x) => x.mint === mint);
    return { position: { mint, ticker: p ? p.ticker : mint.slice(0, 5), state: p ? p.state : "EXITED",
      signal_at: null, signal_price: null, entry_price: null, stake_usd: p ? p.stake : 3,
      current_multiple: p ? p.multiple : null, realized_multiple: p ? p.multiple : null,
      realized_pnl_usd: p ? p.pnl : 0, secured: 0, n_tp: 0 }, events: [], rungs: [] };
  };
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    if (!url.includes("/api/")) return realFetch(input, init);
    const u = new URL(url, "http://x");
    const path = u.pathname;
    if (path === "/api/snapshot") return J(__PROTO__.snapshot);
    if (path === "/api/health") return J({ ok: true, prototype: true });
    if (path === "/api/history") {
      const scope = u.searchParams.get("scope") || "live";
      return J({ scope, limit: 200, rows: __PROTO__.history[scope] || [] });
    }
    if (path === "/api/control") {
      if (init && init.method === "POST") return J({ ok: true });
      return J(__PROTO__.control);
    }
    let m = path.match(/^\\/api\\/token\\/([^/]+)\\/candles/);
    if (m) {
      const c = __PROTO__.candles[m[1]];
      return J(c || { mint: m[1], interval: "1m", candles: [],
                      levels: (__PROTO__.details[m[1]] || {}).rungs ? {} : {} });
    }
    m = path.match(/^\\/api\\/token\\/([^/]+)\\/live/);
    if (m) return J({ price: null, liquidity: null, price_change_24h: null,
                      ts: new Date().toISOString() });
    m = path.match(/^\\/api\\/token\\/([^/]+)/);
    if (m) return J(__PROTO__.details[m[1]] || synthDetail(m[1]));
    return J({ error: "not in prototype" }, 404);
  };
  window.WebSocket = class {
    constructor() {
      setTimeout(() => this.onopen && this.onopen(), 60);
      setTimeout(() => this.onmessage && this.onmessage(
        { data: JSON.stringify({ type: "snapshot", payload: __PROTO__.snapshot }) }), 180);
      this._t = setInterval(() => this.onmessage && this.onmessage(
        { data: JSON.stringify({ type: "heartbeat", ts: 1 }) }), 2500);
    }
    close() { clearInterval(this._t); this.onclose && this.onclose(); }
    send() {}
  };
})();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/demo_state.db")
    ap.add_argument("--out", default="/tmp/mepola-prototype.html")
    args = ap.parse_args()

    html = (DIST / "index.html").read_text()
    css_m = re.search(r'href="(/assets/[^"]+\.css)"', html)
    js_m = re.search(r'src="(/assets/[^"]+\.js)"', html)
    if not css_m or not js_m:
        print("dist/index.html missing asset refs", file=sys.stderr)
        return 1
    css = (DIST / css_m.group(1).lstrip("/")).read_text()
    js = (DIST / js_m.group(1).lstrip("/")).read_text()
    js = re.sub(r"//# sourceMappingURL=.*$", "", js, flags=re.M)

    payload = build_payload(args.db)
    shim = SHIM.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))

    html = re.sub(r'<link[^>]+href="/assets/[^"]+\.css"[^>]*>', "", html)
    html = re.sub(r'<link[^>]+rel="modulepreload"[^>]*>', "", html)
    html = re.sub(r'<script[^>]+src="/assets/[^"]+\.js"[^>]*></script>', "", html)
    html = html.replace("</head>",
                        f"<style>{css}</style>{shim}</head>")
    html = html.replace("</body>",
                        f'<script type="module">{js}</script></body>')

    Path(args.out).write_text(html)
    size = Path(args.out).stat().st_size
    print(f"functional prototype: {args.out} ({size/1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
