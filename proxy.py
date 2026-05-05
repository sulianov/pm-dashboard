"""
PM Dashboard — Local Proxy Server
Runs on http://localhost:8765

Routes:
  GET  /                          → dashboard.html
  GET  /api/roles                 → roles.json (local person directory)
  GET  /api/boards                → boards.json (board ID directory)
  POST /api/jira                  → Jira search (read)
  POST /api/jira/update           → Jira issue update — fields write (REST API v2)
  POST /api/jira/sprints          → List active/future sprints for a board (Agile API)
  POST /api/jira/sprint/assign    → Move issue to sprint or backlog (Agile API)
  POST /api/jira/jpo-teams        → Portfolio team members (JPO internal API)
  POST /api/velocity              → 30-day resolved SP per dev (Dev Owner field)
  POST /api/active-sprints        → Active sprint dates across given board IDs
  POST /api/last-sprint           → Most recently closed sprint across board IDs
  POST /api/sprint-burn           → SP resolved in a given sprint per dev
  POST /api/suggest-owner         → AI dev-owner suggestion via GitHub Models
  OPTIONS *                       → CORS preflight

Usage:
  python proxy.py
  Then open http://localhost:8765/ in your browser.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import threading

import signal
import sys

def shutdown(sig, frame):
    print("Shutting down proxy...")
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


PORT        = 8765
HTML_FILE   = "dashboard.html"
ROLES_FILE  = "roles.json"
BOARDS_FILE = "boards.json"
CONFIG_FILE = "config.json"

# ── Load instance config ───────────────────────────────────────────────────
def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[WARN] {CONFIG_FILE} not found — using built-in defaults")
        return {}
    except Exception as e:
        print(f"[WARN] Failed to parse {CONFIG_FILE}: {e} — using built-in defaults")
        return {}

CFG = _load_config()

# Convenience accessors with fallback defaults
_F   = CFG.get("fields", {})
F_DEV_OWNER   = _F.get("devOwner",   "customfield_10125")
F_SP          = _F.get("storyPoints","customfield_10006")
_WF  = CFG.get("workflow", {})
VEL_RESOLUTIONS    = _WF.get("velocityResolutions", ["Done", "Fixed"])
LEFT_TO_BURN_STATUSES = _WF.get("leftToBurnStatuses",
    ["Ready for Build", "Verified", "Build", "To Do", "In Progress"])
# JQL fragments derived from config
_CF_DEV_OWNER_JQL   = f"cf[{F_DEV_OWNER.replace('customfield_', '')}]"
_VEL_RES_JQL        = ", ".join(VEL_RESOLUTIONS)          # e.g. "Done, Fixed"
_LEFT_TO_BURN_JQL   = ", ".join(f'"{s}"' for s in LEFT_TO_BURN_STATUSES)

# ── PR search helpers ──────────────────────────────────────────────────────
from datetime import datetime, timezone

def _days_ago(iso_str):
    """Return integer days between now and an ISO-8601 date string."""
    if not iso_str:
        return 0
    try:
        # Handle both Z-suffix and +00:00 style
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return 0

def _gh_state(item):
    """Normalise GitHub issue/PR state field."""
    if item.get("draft"):
        return "draft"
    pr = item.get("pull_request", {})
    if pr.get("merged_at"):
        return "merged"
    return item.get("state", "open")   # "open" or "closed"

def _ado_state(item):
    """Normalise ADO PR status to a common vocabulary."""
    s = (item.get("status") or "").lower()
    mapping = {"active": "open", "completed": "merged", "abandoned": "abandoned"}
    return mapping.get(s, s)


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} → {fmt % args}")

    # ── CORS headers ────────────────────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Jira-Base")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── GET: static HTML or local data files ──────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/roles":
            self._handle_get_roles()
            return
        if path == "/api/boards":
            self._handle_get_boards()
            return
        if path == "/api/config":
            self._send_json(200, CFG)
            return
        try:
            with open(HTML_FILE, "rb") as f:
                html = f.read().decode("utf-8")
            # Inject JIRA_CFG before </head> so JS constants can read it
            inject = (
                "<script>\n"
                f"window.JIRA_CFG = {json.dumps(CFG, ensure_ascii=False)};\n"
                "</script>\n"
            )
            html = html.replace("</head>", inject + "</head>", 1)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, f"{HTML_FILE} not found")
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    # ── Jira proxy ──────────────────────────────────────────────────────────
    def do_POST(self):
        length   = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        path     = self.path.split("?")[0]

        if path == "/api/jira":
            self._handle_jira(raw_body)
        elif path == "/api/jira/update":
            self._handle_jira_update(raw_body)
        elif path == "/api/jira/sprints":
            self._handle_jira_sprints(raw_body)
        elif path == "/api/jira/sprint/assign":
            self._handle_jira_sprint_assign(raw_body)
        elif path == "/api/jira/jpo-teams":
            self._handle_jpo_teams(raw_body)
        elif path == "/api/velocity":
            self._handle_velocity(raw_body)
        elif path == "/api/active-sprints":
            self._handle_active_sprints(raw_body)
        elif path == "/api/last-sprint":
            self._handle_last_sprint(raw_body)
        elif path == "/api/sprint-burn":
            self._handle_sprint_burn(raw_body)
        elif path == "/api/sprint-committed":
            self._handle_sprint_committed(raw_body)
        elif path == "/api/suggest-owner":
            self._handle_suggest_owner(raw_body)
        elif path == "/api/pr-search":
            self._handle_pr_search(raw_body)
        else:
            self._send_json(404, {"error": f"Unknown route: {path}"})

    # ── GET /api/roles — local person directory ─────────────────────────────
    def _handle_get_roles(self):
        try:
            if os.path.exists(ROLES_FILE):
                with open(ROLES_FILE, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                body = b"[]"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            print(f"  ← [ROLES] served {len(body)} bytes")
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── GET /api/boards — local board ID directory ──────────────────────────────
    def _handle_get_boards(self):
        try:
            if os.path.exists(BOARDS_FILE):
                with open(BOARDS_FILE, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            else:
                body = b"[]"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            print(f"  \u2190 [BOARDS] served {len(body)} bytes")
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── Jira issue update (PUT via POST for browser compat) ──────────────────
    def _handle_jira_update(self, raw_body):
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"})
            return
        if not jira_base.startswith('https://'):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"})
            return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        issue_key = data.get("key", "")
        fields    = data.get("fields", {})
        if not issue_key:
            self._send_json(400, {"error": "Missing 'key' in body"})
            return

        target_url = f"{jira_base}/rest/api/2/issue/{issue_key}"
        put_body   = json.dumps({"fields": fields}).encode()
        print(f"  → [UPDATE ISSUE] {target_url} fields={list(fields.keys())}")
        try:
            req = urllib.request.Request(
                target_url,
                data=put_body,
                headers={
                    "Content-Type":   "application/json",
                    "Accept":         "application/json",
                    "Authorization":  auth,
                    "Content-Length": str(len(put_body)),
                },
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body   = resp.read()
                resp_status = resp.status
            print(f"  ← Jira UPDATE: {resp_status}")
            self.send_response(resp_status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body if resp_body else b"{}")
        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← Jira UPDATE error {e.code}: {err_body.decode('utf-8', errors='replace')[:200]}")
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── Jira sprint list (Agile API GET) ────────────────────────────────────
    def _handle_jira_sprints(self, raw_body):
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return
        board_id = data.get("boardId")
        if not board_id:
            self._send_json(400, {"error": "Missing 'boardId'"}); return
        target_url = f"{jira_base}/rest/agile/1.0/board/{board_id}/sprint?state=active,future&maxResults=50"
        print(f"  → [SPRINTS] {target_url}")
        try:
            req = urllib.request.Request(
                target_url,
                headers={"Accept": "application/json", "Authorization": auth},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body, resp_status = resp.read(), resp.status
            print(f"  ← Sprints: {resp_status}")
            self.send_response(resp_status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body if resp_body else b"{}")
        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← Sprints error {e.code}")
            self.send_response(e.code); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── Jira sprint assign (Agile API POST) ──────────────────────────────────
    def _handle_jira_sprint_assign(self, raw_body):
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return
        key       = data.get("key", "")
        sprint_id = data.get("sprintId")   # None → move to backlog
        if not key:
            self._send_json(400, {"error": "Missing 'key'"}); return
        if sprint_id is None:
            target_url = f"{jira_base}/rest/agile/1.0/backlog/issue"
        else:
            target_url = f"{jira_base}/rest/agile/1.0/sprint/{sprint_id}/issue"
        post_body = json.dumps({"issues": [key]}).encode()
        print(f"  → [SPRINT ASSIGN] {target_url} issue={key}")
        try:
            req = urllib.request.Request(
                target_url, data=post_body,
                headers={
                    "Content-Type": "application/json", "Accept": "application/json",
                    "Authorization": auth, "Content-Length": str(len(post_body)),
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body, resp_status = resp.read(), resp.status
            print(f"  ← Sprint Assign: {resp_status}")
            self.send_response(resp_status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body if resp_body else b"{}")
        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← Sprint Assign error {e.code}")
            self.send_response(e.code); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── Jira search ──────────────────────────────────────────────────────────
    def _handle_jira(self, raw_body):
        jira_base  = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth       = self.headers.get("Authorization", "")

        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"})
            return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"})
            return

        target_url = jira_base + "/rest/api/2/search"
        print(f"  → [SEARCH] {target_url}")

        try:
            req = urllib.request.Request(
                target_url,
                data=raw_body,
                headers={
                    "Content-Type":   "application/json",
                    "Accept":         "application/json",
                    "Authorization":  auth,
                    "Content-Length": str(len(raw_body)),
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                resp_body   = resp.read()
                resp_status = resp.status
            print(f"  ← Jira: {resp_status}")
            self.send_response(resp_status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body if resp_body else b"{}")

        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← Jira HTTP error {e.code}: {err_body.decode('utf-8', errors='replace')[:200]}")
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)

        except urllib.error.URLError as e:
            print(f"  ← Connection failed: {e.reason}")
            self._send_json(502, {"error": f"Cannot reach Jira: {e.reason}"})

        except Exception as e:
            print(f"  ← Unexpected error: {e}")
            self._send_json(500, {"error": str(e)})

    # ── JPO team members (Portfolio internal API, cookie-auth) ─────────────
    def _handle_jpo_teams(self, raw_body):
        """
        Proxies POST /jira/rest/jpo/1.0/team/get.

        Auth: prefers Authorization (Basic) header like other routes.
              Falls back to X-Jira-Cookies header if Basic Auth is not accepted
              by the JPO endpoint (which is rare on Server/DC).

        Expected request body (JSON):
          {
            "planId":    578,
            "scenarioId": 597,
            "itemKeys":  ["1152"]
          }

        Standard headers (same as other routes):
          X-Jira-Base: https://your-jira.example.com
          Authorization: Basic <base64>      (preferred)
          X-Jira-Cookies: JSESSIONID=...     (fallback)
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        cookies   = self.headers.get("X-Jira-Cookies", "")

        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        if not auth and not cookies:
            self._send_json(400, {"error": "Provide Authorization (Basic) or X-Jira-Cookies header"}); return

        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        plan_id     = data.get("planId")
        scenario_id = data.get("scenarioId")
        item_keys   = data.get("itemKeys", [])

        if not plan_id or not scenario_id:
            self._send_json(400, {"error": "Missing 'planId' or 'scenarioId'"}); return
        if not item_keys:
            self._send_json(400, {"error": "Missing 'itemKeys' array"}); return

        target_url = f"{jira_base}/rest/jpo/1.0/team/get"
        jpo_body   = json.dumps({
            "planId":     plan_id,
            "scenarioId": scenario_id,
            "itemKeys":   [str(k) for k in item_keys],
        }).encode()

        req_headers = {
            "Content-Type":   "application/json",
            "Accept":         "application/json",
            "Content-Length": str(len(jpo_body)),
        }
        if auth:
            req_headers["Authorization"] = auth
        if cookies:
            req_headers["Cookie"] = cookies

        print(f"  → [JPO TEAMS] {target_url} keys={item_keys} auth={'basic' if auth else 'cookie'}")
        try:
            req = urllib.request.Request(
                target_url,
                data=jpo_body,
                headers=req_headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body, resp_status = resp.read(), resp.status
            print(f"  ← JPO teams: {resp_status}")

            # Parse and reshape into a simpler structure for the UI
            raw = json.loads(resp_body)
            person_map = {p["itemKey"]: p for p in raw.get("persons", [])}

            teams_out = []
            for team in raw.get("teams", []):
                seen_keys = set()
                members = []
                for r in team.get("resources", []):
                    if r.get("scenarioType") == "DELETED":
                        continue
                    p = person_map.get(r["personItemKey"])
                    if p and p["itemKey"] not in seen_keys:
                        seen_keys.add(p["itemKey"])
                        members.append({
                            "name":     p["values"]["title"],
                            "username": p["jiraUser"]["jiraUsername"],
                            "itemKey":  p["itemKey"],
                        })
                teams_out.append({
                    "itemKey": team["itemKey"],
                    "title":   team["values"]["title"],
                    "members": members,
                })

            self._send_json(200, {"teams": teams_out})

        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← JPO teams error {e.code}: {err_body.decode('utf-8', errors='replace')[:200]}")
            self.send_response(e.code); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── POST /api/velocity — 30-day resolved SP per dev (Dev Owner field) ──
    def _handle_velocity(self, raw_body):
        """
        Body: { "usernames": ["u1", "u2"] }
        Headers: X-Jira-Base, Authorization
        Returns: { "u1": { "velocitySP": 21.0 }, "u2": { "velocitySP": 8.0 }, ... }
        Counts SP on tickets where cf[10125] (Dev Owner) is the user
        AND resolution in (Done, Fixed) AND resolutiondate >= -30d.
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        usernames = data.get("usernames", [])
        if not usernames:
            self._send_json(400, {"error": "Missing usernames"}); return

        projects = data.get("projects", [])
        user_clause = ", ".join(f'"{u}"' for u in usernames)
        proj_clause = (
            "project in (" + ", ".join(f'"{p}"' for p in projects) + ") AND "
            if projects else ""
        )

        # ── Query 1: velocity — 30d SP totals ──────────────────────────────────
        jql_vel = (
            f"{proj_clause}{_CF_DEV_OWNER_JQL} in ({user_clause}) "
            f"AND resolution in ({_VEL_RES_JQL}) "
            f"AND resolutiondate >= -30d"
        )

        # ── Query 2: recent area — 14d summaries only ────────────────────────
        jql_area = (
            f"{proj_clause}{_CF_DEV_OWNER_JQL} in ({user_clause}) "
            f"AND resolution in ({_VEL_RES_JQL}) "
            f"AND resolutiondate >= -14d"
        )

        target_url = jira_base + "/rest/api/2/search"

        vel_payload = json.dumps({
            "jql":        jql_vel,
            "fields":     [F_DEV_OWNER, F_SP],
            "maxResults": 500,
        }).encode()

        area_payload = json.dumps({
            "jql":        jql_area,
            "fields":     [F_DEV_OWNER, "summary"],
            "maxResults": 200,
        }).encode()

        print(f"  \u2192 [VELOCITY] {len(usernames)} devs, 30d velocity + 14d area")
        try:
            # -- velocity query --
            req = urllib.request.Request(
                target_url, data=vel_payload,
                headers={
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "Authorization": auth,
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                vel_issues = json.loads(resp.read()).get("issues", [])

            # -- area query --
            req2 = urllib.request.Request(
                target_url, data=area_payload,
                headers={
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "Authorization": auth,
                },
                method="POST"
            )
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                area_issues = json.loads(resp2.read()).get("issues", [])

            # Sum SP by dev owner username (30d)
            sp_by_user    = {u.lower(): 0.0 for u in usernames}
            count_by_user = {u.lower(): 0   for u in usernames}
            for issue in vel_issues:
                f        = issue["fields"]
                owner    = (f.get(F_DEV_OWNER) or {}).get("name", "").lower()
                sp       = float(f.get(F_SP) or 0)
                if owner in sp_by_user:
                    sp_by_user[owner]    += sp
                    count_by_user[owner] += 1

            # Collect recent summaries by dev owner username (14d)
            recent_by_user = {u.lower(): [] for u in usernames}
            for issue in area_issues:
                f     = issue["fields"]
                owner = (f.get(F_DEV_OWNER) or {}).get("name", "").lower()
                summ  = (f.get("summary") or "").strip()
                if owner in recent_by_user and summ:
                    recent_by_user[owner].append(summ)

            result = {
                u: {
                    "velocitySP":      round(sp_by_user[u.lower()], 1),
                    "resolvedCount":   count_by_user[u.lower()],
                    "recentSummaries": recent_by_user[u.lower()][:10],
                }
                for u in usernames
            }
            print(f"  \u2190 [VELOCITY] {len(vel_issues)} vel tickets, {len(area_issues)} area tickets")
            self._send_json(200, result)

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  \u2190 [VELOCITY] error {e.code}: {err[:200]}")
            self._send_json(e.code, {"error": err[:200]})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── POST /api/active-sprints — sprint dates across given board IDs ────────────
    def _handle_active_sprints(self, raw_body):
        """
        Body: { "boardIds": [123, 456, 789] }
        Headers: X-Jira-Base, Authorization
        Returns: {
          "sprints":      [{ "id", "name", "state", "startDate", "endDate" }, ...],
          "activeSprint": { ... }   ← first active sprint found (deduped)
        }
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        board_ids = data.get("boardIds", [])
        if not board_ids:
            self._send_json(400, {"error": "Missing boardIds"}); return

        seen = {}   # sprint_id → sprint dict, deduped across boards
        for board_id in board_ids:
            url = (
                f"{jira_base}/rest/agile/1.0/board/{board_id}/sprint"
                f"?state=active&maxResults=5"
            )
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "Authorization": auth},
                    method="GET"
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = json.loads(resp.read())
                for s in body.get("values", []):
                    sid = s.get("id")
                    if sid and sid not in seen:
                        seen[sid] = {
                            "id":        sid,
                            "name":      s.get("name", ""),
                            "state":     s.get("state", "").lower(),
                            "startDate": (s.get("startDate") or "")[:10],
                            "endDate":   (s.get("endDate")   or "")[:10],
                        }
            except Exception:
                pass   # non-fatal — skip boards that fail or return no sprint

        sprints = list(seen.values())
        active  = next((s for s in sprints if s["state"] == "active"), None)
        print(f"  \u2190 [ACTIVE SPRINTS] {len(sprints)} unique sprints across {len(board_ids)} boards")
        self._send_json(200, {"sprints": sprints, "activeSprint": active})

    # ── POST /api/last-sprint — most recently closed sprint across board IDs ──
    def _handle_last_sprint(self, raw_body):
        """
        Body: { "boardIds": [123, 456] }
        Headers: X-Jira-Base, Authorization
        Returns: { "lastSprints": [{ id, name, state, startDate, endDate }, ...] }
        For each board: paginates through ALL sprints (closed+active), finds the
        active sprint, and returns the sprint immediately before it (the true
        last sprint). Jira caps maxResults at 50, so we page until isLast=true.
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        board_ids = data.get("boardIds", [])
        if not board_ids:
            self._send_json(400, {"error": "Missing boardIds"}); return

        seen_ids = {}  # sprint_id → sprint dict (deduped across boards)
        for board_id in board_ids:
            try:
                # Paginate through all closed+active sprints (Jira caps at 50/page)
                all_sprints = []
                start_at    = 0
                page_size   = 50
                while True:
                    url = (
                        f"{jira_base}/rest/agile/1.0/board/{board_id}/sprint"
                        f"?state=closed,active&startAt={start_at}&maxResults={page_size}"
                    )
                    req = urllib.request.Request(
                        url,
                        headers={"Accept": "application/json", "Authorization": auth},
                        method="GET"
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        page = json.loads(resp.read())
                    values = page.get("values", [])
                    all_sprints.extend(values)
                    if page.get("isLast", True) or not values:
                        break
                    start_at += page_size

                # Find the active sprint's startDate — must originate from this board
                active = next(
                    (s for s in all_sprints
                     if (s.get("state") or "").lower() == "active"
                     and s.get("originBoardId") == board_id),
                    None
                )
                if not active:
                    continue
                active_start = (active.get("startDate") or "")[:10]

                # Among closed sprints originating from this board, pick the one
                # with the latest completeDate that finished before active sprint started.
                closed = [
                    s for s in all_sprints
                    if (s.get("state") or "").lower() == "closed"
                    and s.get("originBoardId") == board_id
                ]

                # Sort by completeDate descending; fall back to endDate
                def _complete(s):
                    return (s.get("completeDate") or s.get("endDate") or "")[:10]

                candidates = [s for s in closed if _complete(s) and _complete(s) <= active_start]
                if not candidates:
                    # Fallback: any closed sprint from this board with latest completeDate
                    candidates = [s for s in closed if _complete(s)]
                if not candidates:
                    continue

                candidates.sort(key=_complete, reverse=True)
                s   = candidates[0]
                end = _complete(s)
                sid = s.get("id")
                if sid and sid not in seen_ids:
                    seen_ids[sid] = {
                        "id":        sid,
                        "name":      s.get("name", ""),
                        "state":     "closed",
                        "startDate": (s.get("startDate") or "")[:10],
                        "endDate":   end,
                    }
            except Exception:
                pass  # non-fatal — skip boards that fail

        last_sprints = list(seen_ids.values())
        names = ", ".join(sp["name"] for sp in last_sprints) or "none"
        print(f"  \u2190 [LAST SPRINT] {names} across {len(board_ids)} boards")
        self._send_json(200, {"lastSprints": last_sprints})

    # ── POST /api/sprint-burn — SP resolved in given sprints per dev ───────
    def _handle_sprint_burn(self, raw_body):
        """
        Body: { "usernames": ["u1", "u2"], "sprintIds": [456, 789], "projects": [] }
              (legacy "sprintId" single value also accepted)
        Headers: X-Jira-Base, Authorization
        Returns: { "burnByUser": { "u1": 13.0, "u2": 5.0 } }
        Counts SP where Dev Owner = user AND sprint in (...) AND resolution in (Done, Fixed).
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        usernames      = data.get("usernames", [])
        use_open       = data.get("useOpenSprints", False)
        # Accept sprintIds (array) or legacy sprintId (single)
        sprint_ids     = data.get("sprintIds") or ([data["sprintId"]] if data.get("sprintId") else [])
        projects        = data.get("projects", [])
        resolved_after  = (data.get("resolvedAfter")  or "")[:10]  # YYYY-MM-DD
        resolved_before = (data.get("resolvedBefore") or "")[:10]  # YYYY-MM-DD
        if not usernames:
            self._send_json(400, {"error": "Missing usernames"}); return
        if not use_open and not sprint_ids:
            self._send_json(400, {"error": "Missing sprintIds"}); return

        user_clause    = ", ".join(f'"{u}"' for u in usernames)
        proj_clause    = (
            "project in (" + ", ".join(f'"{p}"' for p in projects) + ") AND "
            if projects else ""
        )
        sprint_filter  = ("sprint in openSprints()" if use_open else
                          "sprint in (" + ", ".join(str(int(s)) for s in sprint_ids) + ")")
        resolved_filter = (
            (f' AND resolutiondate >= "{resolved_after}"'  if resolved_after  else "") +
            (f' AND resolutiondate <= "{resolved_before}"' if resolved_before else "")
        )
        jql = (
            f"{proj_clause}{_CF_DEV_OWNER_JQL} in ({user_clause}) "
            f"AND {sprint_filter} "
            f"AND resolution != Unresolved"
            f"{resolved_filter}"
        )
        payload = json.dumps({
            "jql":        jql,
            "fields":     [F_DEV_OWNER, F_SP],
            "maxResults": 500,
        }).encode()

        target_url = jira_base + "/rest/api/2/search"
        print(f"  \u2192 [SPRINT BURN] {'openSprints()' if use_open else sprint_ids} {len(usernames)} devs | after={resolved_after or 'none'} before={resolved_before or 'none'}")
        try:
            req = urllib.request.Request(
                target_url, data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "Authorization": auth,
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                issues = json.loads(resp.read()).get("issues", [])

            sp_by_user = {u.lower(): 0.0 for u in usernames}
            for issue in issues:
                f     = issue["fields"]
                owner = (f.get(F_DEV_OWNER) or {}).get("name", "").lower()
                sp    = float(f.get(F_SP) or 0)
                if owner in sp_by_user:
                    sp_by_user[owner] += sp

            burn = {u: round(sp_by_user[u.lower()], 1) for u in usernames}
            print(f"  \u2190 [SPRINT BURN] {len(issues)} tickets")
            self._send_json(200, {"burnByUser": burn})

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  \u2190 [SPRINT BURN] error {e.code}: {err[:200]}")
            self._send_json(e.code, {"error": err[:200]})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── POST /api/sprint-committed — left to burn per dev in open sprint ──
    def _handle_sprint_committed(self, raw_body):
        """
        Body: { "usernames": ["u1", "u2"], "projects": [] }
        Headers: X-Jira-Base, Authorization
        Returns: { "remainingByUser": { "u1": 12.5, "u2": 0.0 } }
        Counts SP where Dev Owner = user AND sprint in openSprints() AND
        resolution = Unresolved AND status in the active work statuses.
        This is "left to burn" — items still in flight this sprint.
        """
        jira_base = self.headers.get("X-Jira-Base", "").rstrip("/")
        auth      = self.headers.get("Authorization", "")
        if not jira_base:
            self._send_json(400, {"error": "Missing X-Jira-Base header"}); return
        if not jira_base.startswith("https://"):
            self._send_json(400, {"error": "X-Jira-Base must start with https://"}); return
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        usernames = data.get("usernames", [])
        projects  = data.get("projects", [])
        if not usernames:
            self._send_json(400, {"error": "Missing usernames"}); return

        user_clause = ", ".join(f'"{u}"' for u in usernames)
        proj_clause = (
            "project in (" + ", ".join(f'"{p}"' for p in projects) + ") AND "
            if projects else ""
        )
        jql = (
            f"{proj_clause}{_CF_DEV_OWNER_JQL} in ({user_clause}) "
            f"AND sprint in openSprints() "
            f"AND resolution = Unresolved "
            f"AND status in ({_LEFT_TO_BURN_JQL})"
        )
        payload = json.dumps({
            "jql":        jql,
            "fields":     [F_DEV_OWNER, F_SP],
            "maxResults": 500,
        }).encode()

        target_url = jira_base + "/rest/api/2/search"
        print(f"  \u2192 [SPRINT REMAINING] openSprints() {len(usernames)} devs")
        try:
            req = urllib.request.Request(
                target_url, data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                    "Authorization": auth,
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                issues = json.loads(resp.read()).get("issues", [])

            sp_by_user = {u.lower(): 0.0 for u in usernames}
            for issue in issues:
                f     = issue["fields"]
                owner = (f.get(F_DEV_OWNER) or {}).get("name", "").lower()
                sp    = float(f.get(F_SP) or 0)
                if owner in sp_by_user:
                    sp_by_user[owner] += sp

            remaining = {u: round(sp_by_user[u.lower()], 2) for u in usernames}
            print(f"  \u2190 [SPRINT REMAINING] {len(issues)} tickets")
            self._send_json(200, {"remainingByUser": remaining})

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  \u2190 [SPRINT REMAINING] error {e.code}: {err[:200]}")
            self._send_json(e.code, {"error": err[:200]})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── POST /api/suggest-owner — AI dev-owner suggestion via GitHub Models ──
    def _handle_suggest_owner(self, raw_body):
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        github_token = data.get("githubToken", "").strip()
        if not github_token:
            self._send_json(400, {"error": "Missing githubToken in payload"}); return

        ticket     = data.get("ticket", {})
        dev_roster = data.get("devRoster", [])

        # ── Split roster: same team first, others as fallback ──────────────────
        ticket_team = (ticket.get("team") or "").strip().lower()

        same_team  = [d for d in dev_roster if (d.get("squad") or "").strip().lower() == ticket_team]
        other_team = [d for d in dev_roster if (d.get("squad") or "").strip().lower() != ticket_team]

        def fmt_dev(d):
            vel_30d        = d.get("velocitySP")
            committed      = int(d.get("storyPoints", 0))       # current sprint only
            sprint_days    = int(d.get("sprintDays", 10))
            remaining_days = int(d.get("remainingDays", sprint_days))
            current_work   = d.get("currentWork",  [])
            recent_work    = d.get("recentWork",   [])

            if vel_30d is not None:
                sprint_vel    = round(vel_30d * (sprint_days    / 30), 1)
                remaining_vel = round(vel_30d * (remaining_days / 30), 1)
                remaining_cap = round(remaining_vel - committed, 1)
                vel_str = (
                    f"{vel_30d} SP/30d \u2192 {sprint_vel} SP/sprint "
                    f"({remaining_days}d left \u2192 {remaining_vel} SP realistically available)"
                )
            else:
                remaining_cap = "unknown"
                vel_str       = "no history"

            lines = [
                f"  {d['username']:30s} | {committed:3d} SP committed | "
                f"remaining: {remaining_cap} SP | {vel_str} | "
                f"{d.get('squad', '')} | {d.get('role', '')}"
            ]
            sep = "\u00b7 "
            if current_work:
                lines.append(f" Current: {sep.join(current_work[:5])}")
            if recent_work:
                lines.append(f" Recent: {sep.join(recent_work[:5])}")

            return "\n".join(lines)

        same_team_lines  = "\n".join(fmt_dev(d) for d in same_team)  or "  (none)"
        other_team_lines = "\n".join(fmt_dev(d) for d in other_team) or "  (none)"

        # ── Sprint context line ─────────────────────────────────────────
        sprint_ctx = (
            f"Sprint: {ticket.get('sprintName', 'unknown')} "
            f"({ticket.get('sprintTotalDays', '?')} days total, "
            f"{ticket.get('sprintRemainingDays', '?')} days remaining)"
            if ticket.get('sprintName') else "Sprint context: unknown"
        )

        system_prompt = (
            "You are a ticket triage assistant for a software delivery team.\n\n"
            "Your goal is to rank the TOP 5 developers for this ticket by overall suitability. "
            "Score each developer across four dimensions, then produce a weighted total:\n\n"
            "  SCORING DIMENSIONS (weight in brackets):\n"
            "  [40%] AREA MATCH \u2014 do the developer's current or recently resolved tickets "
            "overlap semantically with this ticket? Look for shared feature names, "
            "module names, file references, or functional domain keywords in the summaries. "
            "A strong area match (e.g. same feature explicitly named) scores high even across teams.\n"
            "  [30%] CAPACITY \u2014 use remaining capacity: velocity projected to remaining sprint days "
            "minus currently committed SP. Negative remaining = overloaded = low score.\n"
            "  [20%] TEAM MATCH \u2014 developer is on the same team as the ticket. "
            "Prefer same-team but do NOT exclude cross-team developers \u2014 they must still appear "
            "if their area match or capacity score compensates.\n"
            "  [10%] ROLE FIT \u2014 engineers/developers score higher for bug tickets than QA or SC.\n\n"
            "IMPORTANT: the top 5 must be a GLOBAL ranking across ALL developers listed. "
            "A cross-team developer with a strong area match MUST appear in the top 5 if their "
            "weighted score warrants it. Do not filter out cross-team developers before scoring.\n\n"
            "For each of the top 5, explain the score in plain language \u2014 call out area match "
            "explicitly when it is a factor (quote the matching keywords or ticket titles).\n\n"
            "Respond ONLY as a JSON array (no markdown, no code fences) of exactly 5 objects, "
            "ranked by descending confidence:\n"
            '[{"suggestedUsername":"...","suggestedName":"...","confidence":0.0,'
            '"teamMatch":true,"reasoning":"..."}]'
        )

        user_prompt = (
            f"Ticket: {ticket.get('key', '?')} \u2014 {ticket.get('summary', '?')}\n"
            f"Type: {ticket.get('type', 'unknown')}  "
            f"Domain: {ticket.get('domain', 'unknown')}  "
            f"Team: {ticket.get('team', 'unknown')}  "
            f"POD: {ticket.get('pod', 'unknown')}\n"
            f"{sprint_ctx}\n\n"
            f"When scoring AREA MATCH, compare each developer's current and recent work "
            f"against this ticket's summary: \"{ticket.get('summary', '')}\"\n\n"
            f"ALL developers (score globally, do not filter by team before ranking):\n"
            f"  SAME TEAM ({ticket.get('team', '?')}):\n"
            f"  {'username':30s} | SP committed | remaining | velocity | squad | role\n"
            f"{same_team_lines}\n\n"
            f"  OTHER TEAMS:\n"
            f"  {'username':30s} | SP committed | remaining | velocity | squad | role\n"
            f"{other_team_lines}\n\n"
            "Rank all 5 by weighted score. Cross-team developers must be included if their score warrants it."
        )

        gh_payload = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "max_tokens": 1024,
            "temperature": 0.2,
        }).encode()

        req = urllib.request.Request(
            "https://models.inference.ai.azure.com/chat/completions",
            data=gh_payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {github_token}",
            },
            method="POST",
        )
        print(f"  \u2192 [AI SUGGEST] {ticket.get('key')} \u2014 asking GitHub Models...")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
            resp_data = json.loads(resp_body)
            raw_text  = resp_data["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if model wraps anyway
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            suggestion = json.loads(raw_text)
            # Normalise — model might return a single object if it ignores instructions
            if isinstance(suggestion, dict):
                suggestion = [suggestion]
            # Ensure teamMatch is present on each item; compute server-side as fallback
            for s in suggestion:
                if "teamMatch" not in s:
                    sug_user = (s.get("suggestedUsername") or "").strip().lower()
                    matched  = next((d for d in dev_roster if (d.get("username") or "").strip().lower() == sug_user), None)
                    s["teamMatch"] = bool(matched) and (matched.get("squad") or "").strip().lower() == ticket_team
            # Sort descending by confidence
            suggestion = sorted(suggestion, key=lambda x: x.get("confidence", 0), reverse=True)
            print(f"  \u2190 [AI SUGGEST] {len(suggestion)} suggestions, top={suggestion[0].get('suggestedUsername')} conf={suggestion[0].get('confidence')}")
            self._send_json(200, {"suggestions": suggestion[:5]})
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  \u2190 [AI SUGGEST] error {e.code}: {err[:200]}")
            self._send_json(e.code, {"error": f"GitHub Models error {e.code}: {err[:200]}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── POST /api/pr-search — find PRs mentioning a Jira key ─────────────────
    def _handle_pr_search(self, raw_body):
        try:
            data = json.loads(raw_body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"}); return

        jira_key   = (data.get("jiraKey") or "").strip().upper()
        gh_token   = (data.get("githubToken") or "").strip()
        ado_pat    = (data.get("adoPat") or "").strip()

        if not jira_key:
            self._send_json(400, {"error": "Missing jiraKey"}); return

        pr_sources = CFG.get("prSources", {})
        gh_repos   = pr_sources.get("github", [])
        ado_repos  = pr_sources.get("ado", [])

        results = []
        errors  = []
        lock    = threading.Lock()

        def search_github(repo_cfg):
            label = repo_cfg.get("label", repo_cfg.get("repo", ""))
            owner = repo_cfg.get("owner", "")
            repo  = repo_cfg.get("repo", "")
            if not gh_token:
                with lock:
                    errors.append({"source": "GitHub", "label": label, "error": "No GitHub token provided"})
                return
            # GitHub search: PRs with jira_key in title (type:pr covers all states when state not filtered)
            q = urllib.parse.quote(f"{jira_key} repo:{owner}/{repo} type:pr")
            url = f"https://api.github.com/search/issues?q={q}&per_page=50"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read())
                items = body.get("items", [])
                key_lower = jira_key.lower()
                prs = []
                for it in items:
                    # Exact key match — GitHub search can return partial matches
                    if key_lower not in it.get("title", "").lower():
                        continue
                    created   = it.get("created_at", "")
                    age_days  = _days_ago(created)
                    merged_at = (it.get("pull_request") or {}).get("merged_at") or None
                    prs.append({
                        "source":    "GitHub",
                        "label":     label,
                        "number":    it["number"],
                        "title":     it["title"],
                        "state":     _gh_state(it),
                        "createdAt": created,
                        "mergedAt":  merged_at,
                        "ageDays":   age_days,
                        "comments":  it.get("comments", 0),
                        "url":       it.get("html_url", ""),
                        "author":    (it.get("user") or {}).get("login", ""),
                    })
                with lock:
                    results.extend(prs)
                print(f"  ← [PR SEARCH] GitHub {label}: {len(items)} raw → {len(prs)} matched for {jira_key}")
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                print(f"  ← [PR SEARCH] GitHub {label} error {e.code}: {err[:200]}")
                with lock:
                    errors.append({"source": "GitHub", "label": label, "error": f"HTTP {e.code}"})
            except Exception as e:
                with lock:
                    errors.append({"source": "GitHub", "label": label, "error": str(e)})

        def search_ado(repo_cfg):
            label   = repo_cfg.get("label", repo_cfg.get("repo", ""))
            org     = repo_cfg.get("org", "")
            project = repo_cfg.get("project", "")
            repo    = repo_cfg.get("repo", "")
            if not ado_pat:
                with lock:
                    errors.append({"source": "ADO", "label": label, "error": "No ADO PAT provided"})
                return
            import base64
            b64 = base64.b64encode(f":{ado_pat}".encode()).decode()
            # Search all states by not passing statusFilter (returns active by default) — use all
            url = (
                f"https://dev.azure.com/{urllib.parse.quote(org)}/"
                f"{urllib.parse.quote(project)}/_apis/git/repositories/"
                f"{urllib.parse.quote(repo)}/pullrequests"
                f"?searchCriteria.title={urllib.parse.quote(jira_key)}"
                f"&searchCriteria.status=all&$top=200&api-version=7.1"
            )
            req = urllib.request.Request(url, headers={
                "Authorization": f"Basic {b64}",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read())
                items = body.get("value", [])
                key_lower = jira_key.lower()
                prs = []
                for it in items:
                    # ADO title filter is a loose token match — enforce exact key match here
                    if key_lower not in it.get("title", "").lower():
                        continue
                    created  = it.get("creationDate", "")
                    age_days = _days_ago(created)
                    pr_id    = it["pullRequestId"]
                    pr_url   = (
                        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
                        f"{urllib.parse.quote(project)}/_git/"
                        f"{urllib.parse.quote(repo)}/pullrequest/{pr_id}"
                    )
                    merged_at = it.get("closedDate") if _ado_state(it) == "merged" else None
                    prs.append({
                        "source":    "ADO",
                        "label":     label,
                        "number":    pr_id,
                        "title":     it.get("title", ""),
                        "state":     _ado_state(it),
                        "createdAt": created,
                        "mergedAt":  merged_at,
                        "ageDays":   age_days,
                        "comments":  None,   # not included in list endpoint
                        "url":       pr_url,
                        "author":    (it.get("createdBy") or {}).get("displayName", ""),
                    })
                with lock:
                    results.extend(prs)
                print(f"  ← [PR SEARCH] ADO {label}: {len(items)} raw → {len(prs)} matched for {jira_key}")
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                print(f"  ← [PR SEARCH] ADO {label} error {e.code}: {err[:200]}")
                with lock:
                    errors.append({"source": "ADO", "label": label, "error": f"HTTP {e.code}"})
            except Exception as e:
                with lock:
                    errors.append({"source": "ADO", "label": label, "error": str(e)})

        threads = []
        for rc in gh_repos:
            t = threading.Thread(target=search_github, args=(rc,), daemon=True)
            threads.append(t); t.start()
        for rc in ado_repos:
            t = threading.Thread(target=search_ado, args=(rc,), daemon=True)
            threads.append(t); t.start()
        for t in threads:
            t.join(timeout=20)

        # Sort: open first, then by age descending
        state_order = {"open": 0, "draft": 1, "merged": 2, "closed": 3, "abandoned": 3}
        results.sort(key=lambda p: (state_order.get(p["state"], 9), -(p["ageDays"] or 0)))

        self._send_json(200, {"prs": results, "errors": errors})

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print("=" * 52)
    print("  PM Dashboard — Local Proxy")
    print("=" * 52)
    print(f"  Open: http://localhost:{PORT}/")
    print()
    print("  Keep this window open while using the app.")
    print("  Ctrl+C to stop.")
    print("=" * 52)
    server = HTTPServer(("localhost", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Proxy stopped.")
