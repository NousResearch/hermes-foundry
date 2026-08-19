"""Loopback gateway for Hermes Full Foundry.

The edge talks to two endpoints:

    POST {RouterUrl}/v1/chat/completions   the turn itself
    POST {RouterUrl}/v1/edge-log           our own out-of-band telemetry

The gateway forwards chat requests to Hermes, carries short-lived agent credentials to the local
sidecar, handles attachment handoff, and exports edge diagnostics.

Standard library only, on purpose. No pip install, no lockfile, nothing to audit.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_URL = os.environ["HERMES_URL"].rstrip("/")
HERMES_KEY = os.environ.get("HERMES_KEY", "")
GATEWAY_KEY = os.environ.get("GATEWAY_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

# Shared directory the sidecar spools outbound attachments into. Must match AGENT_OUTBOX in the
# sidecar; both default to the same path so the two halves cannot drift apart silently.
OUTBOX = os.environ.get("AGENT_OUTBOX", "/tmp/agent-outbox")

# Hermes turns are long. This bounds a single upstream call, not the agent's own thinking budget.
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "900"))

# How long a turn will wait for a cold Hermes before giving up (see the retry loop in do_POST).
# 0 preserves the original fail-fast behaviour for the split ACA deployment, where Hermes is a
# separate always-on container and "connection refused" really does mean broken.
HERMES_COLD_START_WAIT = int(os.environ.get("HERMES_COLD_START_WAIT", "0"))

# Where Hermes' stdout is teed, so a 502 can carry its tail (see do_POST). Empty = feature off.
HERMES_LOG_PATH = os.environ.get("HERMES_LOG_PATH", "/tmp/hermes.log")

# Headers the edge sets that Hermes/the router care about. Forwarded verbatim.
PASSTHROUGH = (
    "x-hermes-session-key",
    # These are two different headers with two different jobs, per Hermes'
    # gateway/platforms/api_server.py: Session-KEY scopes long-term memory, Session-ID is what
    # continues the conversation ("Allow caller to continue an existing session by passing
    # X-Hermes-Session-Id. When provided, history is loaded from state.db instead of from the
    # request body.").
    #
    # This list is an ALLOW-LIST: anything not named here is stripped. Adding a header at the edge is
    # therefore only half a change; update and test both ends.
    "x-hermes-session-id",
    "x-identity-assertion",
    "x-agent-graph-token",
    "x-agent-upn",
)


# Last agent Graph token seen on a turn, held in memory only. Served to the m365 sidecar via
# GET /v1/agent-token. Never written to disk, never logged.
_agent_token = {"token": "", "upn": "", "tier": "outsider"}

# Files the sidecar produced during the current turn that belong on the reply as attachments.
#
# WHY THIS IS NOT SENT THROUGH HERMES. An image is bytes, and the only channel between the sidecar
# and the edge that passes through Hermes is the model's own text. A 180 KB PNG is ~246 KB of
# base64: no model reproduces that verbatim, and asking one to would spend ~60k tokens failing. So
# the sidecar spools the file to a shared path (same container: edge :8088, gateway :8080, sidecar,
# Hermes) and registers it here; the bytes travel on the filesystem and only a path crosses a wire.
#
# Turn-scoped by construction: the list is DRAINED onto the response that ends the turn. A file
# registered during turn N therefore cannot leak onto turn N+1's reply, which matters because the
# same container serves the same person repeatedly and a stale screenshot appearing under an
# unrelated answer would be both confusing and a small privacy leak.
_pending_attachments = []
_attachments_lock = threading.Lock()


def _drain_attachments():
    """Pop everything registered this turn and render it as a response header for the edge."""
    with _attachments_lock:
        items, _pending_attachments[:] = list(_pending_attachments), []
    if not items:
        return {}
    log(f"[edge] attach.handoff | {len(items)} file(s) -> edge")
    return {"X-Agent-Attachments": json.dumps(items, separators=(",", ":"))}


def _discard_stale_attachments():
    """Drop anything left over from a previous turn, and delete the files.

    Called at the START of every turn. The end-of-turn drain already scopes the normal path, but a
    turn that fails — Hermes 500s, the upstream times out — never reaches it, and its spooled file
    would then ride along on the NEXT reply. Clearing on entry makes that impossible by
    construction rather than by remembering to drain on every error path.
    """
    with _attachments_lock:
        stale, _pending_attachments[:] = list(_pending_attachments), []
    for it in stale:
        try:
            os.unlink(it["path"])
        except OSError:
            pass
    if stale:
        log(f"[edge] attach.stale.dropped | {len(stale)} file(s) from an earlier turn")


def log(*parts):
    # Unbuffered so `az containerapp logs show --follow` is actually live.
    print(*parts, flush=True)
    _ship(" ".join(str(p) for p in parts))


# ---- optional Application Insights diagnostics ------------------------------------------------
# Use a non-reserved explicit variable first and fall back to the platform connection string. The
# shipper is standard-library-only, fire-and-forget, and never allowed to break a turn.
_AI_CS = (os.environ.get("FULL_FOUNDRY_AI_CONNECTION_STRING", "")
          or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", ""))
_AI_IKEY = ""
_AI_URL = ""
if _AI_CS:
    _parts = dict(
        kv.split("=", 1) for kv in _AI_CS.split(";") if "=" in kv
    )
    _AI_IKEY = _parts.get("InstrumentationKey", "")
    _AI_URL = (_parts.get("IngestionEndpoint", "") or "").rstrip("/") + "/v2/track"


def _ship(message: str, level: int = 1):
    """Fire-and-forget a MessageData envelope to App Insights. Never raises."""
    if not (_AI_IKEY and _AI_URL and message):
        return

    def _send():
        try:
            body = json.dumps({
                "name": "Microsoft.ApplicationInsights.Message",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "iKey": _AI_IKEY,
                "tags": {"ai.cloud.role": "plan-f-gateway",
                         "ai.cloud.roleInstance": os.environ.get("HOSTNAME", "plan-f")},
                "data": {"baseType": "MessageData",
                         "baseData": {"ver": 2, "message": message[:8000],
                                      "severityLevel": level,
                                      "properties": {"source": "plan-f-gateway"}}},
            }).encode()
            req = urllib.request.Request(_AI_URL, data=body,
                                        headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass                                              # diagnostics must never break a turn

    threading.Thread(target=_send, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # The default logs one line per request to stderr, which drowns the [edge] lines we care about.
    def log_message(self, fmt, *args):
        pass

    def _authorised(self):
        if not GATEWAY_KEY:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {GATEWAY_KEY}"

    def _send(self, code, body=b"", ctype="application/json", extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path in ("/healthz", "/"):
            self._send(200, b'{"ok":true}')
            return
        # The m365 MCP sidecar polls this for the agent's current Graph token. The sidecar sits next
        # to Hermes as a long-lived container, so it cannot take the token from an env var captured
        # at start the way the per-turn sandbox sidecar does.
        #
        # Deliberately in-memory and never written to disk: this is a user token for the AGENT, and
        # persisting it would create the standing credential the whole design avoids. If no turn has
        # carried one yet, say so plainly rather than returning an empty 200 that reads like success.
        if self.path == "/v1/agent-token":
            if not self._authorised():
                self._send(401, b'{"error":"unauthorized"}')
                return
            tok = _agent_token.get("token", "")
            if not tok:
                self._send(404, b'{"error":"no agent token seen yet"}')
                return
            self._send(200, json.dumps({"token": tok, "upn": _agent_token.get("upn", ""),
                                        "tier": _agent_token.get("tier", "outsider")}).encode())
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        # Drain the body BEFORE anything can return early. On a keep-alive connection an unread
        # body stays in the socket buffer and the server parses it as the next request line, so a
        # rejected request corrupts the one after it:
        #   Unsupported method ('{"stage":"probe"}POST')
        # That is a 501 on a request that was perfectly valid, which is a genuinely confusing
        # thing to debug. Read first, decide second.
        raw = self._body()

        if not self._authorised():
            self._send(401, b'{"error":"unauthorized"}')
            return

        # ---- telemetry sink -------------------------------------------------------------------
        # `http.in` means a request reached the container; `email.in` means an agentic email
        # notification reached the handler.
        if self.path == "/v1/edge-log":
            try:
                d = json.loads(raw or b"{}")
            except Exception:
                d = {"stage": "unparseable", "detail": raw[:200].decode("utf-8", "replace")}
            log(f"[edge] {d.get('stage','?')} | {d.get('detail','')} | session={d.get('session','?')}")
            self._send(204)
            return

        # ---- outbound attachment registration ---------------------------------------------------
        # The sidecar calls this after spooling a file it wants attached to the reply (see
        # _pending_attachments above for why the bytes do not travel through the model).
        if self.path == "/v1/attach":
            try:
                d = json.loads(raw or b"{}")
                path, mime = str(d["path"]), str(d.get("mime") or "application/octet-stream")
                name = str(d.get("name") or os.path.basename(path))
            except Exception as e:
                log(f"[edge] attach.bad | {type(e).__name__}: {e}")
                self._send(400, b'{"error":"path required"}')
                return
            # Only ever accept a path inside the outbox. The sidecar is ours, but this endpoint would
            # otherwise let anything that can reach the gateway name an arbitrary file and have the
            # edge upload it to a chat — /etc/shadow included. Resolve first so ../ cannot escape.
            real, root = os.path.realpath(path), os.path.realpath(OUTBOX)
            if not (real == root or real.startswith(root + os.sep)) or not os.path.isfile(real):
                log(f"[edge] attach.rejected | outside {OUTBOX} or missing")
                self._send(400, b'{"error":"path must be an existing file in the outbox"}')
                return
            with _attachments_lock:
                _pending_attachments.append({"path": real, "mime": mime, "name": name})
                n = len(_pending_attachments)
            log(f"[edge] attach.registered | {mime} | {os.path.getsize(real)}B | pending={n}")
            self._send(204)
            return

        # ---- the turn -------------------------------------------------------------------------
        if self.path == "/v1/chat/completions":
            _discard_stale_attachments()

            # Capture the agent's Graph token as it passes through, so the m365 sidecar can use the
            # SAME credential the edge minted for this turn rather than holding a blueprint secret
            # of its own. Keeps "the agent never holds a credential" true for the tool path too.
            tok = self.headers.get("x-agent-graph-token")
            if tok:
                _agent_token["token"] = tok
                # Caller tier travels with the token: the sidecar refuses data tools when the
                # turn was started by someone outside the organisation. Absent header means
                # unidentified, which is treated as outsider — fail closed.
                _agent_token["tier"] = self.headers.get("X-Caller-Tier") or "outsider"
                _agent_token["upn"] = self.headers.get("x-agent-upn", "") or _agent_token["upn"]
                log(f"[edge] agent.token.refreshed | upn={_agent_token['upn'] or '?'}")

            req = urllib.request.Request(
                f"{HERMES_URL}/v1/chat/completions", data=raw, method="POST"
            )
            req.add_header("Content-Type", "application/json")
            if HERMES_KEY:
                req.add_header("Authorization", f"Bearer {HERMES_KEY}")
            for h in PASSTHROUGH:
                v = self.headers.get(h)
                if v:
                    req.add_header(h, v)
            # WAIT OUT A COLD HERMES instead of 502-ing immediately.
            #
            # Foundry marks the session ready as soon as the edge binds, while Hermes may still be
            # booting (skills sync, Playwright warm-up).
            # Readiness cannot simply wait for Hermes: Hermes' boot exceeds the platform's readiness
            # timeout, so gating on it fails the session instead. This is the only layer that can
            # absorb the gap. It also covers Hermes restarting under supervision.
            #
            # Only connection-level failures retry — an HTTPError means Hermes answered, and a real
            # error must surface immediately rather than being retried into a timeout.
            deadline = time.monotonic() + HERMES_COLD_START_WAIT
            waited = False
            retried_401 = False   # see the cold-start 401 block below; at most one retry per turn
            while True:
                try:
                    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
                        payload = r.read()
                        # Log a truncated preview for diagnostics; this is not a transcript store.
                        try:
                            _p = json.loads(payload)
                            _txt = (_p.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        except Exception:
                            _txt = payload[:400].decode("utf-8", "replace")
                        log(f"[edge] hermes.reply | status={r.status} | chars={len(_txt or '')} | "
                            f"preview={(_txt or '')[:300]!r}")

                        # COLD-START 401, RETRIED — not passed on as an answer.
                        #
                        # Hermes returns HTTP 200 with the failure in the message body, so this is a
                        # "successful" response carrying
                        #   "HTTP 401: Principal does not have access to API/Operation."
                        # The Entra token may not be usable for the first few seconds after cold start,
                        # so retry this specific transient once while the cold-start budget remains.
                        #
                        # We already wait out a cold start at the CONNECTION level; this is the same
                        # race one layer up, and it deserves the same treatment. Retried at most once
                        # and only while the cold-start budget is unspent, so a genuine, permanent
                        # permissions failure still surfaces quickly rather than being masked.
                        if (not retried_401 and time.monotonic() < deadline
                                and "Principal does not have access" in (_txt or "")):
                            retried_401 = True
                            log("[edge] hermes.cold.401 | identity not ready yet — retrying once "
                                "instead of showing the owner an auth error")
                            time.sleep(3)
                            continue

                        # Hand the turn's attachments to the edge. A HEADER, not a body field, on
                        # purpose: the body is Hermes' own chat-completion JSON and the edge
                        # deserialises it into a typed model, so an unexpected key there risks a
                        # parse failure for a feature that is meant to be additive. A header is
                        # invisible to anything that does not look for it.
                        self._send(r.status, payload,
                                   extra_headers=_drain_attachments())
                    return
                except urllib.error.HTTPError as e:
                    detail = e.read()[:500].decode("utf-8", "replace")
                    log(f"[edge] hermes.error | {e.code} | {detail}")
                    self._send(e.code, json.dumps({"error": detail}).encode())
                    return
                except Exception as e:
                    if time.monotonic() < deadline:
                        if not waited:
                            waited = True
                            log(f"[edge] hermes.cold | {type(e).__name__} — waiting up to "
                                f"{HERMES_COLD_START_WAIT}s for Hermes to finish booting")
                        time.sleep(2)
                        continue
                    log(f"[edge] hermes.unreachable | {type(e).__name__}: {e}")
                    # Include the tail of Hermes' own log in the 502 body. In a Foundry hosted
                    # sandbox :logstream is the only window in, and sessions go idle before it can
                    # be attached — so the reply itself has to carry the diagnosis or the failure
                    # is simply undebuggable from outside.
                    tail = ""
                    try:
                        with open(HERMES_LOG_PATH, "rb") as fh:
                            fh.seek(0, 2)
                            fh.seek(max(0, fh.tell() - 4000))
                            tail = fh.read().decode("utf-8", "replace")[-4000:]
                    except Exception as read_err:
                        tail = f"(no hermes log at {HERMES_LOG_PATH}: {read_err})"
                    self._send(502, json.dumps({"error": str(e), "hermes_log_tail": tail}).encode())
                    return

        self._send(404, b'{"error":"not found"}')


if __name__ == "__main__":
    log(f"[gateway] up on :{PORT} -> {HERMES_URL} (auth={'on' if GATEWAY_KEY else 'OFF'})")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
