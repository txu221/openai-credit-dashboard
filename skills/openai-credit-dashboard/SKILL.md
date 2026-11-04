---
name: openai-credit-dashboard
description: Start the local OpenAI Credit Dashboard to track API grant spending, estimated local token costs, and expiry forecasts.
---

Resolve the repository root as two directories above this SKILL.md (follow a
symlink to its real location first). It must contain server.py and pricing.json.
Python 3.9+ is required; no packages or build step are needed.

1. Check http://127.0.0.1:8765/api/health with a short timeout. Reuse it only if
   its JSON has `app: "openai-credit-dashboard"`. If the port belongs to another
   service, leave it running and choose another port with `--port`.
2. From the repository root, start `python3 server.py --port 8765` with the
   environment's supported long-running/background process mechanism. Preserve
   its process or session identifier so it can be stopped if requested. Use the
   user's requested mode/config/port flags when provided. Do not print or inspect
   secret values. The server reads OPENAI_ADMIN_KEY from the environment or the
   repository's .env; keys must never be placed in CLI arguments or config.json.
3. Check `/api/health` and `/api/summary` at that port with a short timeout.
   Return the dashboard URL only after health succeeds. If summary reports an
   error, report its safe message as well; do not claim billing data loaded.
   A 401/403 requires an Admin key; do not silently switch to local estimates.
4. Open the URL if a browser-opening tool is available; otherwise provide
   http://127.0.0.1:8765/ (adjust the port).

For headless output, run `python3 server.py --json` instead. Grant settings and
local data paths are documented in ../../README.md. Do not install dependencies,
fetch a release, modify the user's logs, or replace their configuration to launch.
