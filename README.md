# Banter Coach
Chat-sim game that trains playful texting banter. A local LLM (LM Studio) judges your replies.

- `index.html`: the game (single file). Coach server URL comes from `?api=<url>` (saved in localStorage).
- `coach_proxy.py`: proxy in front of LM Studio (`/judge`, `/reply`, `/health`). Run `python3 coach_proxy.py`, then expose port 8787, e.g. `cloudflared tunnel --url http://127.0.0.1:8787`.
