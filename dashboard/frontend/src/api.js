// Snapshot fetch + live WebSocket with auto-reconnect.

// --- BOOK: which data source THIS TAB views/trades -------------------------------------------- //
// "live" = the real-money book. "paper" = the paper twin (practice + measurement).
// CRITICAL: the active book is PER-TAB module state. localStorage only seeds the initial value and
// persists the preference across reloads — actions must NEVER read localStorage at click time,
// because it is shared across tabs: another tab switching books would silently retarget this tab's
// orders (a practice click firing a REAL live order, or vice versa).
let _book = localStorage.getItem("mepola_book") === "paper" ? "paper" : "live";
export const currentBook = () => _book;
export const setCurrentBook = (b) => {
  _book = b === "paper" ? "paper" : "live";
  try {
    localStorage.setItem("mepola_book", _book);
  } catch {
    /* private mode etc. — per-tab state still correct */
  }
};
// helper for raw fetch() call sites: appends the current book to any /api URL
export const withBook = (url) => url + (url.includes("?") ? "&" : "?") + "book=" + currentBook();

export async function fetchSnapshot(book) {
  const r = await fetch(`/api/snapshot?book=${book || currentBook()}`);
  if (!r.ok) throw new Error("snapshot failed");
  return r.json();
}

// --- MANUAL trading control plane (writes rows; the engine executes them) ------------------- //
async function jreq(method, url, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${method} ${url} failed (${r.status})`);
  return j;
}

// Every action targets the CURRENT book: on LIVE it's a real order the engine executes on-chain;
// on PAPER it's a practice order the paper twin fills with simulated money (full functionality,
// zero risk — the paper book never touches the live engine or the wallet).
export const manual = {
  placeOrder: (o) => jreq("POST", "/api/manual/order", { ...o, book: currentBook() }),
  cancelOrder: (id) => jreq("DELETE", withBook(`/api/manual/order/${id}`)),
  modifyOrder: (id, fields) => jreq("PATCH", withBook(`/api/manual/order/${id}`), fields),
  injectSignal: (mint, ticker) => jreq("POST", "/api/signal", { mint, ticker, book: currentBook() }),
  lookup: (mint) => jreq("GET", `/api/lookup/${mint}`),
  release: (mint) => jreq("POST", withBook(`/api/positions/${mint}/release`)),
  ordersFor: async (mint) => {
    const j = await jreq("GET", withBook("/api/manual/orders?status=open"));
    return (j.orders || []).filter((o) => o.mint === mint);
  },
};

export function connectWS(onSnapshot, onStatus) {
  let ws;
  let closed = false;
  let retry;
  let watchdog;
  let lastFrameTs = Date.now();
  let backoff = 1000;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      backoff = 1000;
      lastFrameTs = Date.now();
      onStatus?.("live");
    };
    ws.onmessage = (ev) => {
      lastFrameTs = Date.now();          // F39: ANY frame proves the socket is live
      onStatus?.("live");
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;                          // drop a malformed frame instead of throwing
      }
      if (msg.type === "snapshot") onSnapshot?.(msg.payload);
    };
    ws.onclose = () => {
      clearInterval(watchdog);
      onStatus?.("down");
      if (!closed) {
        retry = setTimeout(open, backoff);
        backoff = Math.min(15000, backoff * 2);   // exponential backoff on repeated failures
      }
    };
    ws.onerror = () => ws.close();
    // F39: the server pushes a snapshot/heartbeat every ~2s. If none arrives for ~8s the
    // socket died silently (laptop sleep, idle-timeout, mobile partition) with NO close frame,
    // so onclose never fires and the status would stay green over stale data. Force a close so
    // onclose schedules a reconnect.
    clearInterval(watchdog);
    watchdog = setInterval(() => {
      if (Date.now() - lastFrameTs > 8000) {
        onStatus?.("down");
        try { ws.close(); } catch {}
      }
    }, 2000);
  };
  open();

  return () => {
    closed = true;
    clearTimeout(retry);
    clearInterval(watchdog);
    ws?.close();
  };
}
