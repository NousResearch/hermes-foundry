#!/usr/bin/env python3
"""Resumable tenant installation and delegated Graph Bot Based configuration.

No tokens/credentials in checkpoints. No automatic approval, policy changes, or agent RBAC.
Microsoft Graph communicationConfiguration is beta; see TENANT_ADMIN_INSTALLATION.md.
"""
import argparse
import fcntl
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "deployment-output/setup-state.json"
GRAPH = "https://graph.microsoft.com"
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
SCOPE = "AgentCommunicationConfiguration.ReadWrite"
PHASES = {"prerequisites", "provisioning", "awaiting_approval", "configure_bot",
          "enable_runtime", "verify_instance", "complete"}


def command(*args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        # Do not echo CLI output which can contain environment secrets or authentication codes.
        raise RuntimeError(f"{args[0]} {args[1]} failed. Check your login/prerequisites and run ./setup.sh --continue.")
    return result.stdout


def guid(value):
    return str(uuid.UUID(value))


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".setup-", dir=STATE.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, STATE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load():
    if not STATE.exists():
        raise RuntimeError("No saved installation. Start with ./setup.sh (without --continue).")
    state = json.loads(STATE.read_text())
    if state.get("schema") != 1 or state.get("phase") not in PHASES:
        raise RuntimeError("Unsupported setup checkpoint. Do not delete it; review before continuing.")
    for key in ("tenant", "subscription", "run_id"):
        guid(state[key])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", state["environment"]):
        raise RuntimeError("Invalid saved Azure environment name.")
    return state


def token(tenant, resource):
    data = json.loads(command("az", "account", "get-access-token", "--tenant", tenant,
                              "--resource", resource, "-o", "json"))
    if data.get("tenant", tenant).lower() != tenant.lower():
        raise RuntimeError("Azure token tenant mismatch.")
    return data["accessToken"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def request(url, access_token, method="GET", body=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.port or not (
        parsed.hostname == "graph.microsoft.com" or
        re.fullmatch(r"[a-zA-Z0-9-]+\.services\.ai\.azure\.com", parsed.hostname or "")
    ):
        raise RuntimeError("Unexpected API destination; refusing to send credentials.")
    req = urllib.request.Request(url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as response:
            payload = response.read()
            return json.loads(payload) if payload.strip() else {}
    except urllib.error.HTTPError as error:
        raise ApiError(error.code, f"{method} failed: HTTP {error.code}; request ID "
                           f"{error.headers.get('request-id', 'unavailable')}. "
                           "Check permission consent and service status. No automatic write retry.") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("API transport failed; a write may have completed. Resume setup to read back state. "
                           "No automatic write retry.") from None


def graph_path(path, params=None):
    return GRAPH + "/v1.0/" + path + ("?" + urllib.parse.urlencode(params) if params else "")


def env_values(environment):
    wanted = {"ACCOUNT", "PROJECT", "AGENT", "BLUEPRINT_ID", "BOT_SERVICE_ID",
              "TENANT_ID", "SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"}
    result = {}
    for line in command("azd", "env", "get-values", "--environment", environment).splitlines():
        key, sep, value = line.partition("=")
        if sep and key in wanted:
            parts = shlex.split(value)
            if len(parts) != 1:
                raise RuntimeError("Invalid deployment output: " + key)
            result[key] = parts[0]
    return result


def deployment(state):
    values = env_values(state["environment"])
    if (values.get("TENANT_ID", values.get("AZURE_TENANT_ID", "")).lower() != state["tenant"] or
        values.get("SUBSCRIPTION_ID", values.get("AZURE_SUBSCRIPTION_ID", "")).lower() != state["subscription"]):
        raise RuntimeError("Saved installation and azd tenant/subscription do not match.")
    if state.get("existing_deployment"):
        # Explicit operator adoption only. Keep original azd/Bicep outputs intact and never
        # rerun infrastructure provisioning against a manually published legacy deployment.
        for key in ("ACCOUNT", "PROJECT", "AGENT"):
            if values.get(key) != state["target"].get(key):
                raise RuntimeError("Existing deployment's environment target changed. Refusing to continue.")
        values.update(state["target"])
    for key in ("ACCOUNT", "PROJECT", "AGENT"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", values.get(key, "")):
            raise RuntimeError("Missing or invalid deployment output: " + key)
    target = {k: values[k] for k in ("ACCOUNT", "PROJECT", "AGENT", "BLUEPRINT_ID", "BOT_SERVICE_ID")}
    target["BLUEPRINT_ID"] = guid(target["BLUEPRINT_ID"])
    if state.get("target") and state["target"] != target:
        raise RuntimeError("Deployment IDs changed since the checkpoint. Refusing to configure another blueprint.")
    base = f"https://{values['ACCOUNT']}.services.ai.azure.com/api/projects/{values['PROJECT']}"
    agent = request(f"{base}/agents/{values['AGENT']}?api-version=v1", token(state["tenant"], "https://ai.azure.com"))
    if guid((agent.get("blueprint") or {}).get("client_id", "")) != target["BLUEPRINT_ID"]:
        raise RuntimeError("Existing hosted agent is bound to a different blueprint. Setup has not completed; review the environment.")
    bot = json.loads(command("az", "resource", "show", "--ids", target["BOT_SERVICE_ID"], "-o", "json"))
    if (bot.get("type", "").lower() != "microsoft.botservice/botservices" or
        not bot.get("id", "").lower().startswith("/subscriptions/" + state["subscription"] + "/") or
        guid(bot.get("properties", {}).get("msaAppId", "")) != target["BLUEPRINT_ID"] or
        guid(bot.get("properties", {}).get("msaAppTenantId", "")) != state["tenant"]):
        raise RuntimeError("The Azure Bot does not match this tenant, subscription, and blueprint.")
    endpoint = urllib.parse.urlsplit(bot.get("properties", {}).get("endpoint", ""))
    expected = urllib.parse.urlsplit(f"{base}/agents/{values['AGENT']}/endpoint/protocols/activityProtocol")
    if (endpoint.scheme != expected.scheme or endpoint.netloc != expected.netloc or
        endpoint.path != expected.path or endpoint.fragment or
        urllib.parse.parse_qs(endpoint.query) != {"api-version": ["2025-05-15-preview"]}):
        raise RuntimeError("The Azure Bot endpoint does not target this hosted agent's Activity Protocol. "
                           "Review the deployment; no blueprint binding changed.")
    state["target"] = target
    save(state)
    return agent


def approved(agent):
    return (agent.get("agent_endpoint") or {}).get("publish_approval_status", "").lower() == "approved"


def pause(message):
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("Paused safely. Run ./setup.sh --continue to resume.") from None


def confirm(message):
    try:
        answer = input(message + " Type yes to confirm, or anything else to pause: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() != "yes":
        raise RuntimeError("Not confirmed. Run ./setup.sh --continue when ready.")


def prerequisites(state):
    print("\nBEFORE AZURE PROVISIONING (no license purchases or security-policy changes are automated)")
    print("Use the customer tenant's administrator account, not an unrelated personal tenant.")
    print("1. https://admin.cloud.microsoft/#/agents/overview : Global Administrator accepts Frontier terms.")
    print("2. https://admin.cloud.microsoft/#/licenses : confirm an available agent-specific Frontier/AI Teammates seat.")
    print("   Human Agent 365 governance licensing is not a replacement for that preview seat.")
    print("   If enrollment is unavailable, check Microsoft's current eligibility and Purchase services at")
    print("   https://admin.cloud.microsoft/#/catalog ; see TENANT_ADMIN_INSTALLATION.md before buying anything.")
    print("3. Confirm the pilot employee can use Teams and is in the intended Frontier/hiring audience.")
    print("4. https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2 : select your subscription.")
    print("   Access control (IAM) must allow infrastructure deployment AND role assignments.")
    print("   For this installer, subscription Owner is the simplest setup; scoped Contributor + RBAC admin")
    print("   requires the resource-group/role scopes described in ADVANCED_TENANT_DEPLOYMENT.md.")
    print("5. https://ai.azure.com/ : check hosted-agent regional availability and model quota.")
    print("   The template currently requests gpt-5-mini Global Standard, capacity 5 (5,000 TPM).")
    print("   This is a small pilot quota; see infra/main.bicep for model/version/capacity choices.")
    print("Azure charges apply. Setup builds the image in ACR (no local Docker required), creates the")
    print("runtime and private publication, and by default installs routine RBAC automation once per tenant deployment.")
    print("That automation grants eligible child Agent Identities project access; no per-agent manual CLI grants.")
    print("These portal prerequisites are operator confirmations, not automated entitlement or quota verification.")
    confirm("Have you completed these prerequisites and approved the described Azure deployment?")
    state["prerequisites_confirmed"] = True
    state["phase"] = "provisioning"
    save(state)


def agent_url(state):
    target = state["target"]
    return (f"https://{target['ACCOUNT']}.services.ai.azure.com/api/projects/"
            f"{target['PROJECT']}/agents/{target['AGENT']}")


def check_route(state, agent):
    endpoint = agent.get("agent_endpoint") or {}
    if (not {"activity", "responses"}.issubset(endpoint.get("protocols") or []) or
        "BotServiceTenant" not in {s.get("type") for s in endpoint.get("authorization_schemes") or []}):
        raise RuntimeError("Hosted endpoint is missing Activity/Responses or BotServiceTenant authorization. No route changed.")
    rules = (endpoint.get("version_selector") or {}).get("version_selection_rules") or []
    if len(rules) != 1 or rules[0].get("traffic_percentage") != 100:
        raise RuntimeError("Expected one serving version with 100% traffic. Review the Foundry route; setup did not change it.")
    version = str(rules[0].get("agent_version") or "")
    if not version.isdigit():
        raise RuntimeError("Serving version is missing or invalid.")
    result = request(f"{agent_url(state)}/versions/{version}?api-version=v1",
                     token(state["tenant"], "https://ai.azure.com"))
    if result.get("status") != "active":
        raise RuntimeError("Serving version is not active. Wait or inspect Foundry logs, then resume setup.")
    definition = result.get("definition") or {}
    protocols = {p.get("protocol") for p in definition.get("container_protocol_versions") or []}
    if (definition.get("kind") != "hosted" or
        (definition.get("environment_variables") or {}).get("AgentLogic") != "HermesRouter" or
        not {"activity_protocol", "responses"}.issubset(protocols)):
        raise RuntimeError("Serving version is not the expected Hermes runtime with Activity and Responses protocols.")
    return version


def enable_runtime(state):
    agent = deployment(state)  # Revalidate tenant, blueprint and bot before any write.
    if not approved(agent):
        state["phase"] = "awaiting_approval"
        save(state)
        raise RuntimeError("Blueprint approval is no longer effective. Review Requests before continuing.")
    version = check_route(state, agent)
    status = agent.get("state")
    if status not in {"enabled", "disabled"}:
        raise RuntimeError("Unknown hosted-agent endpoint state. No activation attempted.")
    if status == "disabled":
        print("\nThe Foundry endpoint is disabled. This is NOT an idle/scale-to-zero container.")
        print("Enabling it lets all instances of THIS blueprint receive requests; it does not keep compute always running.")
        confirm("Enable this installation's hosted endpoint now?")
        request(f"{agent_url(state)}:enable?api-version=v1",
                token(state["tenant"], "https://ai.azure.com"), "POST")
        # One write only. An interrupted run resumes with a fresh read before considering another write.
        for attempt in range(12):
            agent = deployment(state)
            if agent.get("state") == "enabled":
                break
            if attempt < 11:
                time.sleep(5)
        else:
            raise RuntimeError("Enable requested but not verified within one minute. Wait, then resume setup.")
    if not approved(agent) or check_route(state, agent) != version:
        raise RuntimeError("Approval or serving version changed during activation. Review before proceeding.")
    state["runtime_verified"] = True
    state["phase"] = "verify_instance"
    save(state)
    print("PASS: Foundry endpoint enabled; serving Hermes version and routing verified.")


def verify_instance_identity(state):
    upn = state.get("instance_upn")
    if not upn:
        try:
            upn = input("Enter the new instance's full email/UPN (not its owner's email): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Instance verification paused. Resume when the Microsoft-created instance is ready.") from None
    if not re.fullmatch(r"[^\s/@]+@[^\s/@]+\.[^\s/@]+", upn):
        raise RuntimeError("Enter the instance's complete email address, as shown in Microsoft 365.")
    access_token = token(state["tenant"], GRAPH)
    user = request(GRAPH + "/beta/users/" + urllib.parse.quote(upn, safe="") +
                   "/microsoft.graph.agentUser", access_token)
    parent = guid(user.get("identityParentId", ""))
    principal = request(GRAPH + "/beta/servicePrincipals/" + parent, access_token)
    if (user.get("accountEnabled") is not True or principal.get("accountEnabled") is not True or
        str(principal.get("agentIdentityBlueprintId", "")).lower() != state["target"]["BLUEPRINT_ID"]):
        raise RuntimeError("Instance is disabled or belongs to a different blueprint. Nothing was changed.")
    state["instance_upn"] = upn
    save(state)
    print("PASS: enabled Agent User and Agent Identity belong to this installation's blueprint.")


def verify_instance(state):
    agent = deployment(state)
    if not approved(agent) or agent.get("state") != "enabled":
        state["phase"] = "enable_runtime"
        save(state)
        raise RuntimeError("Approval/runtime readiness changed. Resume to review it; no silent reactivation.")
    check_route(state, agent)
    print("\nCREATE OR SELECT YOUR FIRST INSTANCE")
    print("Open https://admin.cloud.microsoft/#/agents/all > Hermes Agent on Foundry > Add/Create instance.")
    print("Choose its employee owner/manager, name, alias and domain, then confirm creation.")
    print("Alternatively: https://teams.microsoft.com/ > Apps > Agents for your team > Hermes Agent on Foundry")
    print("> Create instance, using an employee in the approved hiring scope.")
    print("Reuse an existing instance from THIS blueprint when resuming; do not create duplicates.")
    print("Microsoft performs Agent User/license/workload provisioning. Allow a few minutes for Teams visibility.")
    print("If unavailable: check hiring scope, Frontier seat availability and Requests approval in the same tenant.")
    print("Do not manually create Agent User objects or assign per-instance roles as a workaround.")
    verify_instance_identity(state)
    confirm("Is the instance visible in its owner's Teams account?")
    checks = [
        ("chat", 'Send "Hello, introduce yourself" in its Teams chat; verify a real text reply, not only a progress message.'),
        ("onedrive", 'Ask it to create a uniquely named installation-test text file without overwriting existing files, read it back, then delete only that test file.'),
        ("mail", 'Ask it to send a test email to its owner; confirm receipt from the agent mailbox.'),
    ]
    for key, instructions in checks:
        if (state.get("operator_checks") or {}).get(key):
            continue
        print(instructions)
        confirm("Did this test succeed?")
        state.setdefault("operator_checks", {})[key] = True
        save(state)
    print("If routine automation was installed: allow its reconciliation interval (normally five minutes),")
    print("ask the agent to schedule a test email a few minutes ahead in an explicit timezone, confirm receipt,")
    print("then ask it to delete the test routine. A listed routine alone is not a successful execution test.")
    try:
        routine_result = input("Type tested if execution succeeded, or omitted if you intentionally did not install routine automation: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        routine_result = ""
    if routine_result not in {"tested", "omitted"}:
        raise RuntimeError("Routine acceptance is pending. Resume after testing; do not mark a failed test as omitted.")
    state["routine_check"] = routine_result
    state["phase"] = "complete"
    save(state)
    print("Installation checks complete. Runtime configuration verified by API; Teams/OneDrive/email confirmed by the operator.")
    print("Routine outcome (operator reported): " + routine_result + ". This is not an automated execution test.")


def setup_client(state, admin_token, user):
    """Create/reuse a setup app; consent only for this administrator, not all employees."""
    def api(path, method="GET", body=None, params=None):
        return request(graph_path(path, params), admin_token, method, body)
    def create(path, body, marker):
        if state.get(marker):
            raise RuntimeError("Previous " + path + " creation has an unknown outcome. Review Entra; no duplicate created.")
        state[marker] = True
        save(state)
        try:
            return api(path, "POST", body)
        except ApiError as error:
            # Explicit validation/auth rejection cannot have created the object. A transport
            # failure, 408, 409, or server failure needs readback/review, not blind recreation.
            if error.status in (400, 401, 403, 404, 405, 422, 429):
                state.pop(marker, None)
                save(state)
            raise
    graph = api("servicePrincipals", params={"$filter": f"appId eq '{GRAPH_APP}'",
                                             "$select": "id,oauth2PermissionScopes"})["value"]
    if len(graph) != 1:
        raise RuntimeError("Could not resolve Microsoft Graph in this tenant.")
    scopes = [s for s in graph[0]["oauth2PermissionScopes"] if s.get("value") == SCOPE and s.get("isEnabled")]
    if len(scopes) != 1:
        raise RuntimeError("This tenant does not expose the required communication configuration permission.")
    name = "Hermes Setup " + state["run_id"][:12]
    apps = api("applications", params={"$filter": f"displayName eq '{name}'"})["value"]
    access = [{"resourceAppId": GRAPH_APP, "resourceAccess": [{"id": scopes[0]["id"], "type": "Scope"}]}]
    if not apps:
        app = create("applications", {"displayName": name, "signInAudience": "AzureADMyOrg",
            "isFallbackPublicClient": True, "publicClient": {"redirectUris": ["http://localhost"]},
            "requiredResourceAccess": access}, "client_creation_started")
    elif len(apps) == 1:
        app = apps[0]
    else:
        raise RuntimeError("Multiple setup apps found. Review Entra; none were modified.")
    if (app.get("signInAudience") != "AzureADMyOrg" or not app.get("isFallbackPublicClient") or
        app.get("requiredResourceAccess") != access or
        "http://localhost" not in app.get("publicClient", {}).get("redirectUris", []) or
        app.get("passwordCredentials") or app.get("keyCredentials")):
        raise RuntimeError("Existing setup app has unexpected permissions or credentials; refusing to reuse it.")
    if state.get("client_id") and state["client_id"] != app["appId"]:
        raise RuntimeError("Setup application identity changed.")
    state.update(client_id=guid(app["appId"]), client_object_id=guid(app["id"]))
    save(state)
    principals = api("servicePrincipals", params={"$filter": f"appId eq '{state['client_id']}'"})["value"]
    if not principals:
        principal = create("servicePrincipals", {"appId": state["client_id"]}, "principal_creation_started")
    elif len(principals) == 1:
        principal = principals[0]
    else:
        raise RuntimeError("Multiple setup service principals found.")
    if not principal.get("accountEnabled") or principal.get("appRoleAssignmentRequired"):
        raise RuntimeError("Setup app is disabled or requires explicit assignment. Review Entra; no policy changed.")
    state["client_principal_id"] = guid(principal["id"])
    save(state)
    grants = api("oauth2PermissionGrants", params={"$filter": f"clientId eq '{principal['id']}'"})["value"]
    matches = [g for g in grants if g.get("consentType") == "Principal" and g.get("principalId") == user
               and g.get("resourceId") == graph[0]["id"] and g.get("scope", "").split() == [SCOPE]]
    if not matches:
        if any(g.get("consentType") == "AllPrincipals" or g.get("principalId") == user for g in grants):
            raise RuntimeError("Unexpected existing setup consent. Review rather than broadening it.")
        consent_key = "consent_started_" + user
        grant = create("oauth2PermissionGrants", {"clientId": principal["id"],
            "consentType": "Principal", "principalId": user, "resourceId": graph[0]["id"], "scope": SCOPE}, consent_key)
        state["consent_grant_id"] = grant["id"]
        save(state)
    return state["client_id"]


def ensure_msal():
    if importlib.util.find_spec("msal"):
        return
    folder = ROOT / "deployment-output/setup-auth-venv"
    python = folder / "bin/python"
    print("Preparing the local Microsoft sign-in library (MSAL). No credentials will be saved.", flush=True)
    if not python.exists():
        command(sys.executable, "-m", "venv", str(folder))
    command(str(python), "-m", "pip", "install", "--disable-pip-version-check", "msal==1.36.0")
    # The persisted phase is already configure_bot; reentry never reprovisions Azure.
    sys.stdout.flush()
    # exec closes the non-inheritable lock descriptor; the resumed process reacquires it.
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), "--continue"])


def browser_token(tenant, client, user):
    import msal
    callback = {}
    flow = None

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/start":
                self.send_response(302)
                self.send_header("Location", flow["auth_uri"])
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            if (parsed.path != "/" or query.get("state") != [flow["state"]] or
                ("code" in query) == ("error" in query) or
                any(len(value) != 1 for value in query.values())):
                self.send_error(400, "Invalid sign-in callback")
                return
            callback.update({k: v[0] for k, v in query.items() if len(v) == 1})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Sign-in response received. Return to the setup terminal for the result.")

    app = msal.PublicClientApplication(client, authority=f"https://login.microsoftonline.com/{tenant}",
                                       exclude_scopes=["offline_access"])
    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 2
        flow = app.initiate_auth_code_flow([GRAPH + "/" + SCOPE],
            redirect_uri=f"http://localhost:{server.server_port}", prompt="select_account", response_mode="query")
        print(f"Open on THIS computer in your preferred browser: http://localhost:{server.server_port}/start", flush=True)
        print("Sign in with the same tenant administrator. Complete MFA. This link waits for 15 minutes.", flush=True)
        print("For WSL/remote shells, the browser must reach this computer's localhost callback.", flush=True)
        deadline = time.monotonic() + 900
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if not callback:
        raise RuntimeError("Sign-in timed out. Run ./setup.sh --continue for a new link.")
    try:
        result = app.acquire_token_by_auth_code_flow(flow, callback)
    except ValueError:
        # Do not print raw callback/flow contents (authorization code and PKCE verifier).
        raise RuntimeError("Microsoft rejected sign-in response validation. No bot configuration changed. "
                           "Run ./setup.sh --continue for a fresh sign-in.") from None
    if "access_token" not in result:
        # Only safe diagnostic identifiers; never log the token result or error_description.
        code = str(result.get("error", "unknown"))
        code = code if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code) else "unknown"
        codes = [str(c) for c in (result.get("error_codes") or []) if re.fullmatch(r"[0-9]{1,10}", str(c))]
        correlation = str(result.get("correlation_id", ""))
        correlation = correlation if re.fullmatch(r"[a-fA-F0-9-]{36}", correlation) else "unavailable"
        raise RuntimeError(f"Microsoft sign-in failed ({code}; codes: {','.join(codes) or 'unavailable'}; "
                           f"correlation ID: {correlation}). No bot configuration changed. "
                           "Run ./setup.sh --continue for a fresh browser sign-in. "
                           "Do not disable Security Defaults or MFA.")
    claims = result.get("id_token_claims", {})
    if claims.get("tid") != tenant or claims.get("oid") != user:
        raise RuntimeError("Signed in as a different tenant/administrator. No bot configuration changed.")
    return result["access_token"]


def bind_bot(blueprint, bot, access_token):
    url = f"{GRAPH}/beta/applications/{guid(blueprint)}/microsoft.graph.agentIdentityBlueprint/communicationConfiguration"
    before = request(url, access_token)
    allowed = {"@odata.context", "@odata.type", "id", "endpointConfiguration",
               "isOverridableAtAgentIdLevel", "teamworkConfiguration"}
    if set(before) - allowed:
        raise RuntimeError("Unknown communication configuration fields. Refusing a full replacement.")
    preserved = ("isOverridableAtAgentIdLevel", "teamworkConfiguration")
    body = {key: before[key] for key in preserved if key in before}
    body["endpointConfiguration"] = {"configurationType": "botBased", "botBased": {"botId": guid(bot)}, "apiBased": None}
    if before.get("endpointConfiguration") != body["endpointConfiguration"]:
        request(url, access_token, "PUT", body)
    after = request(url, access_token)
    endpoint = after.get("endpointConfiguration", {})
    if (endpoint.get("configurationType") != "botBased" or
        (endpoint.get("botBased") or {}).get("botId") != guid(bot) or endpoint.get("apiBased") is not None or
        any(before.get(key) != after.get(key) for key in preserved)):
        raise RuntimeError("Bot configuration readback mismatch. Review before retrying.")


def run(state):
    if state["phase"] == "complete":
        if state.get("runtime_verified") and state.get("operator_checks"):
            agent = deployment(state)
            if not approved(agent) or agent.get("state") != "enabled":
                raise RuntimeError("Previously completed installation is no longer approved/enabled. "
                                   "No automatic reactivation: review the administrator's lifecycle decision.")
            check_route(state, agent)
            print("Runtime still enabled. Prior operator delivery checks are saved, not repeated live tests.")
            return
        print("Older checkpoint verified Bot Based routing only. Runtime and first-instance checks are still required.")
        state["phase"] = "enable_runtime"
        save(state)
    if state["phase"] == "prerequisites":
        prerequisites(state)
    if state["phase"] == "provisioning":
        result = subprocess.run(["azd", "provision", "--environment", state["environment"]], cwd=ROOT)
        if result.returncode:
            raise RuntimeError("Provisioning stopped. Run ./setup.sh --continue to retry the resumable provisioning phase.")
        deployment(state)  # Catch the post-hook's intentional identity-mismatch early exit.
        state["phase"] = "awaiting_approval"
        save(state)
    if state["phase"] == "awaiting_approval":
        print("\nAPPROVE HERMES IN MICROSOFT 365", flush=True)
        print("Open: https://admin.cloud.microsoft/#/agents/all/requested", flush=True)
        print("Select Hermes Agent on Foundry. Review permissions/hiring scope and approve the request.", flush=True)
        print("It can take about 5 minutes to appear; in rare cases a few hours. These are observed delays, not an SLA.", flush=True)
        print("If shown separately, enable/activate the blueprint at https://admin.cloud.microsoft/#/agents/all", flush=True)
        print("You can stop here and run ./setup.sh --continue later; Azure provisioning will not repeat.", flush=True)
        pause("After accepting the request, press [Enter] to continue: ")
        if not approved(deployment(state)):
            raise RuntimeError("Microsoft has not reported approval yet. Wait and rerun ./setup.sh --continue; no bot changes made.")
        state["phase"] = "configure_bot"
        save(state)
    if state["phase"] == "configure_bot":
        if not approved(deployment(state)):
            state["phase"] = "awaiting_approval"
            save(state)
            raise RuntimeError("Blueprint approval is no longer effective. Review Requests and run ./setup.sh --continue.")
        ensure_msal()
        print("\nConfigure Bot Based routing using Microsoft Graph (beta).", flush=True)
        print("This creates/reuses a tenant-owned setup app with one delegated permission:", SCOPE, flush=True)
        print("Consent is for the signed-in administrator only. No application permissions, secrets, or agent roles are added.", flush=True)
        pause("Press [Enter] to authorize this setup step, or Ctrl+C to pause: ")
        admin_token = token(state["tenant"], GRAPH)
        user = guid(request(graph_path("me", {"$select": "id"}), admin_token)["id"])
        client = setup_client(state, admin_token, user)
        delegated = browser_token(state["tenant"], client, user)
        blueprint = state["target"]["BLUEPRINT_ID"]
        bind_bot(blueprint, blueprint, delegated)
        state["phase"] = "enable_runtime"
        save(state)
        print("\nBot Based configuration verified. No manual Bot ID entry is needed.", flush=True)
    if state["phase"] == "enable_runtime":
        enable_runtime(state)
    if state["phase"] == "verify_instance":
        verify_instance(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--continue", dest="resume", action="store_true")
    parser.add_argument("--existing", action="store_true")
    parser.add_argument("--field", choices=("tenant", "subscription", "environment"))
    parser.add_argument("--tenant")
    parser.add_argument("--subscription")
    parser.add_argument("--environment")
    args = parser.parse_args()
    if args.field:
        print(load()[args.field])
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # One installer per checkout; keep the lock across browser sign-in and approval waits.
    lock = open(STATE.parent / ".setup.lock", "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("Another setup process is running. Stop it before using --continue.") from None
    try:
        start_or_resume(args)
    finally:
        lock.close()


def start_or_resume(args):
    if args.resume:
        state = load()
    else:
        if STATE.exists():
            raise RuntimeError("Saved installation exists. Run ./setup.sh --continue.")
        if not args.environment or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.environment):
            raise RuntimeError("A valid azd environment name is required.")
        state = {"schema": 1, "tenant": guid(args.tenant), "subscription": guid(args.subscription),
                 "environment": args.environment, "run_id": str(uuid.uuid4()), "phase": "prerequisites"}
        if getattr(args, "existing", False):
            adopt_existing(state)
        save(state)
    run(state)


def adopt_existing(state):
    values = env_values(state["environment"])
    if (values.get("TENANT_ID", values.get("AZURE_TENANT_ID", "")).lower() != state["tenant"] or
        values.get("SUBSCRIPTION_ID", values.get("AZURE_SUBSCRIPTION_ID", "")).lower() != state["subscription"]):
        raise RuntimeError("Existing environment does not match the selected tenant/subscription.")
    target = {key: values.get(key, "") for key in ("ACCOUNT", "PROJECT", "AGENT")}
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", v) for v in target.values()):
        raise RuntimeError("Existing environment needs valid ACCOUNT, PROJECT and AGENT outputs.")
    base = (f"https://{target['ACCOUNT']}.services.ai.azure.com/api/projects/"
            f"{target['PROJECT']}/agents/{target['AGENT']}")
    agent = request(base + "?api-version=v1", token(state["tenant"], "https://ai.azure.com"))
    blueprint = guid((agent.get("blueprint") or {}).get("client_id", ""))
    bots = json.loads(command("az", "resource", "list", "--subscription", state["subscription"],
                              "--resource-type", "Microsoft.BotService/botServices", "-o", "json"))
    matches = []
    for entry in bots:
        bot = json.loads(command("az", "resource", "show", "--ids", entry["id"], "-o", "json"))
        props = bot.get("properties") or {}
        if (str(props.get("msaAppId", "")).lower() == blueprint and
            str(props.get("msaAppTenantId", "")).lower() == state["tenant"] and
            props.get("endpoint") == base + "/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"):
            matches.append(bot["id"])
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one matching Azure Bot for the live blueprint/endpoint; no changes made.")
    target.update(BLUEPRINT_ID=blueprint, BOT_SERVICE_ID=matches[0])
    print("\nEXISTING DEPLOYMENT — no infrastructure provisioning, image build, publication or role assignment")
    print("Hosted agent:", base)
    print("Live blueprint:", blueprint)
    print("Matching Azure Bot:", matches[0])
    if values.get("BLUEPRINT_ID", "").lower() != blueprint or values.get("BOT_SERVICE_ID") != matches[0]:
        print("WARNING: old azd blueprint/bot outputs differ. They will NOT be overwritten or deployed.")
    confirm("Use this live target for approval, bot routing, activation and acceptance checks?")
    state.update(existing_deployment=True, target=target, phase="awaiting_approval")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, KeyboardInterrupt) as error:
        print("Setup stopped: " + (str(error) if not isinstance(error, KeyboardInterrupt) else "interrupted") +
              "\nCheckpoint retained. Run ./setup.sh --continue to resume.", file=sys.stderr)
        sys.exit(1)
