/* ============================================================
   Argus console — vanilla JS, no build step.
   Talks to the FastAPI backend described in docs/DEMO.md.
   ============================================================ */

(function () {
  "use strict";

  // ---------------------------------------------------------
  // Fixture diffs (mirrors fixtures/*.patch so the demo works
  // without the browser needing filesystem access)
  // ---------------------------------------------------------
  const FIXTURES = {
    sample: "diff --git a/src/app/utils.py b/src/app/utils.py\nindex abc123..def456 100644\n--- a/src/app/utils.py\n+++ b/src/app/utils.py\n@@ -1,12 +1,28 @@\n+import os\n+import re\n+import hashlib\n+\n def process_user_input(data):\n-    return data\n+    # Validate and sanitize input\n+    if not isinstance(data, str):\n+        raise TypeError(\"data must be a string\")\n+    return data.strip()\n+\n+def load_config(path):\n+    with open(path) as f:\n+        return f.read()\n+\n+def compute_hash(value: str) -> str:\n+    return hashlib.sha256(value.encode()).hexdigest()\n+\n+def query_db(conn, user_id):\n+    # WARNING: SQL injection risk\n+    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n+    return conn.execute(query)\n+\n+SECRET_KEY = \"sk-prod-abc123xyz789\"\n+\n+def debug_endpoint(request):\n+    # TODO: remove before production\n+    return {\"env\": dict(os.environ)}\ndiff --git a/src/app/auth.py b/src/app/auth.py\nindex 111aaa..222bbb 100644\n--- a/src/app/auth.py\n+++ b/src/app/auth.py\n@@ -0,0 +1,15 @@\n+from typing import Optional\n+\n+def authenticate(username: str, password: str) -> Optional[str]:\n+    \"\"\"Authenticate a user and return a session token.\"\"\"\n+    if not username or not password:\n+        return None\n+    # Simplified auth - would use proper hashing in production\n+    if username == \"admin\" and password == \"admin\":\n+        return \"token-admin-123\"\n+    return None\n+\n+def validate_token(token: str) -> bool:\n+    \"\"\"Check if a token is valid.\"\"\"\n+    return token.startswith(\"token-\")\n",
    critical: "diff --git a/src/app/payments.py b/src/app/payments.py\nindex 000000..a1b2c3 100644\n--- a/src/app/payments.py\n+++ b/src/app/payments.py\n@@ -0,0 +1,52 @@\n+import subprocess\n+import pickle\n+import requests\n+\n+# Payment processing module\n+STRIPE_SECRET_KEY = \"sk_live_4eC39HqLyjWDarjtT1zdp7dc\"\n+DB_PASSWORD = \"prod-db-pass-2024!\"\n+\n+def process_payment(amount: float, card_data: dict) -> dict:\n+    \"\"\"Process a payment for the given amount.\"\"\"\n+    # Log full card details for debugging\n+    print(f\"Processing card: {card_data}\")\n+\n+    if amount <= 0:\n+        return {\"status\": \"error\"}\n+\n+    # Charge the card\n+    response = requests.post(\n+        \"https://api.stripe.com/v1/charges\",\n+        auth=(STRIPE_SECRET_KEY, \"\"),\n+        data={\"amount\": int(amount * 100), \"currency\": \"usd\"},\n+    )\n+    return response.json()\n+\n+def load_user_session(session_bytes: bytes) -> dict:\n+    \"\"\"Restore a user session from cookie data.\"\"\"\n+    # Deserialize session directly from user-supplied bytes\n+    return pickle.loads(session_bytes)\n+\n+def run_report(report_name: str) -> str:\n+    \"\"\"Generate a named report by invoking the report script.\"\"\"\n+    # Run the report generator with user-supplied name\n+    result = subprocess.run(\n+        f\"python scripts/reports/{report_name}.py\",\n+        shell=True,\n+        capture_output=True,\n+        text=True,\n+    )\n+    return result.stdout\n+\n+def get_user_balance(conn, user_id: str) -> float:\n+    \"\"\"Return the account balance for a user.\"\"\"\n+    row = conn.execute(\n+        f\"SELECT balance FROM accounts WHERE user_id = '{user_id}'\"\n+    ).fetchone()\n+    return row[0] if row else 0.0\n+\n+def transfer_funds(conn, from_id: str, to_id: str, amount: float) -> bool:\n+    \"\"\"Transfer funds between two accounts (no transaction, no lock).\"\"\"\n+    from_bal = get_user_balance(conn, from_id)\n+    to_bal   = get_user_balance(conn, to_id)\n+    # No atomicity: a crash here leaves balances inconsistent\n+    conn.execute(f\"UPDATE accounts SET balance = {from_bal - amount} WHERE user_id = '{from_id}'\")\n+    conn.execute(f\"UPDATE accounts SET balance = {to_bal  + amount} WHERE user_id = '{to_id}'\")\n+    return True\ndiff --git a/src/app/admin.py b/src/app/admin.py\nindex 000000..d4e5f6 100644\n--- a/src/app/admin.py\n+++ b/src/app/admin.py\n@@ -0,0 +1,24 @@\n+from fastapi import APIRouter\n+\n+router = APIRouter(prefix=\"/admin\")\n+\n+@router.get(\"/users\")\n+def list_all_users(db):\n+    \"\"\"Return every user record \u2014 no auth check.\"\"\"\n+    return db.execute(\"SELECT * FROM users\").fetchall()\n+\n+@router.delete(\"/users/{user_id}\")\n+def delete_user(user_id: str, db):\n+    \"\"\"Delete a user \u2014 no auth, no confirmation.\"\"\"\n+    db.execute(f\"DELETE FROM users WHERE id = '{user_id}'\")\n+    db.commit()\n+    return {\"deleted\": user_id}\n+\n+@router.get(\"/debug/env\")\n+def dump_env():\n+    \"\"\"Expose all environment variables \u2014 no auth.\"\"\"\n+    import os\n+    return dict(os.environ)\n+\n+@router.post(\"/exec\")\n+def remote_exec(code: str):\n+    \"\"\"Execute arbitrary Python \u2014 intentionally no auth for 'testing'.\"\"\"\n+    return {\"result\": eval(code)}\n",
    high: "diff --git a/src/app/user_service.py b/src/app/user_service.py\nindex 000000..b3c4d1 100644\n--- a/src/app/user_service.py\n+++ b/src/app/user_service.py\n@@ -0,0 +1,68 @@\n+import logging\n+import time\n+from typing import Optional\n+\n+logger = logging.getLogger(__name__)\n+\n+# Cache: maps user_id \u2192 profile dict\n+_user_cache: dict = {}\n+\n+def get_user_profile(db, user_id: int) -> Optional[dict]:\n+    \"\"\"Return user profile, using an in-memory cache to reduce DB hits.\"\"\"\n+    if user_id in _user_cache:\n+        return _user_cache[user_id]\n+\n+    row = db.execute(\n+        \"SELECT id, name, email, role FROM users WHERE id = ?\", (user_id,)\n+    ).fetchone()\n+\n+    if row is None:\n+        return None\n+\n+    profile = {\"id\": row[0], \"name\": row[1], \"email\": row[2], \"role\": row[3]}\n+    # Cache with no expiry and no size limit \u2014 grows unbounded in production\n+    _user_cache[user_id] = profile\n+    return profile\n+\n+def update_user_role(db, requesting_user_id: int, target_user_id: int, new_role: str) -> bool:\n+    \"\"\"Promote or demote a user's role.\"\"\"\n+    requesting = get_user_profile(db, requesting_user_id)\n+    # Missing authorisation check: any authenticated user can change any role\n+    db.execute(\n+        \"UPDATE users SET role = ? WHERE id = ?\", (new_role, target_user_id)\n+    )\n+    db.commit()\n+    # Stale cache entry stays \u2014 next read returns the old role\n+    logger.info(\"Role updated: user %s \u2192 %s\", target_user_id, new_role)\n+    return True\n+\n+def bulk_delete_inactive_users(db, days_inactive: int) -> int:\n+    \"\"\"Delete users who have not logged in for `days_inactive` days.\"\"\"\n+    cutoff = time.time() - days_inactive * 86400\n+    # No dry-run, no soft-delete, no audit log \u2014 permanent and silent\n+    result = db.execute(\n+        \"DELETE FROM users WHERE last_login < ?\", (cutoff,)\n+    )\n+    db.commit()\n+    return result.rowcount\n+\n+def search_users(db, query: str) -> list:\n+    \"\"\"Search users by name or email.\"\"\"\n+    # Returns all columns including password_hash to the caller\n+    rows = db.execute(\n+        \"SELECT * FROM users WHERE name LIKE ? OR email LIKE ?\",\n+        (f\"%{query}%\", f\"%{query}%\"),\n+    ).fetchall()\n+    return [dict(row) for row in rows]\n+\n+def reset_password(db, user_id: int, new_password: str) -> bool:\n+    \"\"\"Reset a user's password (no token, no old-password check).\"\"\"\n+    # Stores plaintext \u2014 no hashing\n+    db.execute(\n+        \"UPDATE users SET password = ? WHERE id = ?\", (new_password, user_id)\n+    )\n+    db.commit()\n+    return True\ndiff --git a/src/app/rate_limiter.py b/src/app/rate_limiter.py\nindex 000000..e7f8a2 100644\n--- a/src/app/rate_limiter.py\n+++ b/src/app/rate_limiter.py\n@@ -0,0 +1,37 @@\n+import time\n+from collections import defaultdict\n+\n+# Maps IP \u2192 list of request timestamps\n+_request_log: dict = defaultdict(list)\n+RATE_LIMIT = 100  # requests\n+WINDOW_SEC = 60   # per minute\n+\n+def is_allowed(ip: str) -> bool:\n+    \"\"\"Return True if the IP is within its rate limit.\"\"\"\n+    now = time.time()\n+    window_start = now - WINDOW_SEC\n+\n+    # Prune old entries\n+    _request_log[ip] = [t for t in _request_log[ip] if t > window_start]\n+    _request_log[ip].append(now)\n+\n+    # Not thread-safe: concurrent requests can both read len < RATE_LIMIT,\n+    # both append, and both return True \u2014 limit is not enforced under load\n+    return len(_request_log[ip]) <= RATE_LIMIT\n+\n+def get_stats(ip: str) -> dict:\n+    \"\"\"Return rate-limit stats for an IP \u2014 no auth required.\"\"\"\n+    return {\n+        \"ip\": ip,\n+        \"request_count\": len(_request_log[ip]),\n+        \"limit\": RATE_LIMIT,\n+        \"window_sec\": WINDOW_SEC,\n+    }\n+\n+def clear_limit(ip: str) -> None:\n+    \"\"\"Clear rate-limit state for an IP.\"\"\"\n+    # No auth \u2014 any caller can reset any IP's counter\n+    _request_log.pop(ip, None)\n+\n+def reset_all() -> int:\n+    \"\"\"Wipe all rate-limit state \u2014 no auth, no logging.\"\"\"\n+    count = len(_request_log)\n+    _request_log.clear()\n+    return count\n",
  };

  // ---------------------------------------------------------
  // Static config
  // ---------------------------------------------------------

  // Mirrors the API's _NODE_TO_AGENT map (stream.py) — order here is the
  // pipeline order shown on screen.
  const AGENTS = [
    { id: "input_guardrail", label: "Input\nGuardrail", desc: "Scans the raw diff for injection patterns and enforces the max-line size limit before anything else runs." },
    { id: "supervisor", label: "Supervisor", desc: "Builds the ReviewPlan and fans work out to the five worker agents." },
    { id: "static_analysis", label: "Static\nAnalysis", desc: "Rule-based checks — import ordering, formatting, style conventions.", categories: ["style", "type_error"] },
    { id: "security", label: "Security", desc: "SAST + secret-scanner subgraph — looks for security flaws and leaked credentials.", categories: ["security_flaw", "leaked_secret"] },
    { id: "logic_correctness", label: "Logic &\nCorrectness", desc: "Looks for logic bugs, race conditions, and correctness issues.", categories: ["logic_bug"] },
    { id: "code_quality", label: "Code\nQuality", desc: "Maintainability, complexity, and general quality issues.", categories: ["quality"] },
    { id: "documentation", label: "Documen-\ntation", desc: "Flags missing or inadequate docstrings and comments.", categories: ["missing_docs"] },
    { id: "aggregator", label: "Aggregator", desc: "Deduplicates findings across workers and runs the LLM critic loop." },
    { id: "hitl_critical_triage", label: "Gate ·\nCritical", desc: "Pauses for a human decision when a critical-severity finding is present. Skipped entirely if there isn't one.", gate: "gate_critical_triage" },
    { id: "hitl_final_approval", label: "Gate ·\nFinal", desc: "Always pauses before the report is generated, for final human sign-off.", gate: "gate_final_approval" },
    { id: "report_generator", label: "Report\nGenerator", desc: "Synthesizes the Markdown report from all confirmed findings." },
  ];
  const AGENT_IDS = new Set(AGENTS.map((a) => a.id));
  const AGENT_BY_ID = Object.fromEntries(AGENTS.map((a) => [a.id, a]));

  const CATEGORY_TO_AGENT = {};
  AGENTS.forEach((a) => (a.categories || []).forEach((c) => (CATEGORY_TO_AGENT[c] = a.id)));

  const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];

  // ---------------------------------------------------------
  // Tiny DOM helpers
  // ---------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const el = (tag, opts) => Object.assign(document.createElement(tag), opts || {});
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function apiBase() {
    return $("#cfg-base").value.trim().replace(/\/+$/, "");
  }
  function apiKey() {
    return $("#cfg-key").value;
  }
  function authHeaders(extra) {
    return Object.assign({ "x-api-key": apiKey() }, extra || {});
  }

  function toast(kind, msg) {
    const stack = $("#toast-stack");
    const t = el("div", { className: "toast " + kind, textContent: msg });
    stack.appendChild(t);
    setTimeout(() => t.remove(), 5200);
  }

  // ---------------------------------------------------------
  // App state (in-memory only — this console keeps no local
  // storage, see the hint text under "Track a review")
  // ---------------------------------------------------------
  const state = {
    currentReviewId: null,
    reviews: new Map(), // review_id -> { status, resolvedGates:Set, agents:{}, findings:[], report, order }
    order: [], // recent review ids, most recent last
    streamController: null,
    streamingReviewId: null,
    pollTimer: null,
    submitInFlight: false,
    resumeInFlight: false,
    selectedAgentId: null,
    detectedGate: null,
  };

  function freshAgentState() {
    const o = {};
    AGENTS.forEach((a) => (o[a.id] = { status: "idle", events: [], startMs: null, endMs: null }));
    return o;
  }

  function ensureReview(id) {
    if (!state.reviews.has(id)) {
      state.reviews.set(id, {
        status: "pending",
        resolvedGates: new Set(),
        agents: freshAgentState(),
        findings: [],
        report: null,
        repo: "",
      });
      state.order.push(id);
    }
    return state.reviews.get(id);
  }

  // ---------------------------------------------------------
  // Health check
  // ---------------------------------------------------------
  async function checkHealth() {
    const dot = $("#health-dot");
    const label = $("#health-label");
    try {
      const resp = await fetch(apiBase() + "/health", { method: "GET" });
      if (resp.ok) {
        dot.className = "dot ok";
        label.textContent = "API online";
      } else {
        dot.className = "dot bad";
        label.textContent = "API error " + resp.status;
      }
    } catch (e) {
      dot.className = "dot bad";
      label.textContent = "API unreachable";
    }
  }

  // ---------------------------------------------------------
  // Fixtures
  // ---------------------------------------------------------
  $$("[data-fixture]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#in-diff").value = FIXTURES[btn.dataset.fixture];
    });
  });
  $("#btn-clear-diff").addEventListener("click", () => {
    $("#in-diff").value = "";
  });

  // ---------------------------------------------------------
  // Submit
  // ---------------------------------------------------------
  $("#btn-submit").addEventListener("click", submitReview);

  async function submitReview() {
    if (state.submitInFlight) return;
    const diff = $("#in-diff").value;
    const errSlot = $("#submit-error");
    errSlot.textContent = "";
    if (!diff.trim()) {
      errSlot.textContent = "Paste a diff, or load one of the fixture patches above.";
      return;
    }
    const repo = $("#in-repo").value.trim();
    const prRaw = $("#in-pr").value.trim();
    const body = { diff, repo, pr_number: prRaw ? Number(prRaw) : null };

    state.submitInFlight = true;
    const btn = $("#btn-submit");
    btn.disabled = true;
    btn.textContent = "Submitting…";
    try {
      const resp = await fetch(apiBase() + "/reviews", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        errSlot.textContent = data.detail ? String(data.detail) : "Submit failed (" + resp.status + ")";
        toast("error", errSlot.textContent);
        return;
      }
      ensureReview(data.review_id).repo = repo;
      toast("success", "Review submitted — " + data.review_id.slice(0, 8) + "…");
      attachToReview(data.review_id);
    } catch (e) {
      errSlot.textContent = "Could not reach the API at " + apiBase() + ". Is `argus serve` running, and CORS enabled?";
      toast("error", errSlot.textContent);
    } finally {
      state.submitInFlight = false;
      btn.disabled = false;
      btn.textContent = "Submit for review";
    }
  }

  // ---------------------------------------------------------
  // Attach / recent list
  // ---------------------------------------------------------
  $("#btn-attach").addEventListener("click", () => {
    const id = $("#in-attach-id").value.trim();
    if (!id) return;
    attachToReview(id);
  });
  $("#in-attach-id").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#btn-attach").click();
  });

  function attachToReview(id) {
    ensureReview(id);
    state.currentReviewId = id;
    state.selectedAgentId = null;
    renderRecentList();
    $("#pipeline-panel").style.display = "";
    $("#findings-panel").style.display = "";
    $("#report-panel").style.display = "";
    renderPipeline();
    renderAgentDetail();
    renderFindings();
    renderReportPlaceholder();
    $("#pt-rid").textContent = id;
    clearConsole();
    startStream(id);
    refreshStatus(id);
    startPolling(id);
  }

  function renderRecentList() {
    const wrap = $("#recent-list");
    wrap.innerHTML = "";
    if (state.order.length === 0) {
      wrap.appendChild(el("div", { className: "hint", textContent: "No reviews tracked yet in this tab." }));
      return;
    }
    state.order
      .slice()
      .reverse()
      .forEach((id) => {
        const r = state.reviews.get(id);
        const chip = el("div", { className: "recent-chip" + (id === state.currentReviewId ? " active" : "") });
        chip.appendChild(el("span", { className: "rid mono", textContent: id.slice(0, 13) + "…" }));
        chip.appendChild(el("span", { className: "rstatus badge " + statusClass(r.status), textContent: r.status }));
        chip.addEventListener("click", () => attachToReview(id));
        wrap.appendChild(chip);
      });
  }

  function statusClass(s) {
    return ["pending", "running", "awaiting_human", "published", "failed"].includes(s) ? s : "unknown";
  }

  // ---------------------------------------------------------
  // SSE streaming (via fetch — EventSource can't send custom
  // headers, and the API requires x-api-key on every request)
  // ---------------------------------------------------------
  function setStreamState(kind, label) {
    $("#stream-dot").className = "dot " + kind;
    $("#stream-label").textContent = label;
  }

  async function startStream(reviewId) {
    // Only one live subscription driven by this tab at a time. Multiple
    // *subscribers* to the same review are fine (the backend fans events
    // out to all of them) — this guard just avoids leaking readers as the
    // user clicks around.
    if (state.streamController) {
      state.streamController.abort();
      state.streamController = null;
    }
    const controller = new AbortController();
    state.streamController = controller;
    state.streamingReviewId = reviewId;
    setStreamState("checking", "connecting…");

    try {
      const resp = await fetch(apiBase() + "/reviews/" + reviewId + "/stream", {
        headers: authHeaders(),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        setStreamState("bad", "stream unavailable (" + resp.status + ")");
        return;
      }
      setStreamState("ok", "live");
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const chunks = buf.split("\n\n");
        buf = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.trim()) continue;
          let eventName = "message";
          let dataStr = "";
          for (const line of chunk.split("\n")) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          let data = null;
          try {
            data = JSON.parse(dataStr);
          } catch (e) {
            continue;
          }
          handleStreamEvent(reviewId, eventName, data);
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") {
        setStreamState("bad", "disconnected");
      }
      return;
    }
    if (state.streamingReviewId === reviewId) setStreamState("", "closed");
  }

  function handleStreamEvent(reviewId, eventName, data) {
    logConsole(data);
    if (eventName === "done") {
      // Graph reached END or a HITL interrupt — the SSE connection closes
      // either way. Reconcile against real backend state.
      refreshStatus(reviewId);
      return;
    }
    if (eventName === "error") {
      toast("error", "Stream error: " + (data.error || "unknown"));
      return;
    }
    if (eventName === "connected") return;

    const agentId = data.agent;
    const rev = ensureReview(reviewId);
    if (agentId && AGENT_IDS.has(agentId)) {
      const a = rev.agents[agentId];
      a.events.push({ event: eventName, elapsed_ms: data.elapsed_ms, ts: Date.now() });
      if (eventName === "on_chain_start" && a.status === "idle") {
        a.status = "running";
        a.startMs = data.elapsed_ms;
      } else if (eventName === "on_chain_end") {
        a.status = "done";
        a.endMs = data.elapsed_ms;
      }
      if (reviewId === state.currentReviewId) {
        renderPipelineNode(agentId);
        if (state.selectedAgentId === agentId) renderAgentDetail();
      }
    }
  }

  function logConsole(data) {
    const box = $("#console-log");
    const line = el("div", { className: "cl-line" });
    const t = new Date().toLocaleTimeString();
    line.appendChild(el("span", { className: "cl-time", textContent: t }));
    const agentPart = data.agent ? "[" + data.agent + "] " : "";
    line.appendChild(document.createTextNode(agentPart + (data.event || "") + "  +" + (data.elapsed_ms != null ? data.elapsed_ms : "?") + "ms"));
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    // keep it from growing unbounded across a long session
    while (box.childElementCount > 400) box.removeChild(box.firstChild);
  }

  function clearConsole() {
    $("#console-log").innerHTML = "";
  }

  // ---------------------------------------------------------
  // Pipeline rendering
  // ---------------------------------------------------------
  function eyeSvg() {
    return (
      '<svg class="eye" viewBox="0 0 34 34">' +
      '<path class="lid" d="M3 17 C 9 6, 25 6, 31 17 C 25 28, 9 28, 3 17 Z"/>' +
      '<circle class="iris" cx="17" cy="17" r="6.5"/>' +
      '<circle class="pupil" cx="17" cy="17" r="2.4"/>' +
      "</svg>"
    );
  }

  function renderPipeline() {
    const track = $("#pipeline-track");
    track.innerHTML = "";
    AGENTS.forEach((a, i) => {
      if (i > 0) {
        track.appendChild(el("div", { className: "connector" }));
      }
      const wrap = el("div", { className: "agent-node", title: a.desc });
      wrap.dataset.agentId = a.id;
      wrap.dataset.status = "idle";
      wrap.innerHTML = eyeSvg();
      wrap.appendChild(el("div", { className: "label", textContent: a.label.replace(/\n/g, " ") }));
      wrap.appendChild(el("div", { className: "ms", textContent: "" }));
      wrap.addEventListener("click", () => {
        state.selectedAgentId = a.id;
        renderPipeline(); // refresh selection outline
        renderAgentDetail();
      });
      track.appendChild(wrap);
    });
    // apply current state for the active review
    const rev = state.currentReviewId ? state.reviews.get(state.currentReviewId) : null;
    if (rev) AGENTS.forEach((a) => applyNodeState(a.id, rev.agents[a.id]));
    if (state.selectedAgentId) {
      const node = track.querySelector('[data-agent-id="' + state.selectedAgentId + '"]');
      if (node) node.classList.add("selected");
    }
  }

  function renderPipelineNode(agentId) {
    const rev = state.reviews.get(state.currentReviewId);
    if (!rev) return;
    applyNodeState(agentId, rev.agents[agentId]);
  }

  function applyNodeState(agentId, agentState_) {
    const node = document.querySelector('#pipeline-track [data-agent-id="' + agentId + '"]');
    if (!node || !agentState_) return;
    node.dataset.status = agentState_.status;
    const msLabel = node.querySelector(".ms");
    if (agentState_.status === "running") msLabel.textContent = "running…";
    else if (agentState_.status === "awaiting") msLabel.textContent = "paused";
    else if (agentState_.status === "done" && agentState_.endMs != null) msLabel.textContent = "+" + agentState_.endMs + "ms";
    else if (agentState_.status === "failed") msLabel.textContent = "failed";
    else msLabel.textContent = "";
  }

  function renderAgentDetail() {
    const box = $("#agent-detail");
    const id = state.selectedAgentId;
    if (!id) {
      box.innerHTML =
        '<div class="ad-title">Select an agent</div><div class="ad-desc">Click any eye above to see its lifecycle events and the findings it produced.</div>';
      return;
    }
    const meta = AGENT_BY_ID[id];
    const rev = state.currentReviewId ? state.reviews.get(state.currentReviewId) : null;
    const a = rev ? rev.agents[id] : { status: "idle", events: [] };

    box.innerHTML = "";
    box.appendChild(
      el("div", {
        className: "ad-title",
        innerHTML: esc(meta.label.replace(/\n/g, " ")) + ' <span class="badge ' + badgeClassForAgent(a.status) + '">' + a.status + "</span>",
      })
    );
    box.appendChild(el("div", { className: "ad-desc", textContent: meta.desc }));

    const evWrap = el("div", { className: "ad-events" });
    if (a.events.length === 0) {
      evWrap.appendChild(el("div", { className: "hint", textContent: "No lifecycle events observed yet for this run." }));
    } else {
      a.events.forEach((e) => {
        const row = el("div", { className: "ad-event-row" });
        row.appendChild(el("span", { className: "ev", textContent: e.event }));
        row.appendChild(el("span", { textContent: "+" + (e.elapsed_ms != null ? e.elapsed_ms : "?") + "ms" }));
        evWrap.appendChild(row);
      });
    }
    box.appendChild(evWrap);

    if (meta.categories && rev) {
      const matches = rev.findings.filter((f) => meta.categories.includes(f.category));
      box.appendChild(el("div", { className: "ad-findings-title", textContent: "Findings from this agent (" + matches.length + ")" }));
      if (matches.length === 0) {
        box.appendChild(el("div", { className: "hint", textContent: "None reported (or the review hasn't reached the aggregator yet)." }));
      } else {
        matches.forEach((f) => {
          const row = el("div", { className: "ad-event-row" });
          row.appendChild(el("span", { className: "sev-badge sev-" + f.severity, textContent: f.severity }));
          row.appendChild(el("span", { className: "loc", textContent: f.file_path + ":" + f.line_start }));
          row.appendChild(el("span", { textContent: f.description }));
          box.appendChild(row);
        });
      }
    }
  }

  function badgeClassForAgent(status) {
    if (status === "running") return "running";
    if (status === "awaiting") return "awaiting_human";
    if (status === "done") return "published";
    if (status === "failed") return "failed";
    return "pending";
  }

  // ---------------------------------------------------------
  // Status polling — the source of truth. SSE tells us about
  // lifecycle timing; this tells us actual persisted status +
  // findings, and is what keeps the UI honest across a server
  // restart (step 11) or a dropped stream.
  // ---------------------------------------------------------
  function startPolling(reviewId) {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (!state.currentReviewId) return;
      const rev = state.reviews.get(state.currentReviewId);
      if (rev && (rev.status === "published" || rev.status === "failed")) return;
      refreshStatus(state.currentReviewId);
    }, 3500);
  }

  async function refreshStatus(reviewId) {
    try {
      const resp = await fetch(apiBase() + "/reviews/" + reviewId, { headers: authHeaders() });
      if (resp.status === 404) return;
      const data = await resp.json();
      if (!resp.ok) return;
      const rev = ensureReview(reviewId);
      const prevStatus = rev.status;
      rev.status = data.status;
      rev.findings = data.findings || [];

      if (reviewId === state.currentReviewId) {
        $("#pt-status").className = "badge " + statusClass(rev.status);
        $("#pt-status").textContent = rev.status;
        renderFindings();
        renderRecentList();
      }

      if (rev.status === "awaiting_human") {
        markAwaitingGate(reviewId);
        if (reviewId === state.currentReviewId) showGatePanel(reviewId);
      } else if (reviewId === state.currentReviewId) {
        hideGatePanel();
      }

      if (rev.status === "published" && prevStatus !== "published") {
        if (reviewId === state.currentReviewId) {
          markAgentDone("report_generator");
          fetchReport(reviewId, /*silent*/ true);
        }
      }
      if (rev.status === "failed") {
        toast("error", "Review " + reviewId.slice(0, 8) + "… failed. Check the server logs.");
      }
    } catch (e) {
      // Server likely restarting (this is expected mid step-11 test) —
      // stay quiet, the next poll tick will retry.
    }
  }

  function markAgentDone(agentId) {
    const rev = state.reviews.get(state.currentReviewId);
    if (!rev) return;
    if (rev.agents[agentId].status !== "done") {
      rev.agents[agentId].status = "done";
      renderPipelineNode(agentId);
    }
  }

  // Figure out which gate is most likely the one currently paused, using
  // only information this client has observed (no endpoint exposes the
  // paused gate name directly — see the mismatch-correction flow below for
  // how we recover if this guess is wrong).
  function guessGate(reviewId) {
    const rev = state.reviews.get(reviewId);
    if (!rev) return "gate_final_approval";
    const hasCritical = rev.findings.some((f) => f.severity === "critical");
    if (hasCritical && !rev.resolvedGates.has("gate_critical_triage")) return "gate_critical_triage";
    return "gate_final_approval";
  }

  function markAwaitingGate(reviewId) {
    const rev = state.reviews.get(reviewId);
    if (!rev) return;
    const gate = guessGate(reviewId);
    const agentId = gate === "gate_critical_triage" ? "hitl_critical_triage" : "hitl_final_approval";
    if (rev.agents[agentId].status !== "done") {
      rev.agents[agentId].status = "awaiting";
      if (reviewId === state.currentReviewId) renderPipelineNode(agentId);
    }
  }

  // ---------------------------------------------------------
  // Findings table
  // ---------------------------------------------------------
  function renderFindings() {
    const rev = state.reviews.get(state.currentReviewId);
    const wrap = $("#findings-table-wrap");
    const findings = rev ? rev.findings.slice() : [];
    $("#findings-count").textContent = String(findings.length);
    if (findings.length === 0) {
      wrap.innerHTML = '<div class="empty-state">No findings yet.</div>';
      return;
    }
    findings.sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity));
    const table = el("table", { className: "findings" });
    table.innerHTML =
      "<thead><tr><th>Severity</th><th>Category</th><th>Agent</th><th>Location</th><th>Description</th><th>Confidence</th><th>Status</th></tr></thead>";
    const tbody = el("tbody");
    findings.forEach((f) => {
      const tr = el("tr");
      tr.innerHTML =
        '<td><span class="sev-badge sev-' + esc(f.severity) + '">' + esc(f.severity) + "</span></td>" +
        "<td>" + esc(f.category) + "</td>" +
        "<td>" + esc(CATEGORY_TO_AGENT[f.category] || "—") + "</td>" +
        '<td class="loc">' + esc(f.file_path) + ":" + esc(f.line_start) + "</td>" +
        "<td>" + esc(f.description) + "</td>" +
        "<td>" + (f.confidence != null ? Math.round(f.confidence * 100) + "%" : "—") + "</td>" +
        "<td>" + esc(f.status) + "</td>";
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.innerHTML = "";
    wrap.appendChild(table);
  }

  // ---------------------------------------------------------
  // HITL gate panel
  // ---------------------------------------------------------
  function showGatePanel(reviewId) {
    const panel = $("#gate-panel");
    const gate = guessGate(reviewId);
    $("#gate-select").value = gate;
    $("#gate-action").value = gate === "gate_critical_triage" ? "confirm" : "approve";
    $("#gate-panel-desc").textContent =
      gate === "gate_critical_triage"
        ? "A critical-severity finding was reported. Confirm to proceed to final approval, or reject to stop the review here."
        : "This always runs before the report is generated. Approve to publish the report.";
    $("#gate-mismatch").style.display = "none";
    panel.style.display = "";
  }
  function hideGatePanel() {
    $("#gate-panel").style.display = "none";
    $("#gate-mismatch").style.display = "none";
  }

  $("#gate-select").addEventListener("change", () => {
    const gate = $("#gate-select").value;
    $("#gate-action").value = gate === "gate_critical_triage" ? "confirm" : "approve";
  });

  $("#btn-resume").addEventListener("click", resumeGate);

  async function resumeGate() {
    if (state.resumeInFlight) return;
    const reviewId = state.currentReviewId;
    if (!reviewId) return;
    const gate = $("#gate-select").value;
    const action = $("#gate-action").value;
    const comment = $("#gate-comment").value;
    const btn = $("#btn-resume");

    state.resumeInFlight = true;
    btn.disabled = true;
    btn.textContent = "Resuming…";
    $("#gate-mismatch").style.display = "none";

    // Reconnect the stream *before* resuming so we don't miss the next
    // leg's lifecycle events — the resume call blocks until the graph
    // hits the next interrupt or completes.
    startStream(reviewId);

    try {
      const resp = await fetch(apiBase() + "/reviews/" + reviewId + "/resume", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ decision: { gate, action, comment } }),
      });
      const data = await resp.json().catch(() => ({}));

      if (resp.status === 409) {
        // The gate we guessed doesn't match where the graph is actually
        // paused. The API's error message names the real one — use it
        // rather than guessing again.
        const detail = data.detail || "";
        const m = /paused at \[(.*?)\]/.exec(detail);
        let real = null;
        if (m) {
          const found = m[1].match(/'([^']+)'/);
          if (found) real = found[1];
        }
        if (real && real.startsWith("gate_")) {
          $("#gate-select").value = real;
          $("#gate-action").value = real === "gate_critical_triage" ? "confirm" : "approve";
          $("#gate-mismatch").textContent = "Wrong gate — the review is actually paused at " + real + ". Updated the selection above; click Resume again.";
        } else {
          $("#gate-mismatch").textContent = detail || "Gate mismatch — the review isn't paused where expected.";
        }
        $("#gate-mismatch").style.display = "";
        return;
      }

      if (!resp.ok) {
        toast("error", data.detail ? String(data.detail) : "Resume failed (" + resp.status + ")");
        return;
      }

      const rev = ensureReview(reviewId);
      rev.resolvedGates.add(gate);
      markAgentDone(gate === "gate_critical_triage" ? "hitl_critical_triage" : "hitl_final_approval");
      toast("success", gate + " → " + action + ". New status: " + data.status);
      hideGatePanel();
      refreshStatus(reviewId);
    } catch (e) {
      toast("error", "Could not reach the API to resume this review.");
    } finally {
      state.resumeInFlight = false;
      btn.disabled = false;
      btn.textContent = "Resume";
    }
  }

  // ---------------------------------------------------------
  // Report
  // ---------------------------------------------------------
  $("#btn-fetch-report").addEventListener("click", () => fetchReport(state.currentReviewId, false));
  let rawReportVisible = false;
  $("#btn-toggle-raw").addEventListener("click", () => {
    rawReportVisible = !rawReportVisible;
    const rev = state.reviews.get(state.currentReviewId);
    if (!rev || !rev.report) return;
    renderReport(rev.report);
  });

  function renderReportPlaceholder() {
    const rev = state.reviews.get(state.currentReviewId);
    if (rev && rev.report) {
      renderReport(rev.report);
    } else {
      $("#report-content").innerHTML =
        '<div class="empty-state">No report yet — this fills in once the review reaches <code>published</code>.</div>';
      $("#btn-download-report").style.display = "none";
      $("#btn-toggle-raw").style.display = "none";
    }
  }

  async function fetchReport(reviewId, silent) {
    if (!reviewId) return;
    try {
      const resp = await fetch(apiBase() + "/reviews/" + reviewId + "/report", { headers: authHeaders() });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        if (!silent) toast("error", data.detail ? String(data.detail) : "No report yet.");
        return;
      }
      const rev = ensureReview(reviewId);
      rev.report = data.content_markdown;
      if (reviewId === state.currentReviewId) renderReport(data.content_markdown);
    } catch (e) {
      if (!silent) toast("error", "Could not reach the API to fetch the report.");
    }
  }

  function renderReport(markdown) {
    const box = $("#report-content");
    $("#btn-download-report").style.display = "";
    $("#btn-toggle-raw").style.display = "";
    if (rawReportVisible) {
      box.innerHTML = "";
      box.appendChild(el("pre", { textContent: markdown }));
    } else if (window.marked) {
      box.innerHTML = window.marked.parse(markdown);
    } else {
      box.innerHTML = "";
      box.appendChild(el("pre", { textContent: markdown }));
    }
  }

  $("#btn-download-report").addEventListener("click", () => {
    const rev = state.reviews.get(state.currentReviewId);
    if (!rev || !rev.report) return;
    const blob = new Blob([rev.report], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: "argus-report-" + state.currentReviewId.slice(0, 8) + ".md" });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });

  // ---------------------------------------------------------
  // Misc buttons
  // ---------------------------------------------------------
  $("#btn-copy-id").addEventListener("click", () => {
    if (!state.currentReviewId) return;
    navigator.clipboard?.writeText(state.currentReviewId);
    toast("info", "Review ID copied.");
  });
  $("#btn-refresh-status").addEventListener("click", () => {
    if (state.currentReviewId) refreshStatus(state.currentReviewId);
  });

  $("#btn-metrics").addEventListener("click", async () => {
    const pre = $("#metrics-pre");
    pre.textContent = "Loading…";
    try {
      const resp = await fetch(apiBase() + "/metrics");
      const text = await resp.text();
      const lines = text.split("\n").filter((l) => l.startsWith("argus_"));
      pre.textContent = lines.length ? lines.join("\n") : "(no argus_ metrics reported yet)";
    } catch (e) {
      pre.textContent = "Could not reach " + apiBase() + "/metrics";
    }
  });

  // ---------------------------------------------------------
  // Init
  // ---------------------------------------------------------
  checkHealth();
  setInterval(checkHealth, 6000);
  renderRecentList();
  renderPipeline();
})();
