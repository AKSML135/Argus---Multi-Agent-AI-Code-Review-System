# Argus frontend

A single-page console for the review pipeline described in `docs/DEMO.md` —
submit a diff, watch the graph run agent-by-agent in real time, resolve the
two HITL gates, and read the final report. Plain HTML/CSS/JS, no build step,
no dependencies except `marked.js` (loaded from a CDN) for rendering the
Markdown report.

## Run it

1. Start the API (from the project root):
   ```bash
   argus serve --port 8000
   ```
2. Serve this folder as static files — don't open `index.html` via
   `file://`, some browsers block the streaming `fetch()` calls from a
   `file://` origin:
   ```bash
   cd frontend
   python3 -m http.server 5173
   ```
3. Open `http://localhost:5173` in your browser.

The top bar has the API base URL and `x-api-key` fields, pre-filled with
`http://localhost:8000` / `dev-secret-key` to match `docs/DEMO.md`. Change
them if your server runs elsewhere.

## What it does

- **Submit a review** — paste a diff, or load one of the three bundled
  fixture patches (`sample.patch`, `critical_bugs.patch`, `high_bugs.patch`)
  with one click.
- **Live pipeline** — an eye per agent (input guardrail → supervisor → the
  five workers → aggregator → the two HITL gates → report generator) that
  opens as its node starts and fills in solid green when it finishes.
  Click any eye to see its raw lifecycle events and the findings it
  produced. This is driven by the `/reviews/{id}/stream` SSE endpoint,
  read via `fetch()` rather than `EventSource` since the API requires the
  `x-api-key` header, which `EventSource` cannot send.
- **HITL gates** — when the review pauses, a gate panel appears with the
  gate name and action pre-filled from what the client can infer (whether
  a critical finding exists and which gates have already been resolved).
  If that guess is wrong, the API's own error message names the actual
  paused gate, and the console reads it back and corrects the form for you
  automatically — no state is ever guessed twice blindly.
- **Findings table** and **final report** (rendered Markdown, with a
  download-as-`.md` button).
- **Metrics** — a collapsed panel that fetches `/metrics` and shows the
  `argus_*` lines.
- A status poll (every ~3.5s) runs alongside the SSE stream, so if the
  server restarts mid-review (DEMO.md step 11) the console keeps reporting
  the true state once it comes back, rather than getting stuck on whatever
  the last stream event said.

## Notes / limitations

- **No local storage.** Reviews you submit or attach to are tracked only
  in memory for the current browser tab; reload the page and the list is
  gone. For the step-11 restart test: submit a review, copy its ID from
  the pipeline header (or the "Copy ID" button), restart `argus serve`,
  then paste the ID into "Track a review → Attach" to pick it back up.
- **CORS.** `src/argus/api/main.py` now adds `CORSMiddleware` with a
  wide-open dev origin list so the browser can call the API from a
  different port/origin. That's the only backend change made — tighten
  `allow_origins` before this ever leaves your machine.
- **"Agent" column on findings** is inferred client-side from each
  finding's `category` (e.g. `leaked_secret` → security), because the API
  doesn't currently persist which worker produced a given finding
  (`FindingRow` has no `agent_name` column, and the `agent_runs` table is
  defined in `persistence/models.py` but nothing in the codebase writes to
  it). The mapping is in `app.js` → `CATEGORY_TO_AGENT`.
- Gate mismatches are corrected from the API's error text (`"paused at
  [...]"`), not guessed blindly — the console never fires a second resume
  automatically, it always waits for you to click again after correcting
  the form.
