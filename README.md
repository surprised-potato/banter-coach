# Banter Coach
Chat-sim game that trains playful texting banter. A local LLM (LM Studio) judges your replies.

- `index.html`: the game (single file). Coach server URL comes from `?api=<url>` (saved in localStorage).
- `coach_proxy.py`: proxy in front of LM Studio (`/judge`, `/reply`, `/health`). Run `python3 coach_proxy.py`, then expose port 8787, e.g. `cloudflared tunnel --url http://127.0.0.1:8787`.

## v2
- Profiles: name + PIN (PBKDF2-hashed), progress and every attempt stored server-side in `banter.db` (SQLite, git-ignored). Guests keep progress in localStorage.
- Home menu picks a "next drill" from your weakest skill (from the coach's habit flags), offers the rally when ready, and can generate fresh texts with the LLM.
- Stats: level, pass rate by stage, score trend, skill bars, top habits, full history.
