"""memebot dashboard — a read-only presentation layer over the live SQLite state.

Isolated from the core `memebot` library (which stays httpx+numpy only). The FastAPI backend
(`dashboard.server.app`) reads `runs/live_state.db` read-only and serves a JSON snapshot + a
WebSocket delta stream; the React frontend (`dashboard/frontend`) renders the power-law hero.
"""
