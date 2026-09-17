"""Minimal gateway for the Foundry-hosted stack.

WHY THIS EXISTS
---------------
The Foundry thin edge (HermesRouterAgentLogicService) talks to exactly two endpoints:

    POST {RouterUrl}/v1/chat/completions   the turn itself
    POST {RouterUrl}/v1/edge-log           our own out-of-band telemetry

In the full canonical architecture both are served by `sandbox-router`, which also spawns a
per-user ACA sandbox. That router hard-requires the ACA Sandboxes layer (it reads
ACA_SANDBOX_GROUP, HERMES_DISK_ID and friends with os.environ[...] at import time, so it will not
even start without them), and that preview is not enabled on this subscription yet.

Pointing RouterUrl straight at Hermes would make chat work, because Hermes serves an
OpenAI-compatible /v1/chat/completions and takes the same Bearer token. But /v1/edge-log would
404, and that endpoint is the ONLY instrument that has ever told us the truth about whether an
inbound request reaches the container at all. Foundry's own :logstream returns session_not_found
for us, so a hosted agent is otherwise completely dark. Every real conclusion we have about the
email path came from counting `http.in` and `email.in` lines here. Losing it would make the
Stack A / Stack B comparison unfalsifiable.

So this is a deliberate stand-in: same two endpoints, same auth, no sandbox layer. Swap it for
sandbox-router once ACA Sandboxes is enabled and nothing on the edge side needs to change.

Standard library only, on purpose. No pip install, no lockfile, nothing to audit.
"""

import hashlib
import json
import os
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# app.py is also loaded directly by contract tests, where Python does not automatically place the
# source file's directory on sys.path as it does for `python /opt/gateway/app.py`.
_GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)
from state_sync import StateSyncManager, load_config as load_state_sync_config
from state_sync import write_config as write_state_sync_config
from state_sync import unlisted_durable_candidates

HERMES_URL = os.environ["HERMES_URL"].rstrip("/")
HERMES_KEY = os.environ.get("HERMES_KEY", "")
GATEWAY_KEY = os.environ.get("GATEWAY_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
SIDECAR_HOST = os.environ.get("SIDECAR_HOST", "127.0.0.1")
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8010"))
HERMES_STATE = os.environ.get("HERMES_STATE", "/files/hermes")

# Shared directory the sidecar spools outbound attachments into. Must match AGENT_OUTBOX in the
# sidecar; both default to the same path so the two halves cannot drift apart silently.
OUTBOX = os.environ.get("AGENT_OUTBOX", "/tmp/agent-outbox")

# Hermes turns are long. This bounds a single upstream call, not the agent's own thinking budget.
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "900"))

# How long a turn will wait for a cold Hermes before giving up (see the retry loop in do_POST).
# 0 preserves the original fail-fast behaviour for the split ACA deployment, where Hermes is a
# separate always-on container and "connection refused" really does mean broken.
HERMES_COLD_START_WAIT = int(os.environ.get("HERMES_COLD_START_WAIT", "0"))

# Headers the edge sets that Hermes/the router care about. Forwarded verbatim.
PASSTHROUGH = (
    "x-hermes-session-key",
    # ⚠️ SESSION-ID MUST BE HERE OR THE AGENT HAS NO MEMORY.
    #
    # These are two different headers with two different jobs, per Hermes'
    # gateway/platforms/api_server.py: Session-KEY scopes long-term memory, Session-ID is what
    # continues the conversation ("Allow caller to continue an existing session by passing
    # X-Hermes-Session-Id. When provided, history is loaded from state.db instead of from the
    # request body.").
    #
    # On 2026-08-17 the edge was taught to send Session-Id and this list was not updated, so the
    # gateway silently dropped it one hop later and every Teams turn was still a fresh conversation.
    # The symptom was indistinguishable from the original bug — the agent fetched seven headlines and
    # then asked the owner what to summarise — and it cost a second round of diagnosis, including a
    # wrong theory that the session was pinned to an old agent version.
    #
    # This list is an ALLOW-LIST: anything not named here is stripped. Adding a header at the edge is
    # therefore only half a change. Grep both ends.
    "x-hermes-session-id",
    "x-hermes-correlation-id",
    "x-identity-assertion",
    "x-agent-graph-token",
    "x-agent-upn",
)


# Last agent Graph token seen on a turn, held in memory only. Served to the m365 sidecar via
# GET /v1/agent-token. Never written to disk, never logged.
_agent_token = {
    "token": "", "upn": "", "tier": "outsider", "routine_capability": "", "correlation": ""
}
_authority_lock = threading.Lock()

# The sidecar asks for the current turn's authority through /v1/agent-token, but that request does
# not yet carry an end-to-end turn ID. A ThreadingHTTPServer could otherwise run two users' Hermes
# turns concurrently and let one tool call observe the other's Graph token or owner capability.
# Serialize only chat turns within this agent container until the Hermes MCP transport can propagate
# a correlation ID. Health, telemetry, attachment registration and token reads remain concurrent,
# which is necessary for the active turn itself to complete.
_turn_lock = threading.Lock()


def _state_sync_log(message):
    # state_sync emits only operation names, counts, sizes and error classes. Route through the normal
    # redactor/telemetry path anyway so that one logging policy remains authoritative.
    log(message)


_state_sync = StateSyncManager(HERMES_STATE, logger=_state_sync_log)


def _clear_turn_authority():
    with _authority_lock:
        _agent_token.update(
            token="", upn="", tier="outsider", routine_capability="", correlation="")


def _dependency_socket(target_url=None, host=None, port=None):
    """Return dependency reachability without sending credentials or application content."""
    try:
        if target_url:
            parsed = urllib.parse.urlsplit(target_url)
            probe_host = parsed.hostname
            probe_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        else:
            probe_host, probe_port = host, port
    except ValueError:
        return False
    if not probe_host or not probe_port:
        return False
    try:
        with socket.create_connection((probe_host, probe_port), timeout=0.25):
            return True
    except OSError:
        return False


def _dependency_health():
    """Aggregate local runtime health without exposing endpoints, identities, or error details."""
    components = {
        "gateway": True,
        "hermes": _dependency_socket(target_url=HERMES_URL),
        "m365_sidecar": _dependency_socket(host=SIDECAR_HOST, port=SIDECAR_PORT),
    }
    return {"ok": all(components.values()), "components": components}


def _current_correlation():
    with _authority_lock:
        return _agent_token.get("correlation", "")

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


def _take_attachments():
    """Pop every attachment registered during this turn."""
    with _attachments_lock:
        items, _pending_attachments[:] = list(_pending_attachments), []
    if not items:
        return []
    log(f"[edge] attach.handoff | {len(items)} file(s) -> edge")
    return items


def _drain_attachment_headers():
    """Render turn attachments as a header for the legacy buffered response path."""
    items = _take_attachments()
    if not items:
        return {}
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


_LOG_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_LOG_GUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_LOG_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_LOG_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:sig|token|code|key|client_secret|assertion|access_token)=)[^&\s]+")
_LOG_TENANT_HOST = re.compile(
    r"(?i)\b[a-z0-9-]+(?:-my\.sharepoint\.com|\.onmicrosoft\.com)\b")
_CORRELATION = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _log_fingerprint(value):
    """Stable, non-reversible identifier for correlating redacted log events."""
    return hashlib.sha256(value.group(0).lower().encode()).hexdigest()[:12]


def _redact_log_message(message):
    """Remove user content, tenant names and credentials before stdout/App Insights."""
    text = str(message)
    text = _LOG_JWT.sub("<token>", text)
    text = _LOG_SECRET_QUERY.sub(r"\1<redacted>", text)
    text = _LOG_EMAIL.sub("<email>", text)
    text = _LOG_TENANT_HOST.sub("<tenant-host>", text)
    text = _LOG_GUID.sub(lambda match: f"<id:{_log_fingerprint(match)}>", text)
    return text


def _safe_correlation(value=""):
    candidate = str(value or "")
    return candidate if _CORRELATION.fullmatch(candidate) else os.urandom(8).hex()


def log(*parts):
    # Unbuffered so `az containerapp logs show --follow` is actually live.
    message = _redact_log_message(" ".join(str(p) for p in parts))
    correlation = _current_correlation()
    if correlation and "correlation=" not in message:
        message += f" | correlation={correlation}"
    print(message, flush=True)
    _ship(message)


# ---- ship our own log lines to Application Insights -------------------------------------------
# WHY THIS EXISTS: in a Foundry hosted agent, stdout is effectively unreadable for us. `:logstream`
# returns 404 for every version we tried, and the session `files` API returns 403
# `session_not_accessible` because a Teams-created session belongs to the CALLER's isolation key, not
# ours. What does reach Application Insights is only the .NET framework's own ILogger output — so we
# could see "Received message activity" and every outbound HTTP call, but not one line of our own
# diagnostics, and not Hermes' reply.
#
# The cost of that, 2026-08-17: hours on hfull's Teams turns. Every layer was verified healthy
# (message arrives, edge forwards, model call 200, replies delivered 201/202) and the owner still saw
# only "HTTP 401: Principal does not have access to API/Operation." — with no way to learn which call
# produced it. Exactly the same shape as the morning's copyright-filter hunt, where instrumenting the
# sidecar found the cause in one turn after hours of wrong theories.
#
# The platform injects APPLICATIONINSIGHTS_CONNECTION_STRING. Standard library only, fire-and-forget,
# and it must never break a turn: a failed ship is swallowed.
# APPLICATIONINSIGHTS_CONNECTION_STRING is RESERVED — the platform rejects it in a version env
# ("reserved for platform use ... per container-image-spec"), and it is also NOT actually present
# in our container: during the v11 turn there was zero egress to the ingestion endpoint, so this
# shipper never fired. Read our own non-reserved name first, then fall back to the platform one in
# case a future runtime does inject it.
_AI_CS = (os.environ.get("PLANF_AI_CONNECTION_STRING", "")
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
            with urllib.request.urlopen(req, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as error:
            error.close()
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

    # ---- Hermes SSE -> edge SSE ---------------------------------------------------------------
    #
    # This is deliberately NOT Agent 365 / Teams response streaming. It is an internal connection
    # from Hermes to our .NET edge. Hermes emits structured tool lifecycle events here; the edge
    # turns those into ordinary UpdateActivity calls against the bot-owned progress message. The
    # final Teams answer remains a separate SendActivity call, so a failed progress edit can never
    # discard completed work.
    #
    # Attachments are registered while the stream is already open, too late for HTTP response
    # headers. Therefore the streaming path sends one private `hermes.attachments` SSE event before
    # Hermes' terminal [DONE]. The buffered fallback keeps the older response-header handoff.
    def _relay_sse(self, upstream):
        self.send_response(upstream.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        tools_seen = 0
        data_bytes = 0
        attachments_sent = False

        def write_chunk(payload):
            if not payload:
                return
            self.wfile.write(b"%X\r\n%s\r\n" % (len(payload), payload))
            self.wfile.flush()

        def send_attachments():
            nonlocal attachments_sent
            if attachments_sent:
                return
            attachments_sent = True
            items = _take_attachments()
            if not items:
                return
            event = ("event: hermes.attachments\n"
                     "data: " + json.dumps(items, separators=(",", ":")) + "\n\n").encode()
            write_chunk(event)

        try:
            for line in upstream:
                if line.startswith(b"event: hermes.tool.progress"):
                    tools_seen += 1
                elif line.startswith(b"data: "):
                    data_bytes += len(line)

                # The edge stops reading when it sees [DONE], so the private attachment event must
                # precede that marker. Preserve every original Hermes byte otherwise.
                if line.strip() == b"data: [DONE]":
                    send_attachments()
                write_chunk(line)

            # Defensive fallback for an upstream that closes without [DONE].
            send_attachments()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            log(f"[edge] hermes.stream.done | events={tools_seen} data_bytes={data_bytes}")
        except Exception as e:                  # noqa: BLE001 - client or upstream disconnected
            log(f"[edge] hermes.stream.broken | type={type(e).__name__} | "
                f"after {data_bytes} data bytes")
            # Headers and some chunks are already on the wire, so an ordinary error JSON document
            # would corrupt the SSE framing. If the edge is still connected, finish with a valid
            # private error event and terminal marker; if it is the disconnected side, these writes
            # simply fail and the handler closes. Leave attachments for next-turn stale cleanup.
            try:
                detail = json.dumps({"message": f"Hermes stream failed: {type(e).__name__}"})
                write_chunk(("event: hermes.error\n" f"data: {detail}\n\n").encode())
                write_chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, b'{"ok":true}')
            return
        if self.path == "/healthz":
            health = _dependency_health()
            self._send(200 if health["ok"] else 503,
                       json.dumps(health, separators=(",", ":")).encode())
            return
        if self.path == "/v1/state-sync-config":
            if not self._authorised():
                self._send(401, b'{"error":"unauthorized"}')
                return
            with _authority_lock:
                tier = _agent_token.get("tier", "outsider")
            if tier != "owner":
                self._send(403, b'{"error":"verified owner turn required"}')
                return
            try:
                value = load_state_sync_config(pathlib.Path(HERMES_STATE))
                value = dict(value)
                value["available_unlisted_paths"] = unlisted_durable_candidates(
                    pathlib.Path(HERMES_STATE), value)
                self._send(200, json.dumps(value, separators=(",", ":")).encode())
            except Exception as exc:
                log(f"[state-sync] config read failed type={type(exc).__name__}")
                self._send(500, b'{"error":"state synchronization configuration is invalid"}')
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
            with _authority_lock:
                authority = dict(_agent_token)
            tok = authority.get("token", "")
            routine_capability = authority.get("routine_capability", "")
            if not tok and not routine_capability:
                self._send(404, b'{"error":"no agent turn authority seen yet"}')
                return
            self._send(200, json.dumps({"token": tok, "upn": authority.get("upn", ""),
                                        "tier": authority.get("tier", "outsider"),
                                        "routine_capability": routine_capability}).encode())
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            with _turn_lock:
                try:
                    return self._do_POST()
                finally:
                    # Complete the state write while this turn's short-lived Agent User token still
                    # exists. `_do_POST` has already delivered the HTTP/SSE response, so the small
                    # synchronization delay does not hold the user's answer. A second bounded pass
                    # catches quick background skill creation; longer reviews are picked up on the
                    # owner's next turn.
                    sync = getattr(self, "_state_sync_context", None)
                    # BaseHTTPRequestHandler can reuse one handler for multiple requests on a
                    # keep-alive connection. Remove the turn token from the handler itself before
                    # doing the bounded upload, so a later colleague turn cannot inherit it.
                    self._state_sync_context = None
                    if sync:
                        try:
                            _state_sync.finish_turn(sync)
                        except Exception as exc:  # synchronization must never discard a reply
                            log(f"[state-sync] finish failed type={type(exc).__name__}")
                    # A Graph token or routine capability is usable only while this turn is
                    # executing. Do not leave standing authority between messages.
                    _clear_turn_authority()
        return self._do_POST()

    def _do_POST(self):
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
        # This is the whole reason this service exists. `http.in` means a request reached the
        # container; `email.in` means an agentic email notification reached our handler. As of
        # 2026-08-07 the lifetime count of `email.in` across every Foundry-hosted build is zero.
        if self.path == "/v1/edge-log":
            try:
                d = json.loads(raw or b"{}")
            except Exception:
                d = {"stage": "unparseable", "detail": f"bytes={len(raw)}"}
            supplied_session_tag = str(d.get("session_tag") or "")
            session_tag = (supplied_session_tag
                           if re.fullmatch(r"[a-f0-9]{12}", supplied_session_tag)
                           else "none")
            correlation = str(d.get("correlation") or "")
            suffix = (f" | correlation={correlation}"
                      if _CORRELATION.fullmatch(correlation) else "")
            log(f"[edge] {d.get('stage','?')} | {d.get('detail','')} | "
                f"session_tag={session_tag}{suffix}")
            self._send(204)
            return

        # Owner-editable durable-state whitelist. This is an internal gateway endpoint rather than
        # a generic local file writer: validation and the hard denylist stay outside model control.
        if self.path == "/v1/state-sync-config":
            with _authority_lock:
                tier = _agent_token.get("tier", "outsider")
            if tier != "owner":
                self._send(403, b'{"error":"verified owner turn required"}')
                return
            try:
                value = write_state_sync_config(
                    pathlib.Path(HERMES_STATE), json.loads(raw or b"{}"))
                self._send(200, json.dumps(value, separators=(",", ":")).encode())
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                log(f"[state-sync] config rejected type={type(exc).__name__}")
                self._send(400, b'{"error":"invalid state synchronization configuration"}')
            except Exception as exc:
                log(f"[state-sync] config write failed type={type(exc).__name__}")
                self._send(500, b'{"error":"state synchronization configuration was not changed"}')
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
                log(f"[edge] attach.bad | type={type(e).__name__}")
                self._send(400, b'{"error":"path required"}')
                return
            # Only ever accept a path inside the outbox. The sidecar is ours, but this endpoint would
            # otherwise let anything that can reach the gateway name an arbitrary file and have the
            # edge upload it to a chat — /etc/shadow included. Resolve first so ../ cannot escape.
            real, root = os.path.realpath(path), os.path.realpath(OUTBOX)
            if not (real == root or real.startswith(root + os.sep)) or not os.path.isfile(real):
                log("[edge] attach.rejected | path outside outbox or missing")
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

            # A keep-alive handler may have served an earlier owner turn. Always start with no state
            # authority; only this request's verified owner headers may opt back in below.
            self._state_sync_context = None

            # Caller authority is per request. Refresh it even if this turn has no Graph token;
            # otherwise a failed mint on a colleague turn could inherit the previous owner's tier.
            # Clear the token too: using a stale credential from a different turn is worse than
            # reporting that Microsoft 365 is temporarily unavailable.
            # Capture this request's complete authority as one snapshot. In particular, never
            # inherit a UPN from the previous turn when a header is absent.
            tok = self.headers.get("x-agent-graph-token") or ""
            upn = self.headers.get("x-agent-upn", "") if tok else ""
            with _authority_lock:
                _agent_token.update(
                    token=tok,
                    upn=upn,
                    tier=self.headers.get("X-Caller-Tier") or "outsider",
                    routine_capability=self.headers.get("X-Routine-Capability") or "",
                    correlation=_safe_correlation(
                        self.headers.get("X-Hermes-Correlation-Id")),
                )
            if tok:
                log("[edge] agent.token.refreshed")
            else:
                log("[edge] agent.token.cleared | current turn supplied none")

            # Refresh before Hermes sees every verified-owner turn. The first turn hydrates the
            # complete archive; later turns perform cheap head checks and merge another session's
            # changes when needed. OneDrive itself is scoped to this Agent User, so fixed object
            # names expose no owner, tenant or agent ID. Never load this native memory into a
            # colleague session; colleagues use only relevant, selectively recalled owner
            # permissions.
            caller_tier = self.headers.get("X-Caller-Tier") or "outsider"
            if tok and caller_tier == "owner":
                self._state_sync_context = tok
                try:
                    _state_sync.prepare_turn(tok)
                except Exception as exc:
                    # The local Foundry session is still usable. Fail open for chat, fail closed for
                    # state replacement, and expose only the exception class in telemetry.
                    log(f"[state-sync] prepare failed type={type(exc).__name__}")

            # Hermes' stream is the source of structured tool progress and 30-second keepalives.
            # If an older or alternate Hermes ignores the flag, the response-content-type branch
            # below retains the working buffered path.
            body = raw
            want_stream = True
            try:
                parsed = json.loads(raw or b"{}")
                if isinstance(parsed, dict):
                    want_stream = bool(parsed.get("stream", True))
                    if want_stream and "stream" not in parsed:
                        parsed["stream"] = True
                        body = json.dumps(parsed).encode()
                else:
                    want_stream = False
            except Exception:                   # Hermes owns malformed-request validation
                want_stream = False

            req = urllib.request.Request(
                f"{HERMES_URL}/v1/chat/completions", data=body, method="POST"
            )
            if want_stream:
                req.add_header("Accept", "text/event-stream")
            req.add_header("Content-Type", "application/json")
            if HERMES_KEY:
                req.add_header("Authorization", f"Bearer {HERMES_KEY}")
            for h in PASSTHROUGH:
                v = self.headers.get(h)
                if v:
                    req.add_header(h, v)
            # WAIT OUT A COLD HERMES instead of 502-ing immediately.
            #
            # Single-container (Plan F) topology: Foundry marks the session ready as soon as the EDGE
            # binds its port, then routes a request straight away — while Hermes is still booting
            # (skills sync, Playwright warm-up). Every such request came back
            # "Connection refused" -> 502 -> the user saw "The gateway returned 502."
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
                        if (r.headers.get("Content-Type") or "").startswith("text/event-stream"):
                            self._relay_sse(r)
                            return

                        payload = r.read()
                        # Parse the reply only to classify cold-start failures. Never copy reply text
                        # into logs: it can contain user content and tool results.
                        try:
                            _p = json.loads(payload)
                            _txt = (_p.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        except Exception:
                            _txt = payload[:400].decode("utf-8", "replace")
                        log(f"[edge] hermes.reply | status={r.status} | chars={len(_txt or '')}")

                        # COLD-START 401, RETRIED — not passed on as an answer.
                        #
                        # Hermes returns HTTP 200 with the failure in the message body, so this is a
                        # "successful" response carrying
                        #   "HTTP 401: Principal does not have access to API/Operation."
                        # The sandbox's Entra token is not usable for the first few seconds after a
                        # cold start, so Hermes' first model call is rejected even though every layer
                        # is healthy. Demonstrated by the owner 2026-08-17: three identical Teams
                        # messages gave 401, 401, then a normal answer.
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
                                   extra_headers=_drain_attachment_headers())
                    return
                except urllib.error.HTTPError as e:
                    # Upstream error bodies may contain model output, request details or signed
                    # URLs. Keep them out of telemetry and return a stable public error instead.
                    code = e.code
                    try:
                        e.read(4096)
                    finally:
                        e.close()
                    log(f"[edge] hermes.error | status={code}")
                    self._send(code, json.dumps({
                        "error": f"Hermes returned HTTP {code}."
                    }).encode())
                    return
                except Exception as e:
                    if time.monotonic() < deadline:
                        if not waited:
                            waited = True
                            log(f"[edge] hermes.cold | {type(e).__name__} — waiting up to "
                                f"{HERMES_COLD_START_WAIT}s for Hermes to finish booting")
                        time.sleep(2)
                        continue
                    correlation = _current_correlation() or os.urandom(8).hex()
                    log(f"[edge] hermes.unreachable | type={type(e).__name__} | "
                        f"correlation={correlation}")
                    self._send(502, json.dumps({
                        "error": "Hermes is temporarily unavailable.",
                        "correlation": correlation,
                    }).encode())
                    return

        self._send(404, b'{"error":"not found"}')


if __name__ == "__main__":
    log(f"[gateway] up on :{PORT} -> {HERMES_URL} (auth={'on' if GATEWAY_KEY else 'OFF'})")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
