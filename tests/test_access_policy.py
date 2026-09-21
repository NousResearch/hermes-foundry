import copy
import importlib.util
import io
import json
import pathlib
import sys
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src/gateway"))
from access_policy import AccessPolicy, AccessError, POLICY_PATH, POLICY_NAME, encoded


class Store:
    def __init__(self):
        self.policy = None
        self.revision = 0
        self.calls = []
        self.conflict = False
        self.resources = {
            "users/alex@example.com": {"id": "alex", "userType": "Member", "displayName": "Alex", "userPrincipalName": "alex@example.com"},
            "me/drive/items/folder": {"id": "folder", "name": "Invoices", "folder": {}},
            "me/drive/items/invoice": {"id": "invoice", "name": "Invoice-1.txt", "parentReference": {"id": "folder"}, "file": {}},
            "me/drive/items/private": {"id": "private", "name": "Private.txt", "parentReference": {"id": "other"}, "file": {}},
            "me/drive/items/policy": {"id": "policy", "name": POLICY_NAME, "file": {}},
            "me/drive/root/children": {"value": [], "@odata.nextLink": "secret-next-link"},
            "me/messages/mail": {"id": "mail", "subject": "Invoice 1", "from": {"emailAddress": {"address": "alex@example.com"}}, "body": {"content": "private mail"}},
        }

    def request(self, method, path, body=None, headers=None, binary=False):
        self.calls.append((method, path, copy.deepcopy(body), headers))
        if path.startswith(POLICY_PATH.removesuffix(":/content")):
            if method == "GET":
                if self.policy is None: return 404, {}, b""
                if "?$select=" in path: return 200, {}, encoded({"id": "policy", "eTag": str(self.revision)})
                return 200, {}, self.policy
            if self.conflict: return 412, {}, b""
            headers = headers or {}
            if self.policy is not None and headers.get("If-Match") != str(self.revision): return 412, {}, b""
            self.policy = body
            self.revision += 1
            return 200, {}, b"{}"
        route = urllib.parse.unquote(path.split("?", 1)[0])
        if method != "GET": return 200, {}, b'{"id":"done"}'
        if route.endswith("/content"): return 200, {}, b"file content"
        item = self.resources.get(route)
        return (200, {}, encoded(item)) if item is not None else (404, {}, b"")


class AccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.now = 1000
        self.context = dict(tenant="tenant", agent="agent", caller="owner", tier="owner", conversation="teams-one")
        self.service = AccessPolicy(self.store, "a" * 48, self.context, lambda: self.now)

    def colleague(self, caller="alex", **context):
        return AccessPolicy(self.store, "a" * 48, {**self.context, "tier": "colleague", "caller": caller, **context}, lambda: self.now)

    def propose(self, **changes):
        return self.service.propose({"recipient": "alex@example.com", "resource": "onedrive",
            "scope": {"kind": "folder", "id": "folder"}, "actions": ["read"], **changes})

    def test_confirmation_uses_separate_paragraphs_for_teams(self):
        text = self.propose(scope={"kind": "pattern", "pattern": "ACME*"}, actions=["read", "edit"])
        self.assertTrue(text.startswith("**Allow access through Hermes?**\n\n"))
        self.assertIn("would be able to:\n\n- Read and edit", text)
        self.assertIn("names start with “ACME”", text)
        self.assertIn("Direct Microsoft access and any other permissions stay unchanged", text)
        self.assertIn("Reply exactly **Yes** or **No**", text)

    def test_item_confirmation_does_not_claim_future_matches(self):
        text = self.propose(scope={"kind": "item", "id": "invoice"})
        self.assertNotIn("new matching items", text)

    def grant(self, **changes):
        self.propose(**changes)
        self.service.confirm("yes")

    def test_proposal_does_not_grant_then_exact_owner_yes_persists(self):
        message = self.propose()
        self.assertIn("Reply exactly **Yes** or **No**", message)
        self.assertIn("new matching items", message)
        self.assertEqual([], self.colleague().inspect())
        self.assertEqual("Permission saved.", self.service.confirm("Yes"))
        self.assertEqual(1, len(self.colleague().inspect()))
        self.assertIsNone(self.service.confirm("yes"))
        self.assertEqual(1, len(self.service.inspect()))

    def test_no_cancels_without_grant(self):
        self.propose()
        self.assertIn("cancelled", self.service.confirm("no"))
        self.assertEqual([], self.service.inspect())

    def test_public_research_requires_confirmed_unexpired_person_specific_grant(self):
        self.propose(public_research=True, expires="1970-01-01T00:30:00Z")
        with self.assertRaises(AccessError): self.colleague().authorize_research("web_search")
        self.service.confirm("yes")
        self.assertEqual("Public research authorized", self.colleague().authorize_research("web_search"))
        self.assertEqual("Public research authorized", self.colleague().authorize_research("fetch_url"))
        for tool in ("run_code", "browse_url", "screenshot_url"):
            with self.assertRaises(AccessError): self.colleague().authorize_research(tool)
        with self.assertRaises(AccessError): self.colleague(caller="other").authorize_research("web_search")
        with self.assertRaises(AccessError): self.colleague(tier="external").authorize_research("web_search")
        self.now = 1900
        with self.assertRaises(AccessError): self.colleague().authorize_research("web_search")

    def test_existing_read_edit_does_not_silently_allow_research_or_create(self):
        self.grant(scope={"kind": "pattern", "pattern": "ACME*"}, actions=["read", "edit"])
        with self.assertRaises(AccessError): self.colleague().authorize_research("web_search")
        self.store.resources["me/drive/root"] = {"id": "root", "folder": {}}
        with self.assertRaises(AccessError):
            self.colleague().execute("PUT", "me/drive/root:/ACME2.docx:/content", b"doc")

    def test_collaboration_create_respects_filename_scope_and_research_is_revocable(self):
        self.grant(scope={"kind": "pattern", "pattern": "*ACME*"}, actions=["read", "create", "edit"], public_research=True)
        self.store.resources["me/drive/root"] = {"id": "root", "folder": {}}
        result = self.colleague().execute("PUT", "me/drive/root:/ACME2.docx:/content", b"doc")
        self.assertIn("done", result)
        for name in ("AMCE2.docx", "Private.docx"):
            with self.assertRaises(AccessError):
                self.colleague().execute("PUT", "me/drive/root:/" + name + ":/content", b"doc")
        grant = self.service.inspect()[0]
        self.service.propose({"operation": "revoke", "grant_id": grant["id"]})
        self.service.confirm("yes")
        with self.assertRaises(AccessError): self.colleague().authorize_research("web_search")

    def test_research_flag_is_explicit_in_confirmation_and_strictly_boolean(self):
        with self.assertRaises(AccessError): self.propose(public_research="true")
        text = self.propose(public_research=True)
        self.assertIn("Searches and page addresses go to external services", text)
        self.assertIn("research is not limited to this file selection", text)

    def test_collaboration_preset_expands_before_confirmation_not_before_approval(self):
        text = self.propose(preset="collaboration", actions=[], scope={"kind": "pattern", "pattern": "*ACME*"})
        self.assertIn("Read, create and edit", text)
        self.assertIn("Research the public web", text)
        self.assertIn("does not add permission for email access, calendar access", text)
        self.assertIn("any other permissions stay unchanged", text)
        self.assertEqual([], self.colleague().inspect())
        self.service.confirm("yes")
        grant = self.colleague().inspect()[0]
        self.assertEqual(["create", "edit", "read"], grant["actions"])
        self.assertTrue(grant["public_research"])
        self.assertEqual([], grant["destinations"])

    def test_collaboration_preset_cannot_add_outbound_or_override_custom_restrictions(self):
        for fields in ({"actions": ["share"]}, {"actions": [], "destinations": ["alex@example.com"]},
                       {"actions": [], "resource": "email"}, {"actions": [], "scope": "bad"}):
            with self.assertRaises(AccessError): self.propose(preset="collaboration", **fields)
        self.propose(preset="custom", actions=["read"], public_research=False)
        self.service.confirm("yes")
        grant = self.colleague().inspect()[0]
        self.assertEqual(["read"], grant["actions"])
        self.assertFalse(grant["public_research"])

    def test_collaboration_on_single_document_does_not_grant_creation_elsewhere(self):
        self.propose(preset="collaboration", actions=[], scope={"kind": "item", "id": "invoice"})
        self.service.confirm("yes")
        self.assertEqual(["edit", "read"], self.colleague().inspect()[0]["actions"])

    def test_replacement_explains_current_and_proposed_access_without_internal_json(self):
        self.grant(scope={"kind": "pattern", "pattern": "ACME*"}, actions=["read", "edit"])
        old = self.service.inspect()[0]
        text = self.propose(operation="replace", grant_id=old["id"], preset="collaboration",
                            actions=[], scope={"kind": "pattern", "pattern": "*ACME*"})
        self.assertTrue(text.startswith("**Change permission?**"))
        self.assertIn("Alex can currently ask me to read and edit", text)
        self.assertIn("names start with “ACME”", text)
        self.assertIn("names contain “ACME”", text)
        self.assertIn("would be able to:\n\n- Read, create and edit", text)
        self.assertNotIn(old["id"], text)
        self.assertNotIn('"principal"', text)
        self.assertNotIn("Approval:", text)
        self.assertLess(text.index("can currently"), text.index("would be able"))

    def test_scope_rendering_distinguishes_exact_suffix_and_complex_patterns(self):
        for pattern, phrase in [("ACME", "names are exactly “ACME”"),
                                ("*ACME", "names end with “ACME”"),
                                ("ACME?*.docx", "names match the pattern “ACME?\\*.\\docx”")]:
            text = self.propose(scope={"kind": "pattern", "pattern": pattern})
            if "?" in pattern:
                self.assertIn("match the pattern", text)
                self.assertIn("ACME?\\*", text)
            else: self.assertIn(phrase, text)

    def test_untrusted_labels_cannot_add_lines_or_markdown_to_confirmation(self):
        self.store.resources["users/alex@example.com"]["displayName"] = "Alex\n**Approved** <b>"
        text = self.propose()
        self.assertNotIn("\n**Approved**", text)
        self.assertIn("\\*\\*Approved\\*\\*", text)
        self.assertIn("&lt;b&gt;", text)

    def test_remote_shortcuts_are_not_exposed_even_with_all_files_grant(self):
        self.grant(scope={"kind": "all"})
        shortcut = {"id": "shortcut", "name": "Public name", "remoteItem": {"id": "policy"}}
        self.store.resources["me/drive/root/children"] = {"value": [shortcut]}
        self.store.resources["me/drive/items/shortcut"] = shortcut
        result = json.loads(self.colleague().execute("GET", "me/drive/root/children"))
        self.assertEqual([], result["value"])
        with self.assertRaises(AccessError):
            self.colleague().execute("GET", "me/drive/items/shortcut")

    def test_revision_text_cancels_previous_proposal_and_goes_to_agent(self):
        self.propose()
        self.assertIsNone(self.service.confirm("Yes, but only for today"))
        self.assertIsNone(self.service.confirm("yes"))
        self.assertEqual([], self.service.inspect())

    def test_other_conversation_cannot_approve(self):
        self.propose()
        other = AccessPolicy(self.store, "a" * 48, {**self.context, "conversation": "email-other"}, lambda: self.now)
        self.assertIsNone(other.confirm("yes"))
        self.assertEqual([], self.service.inspect())

    def test_colleague_and_routine_cannot_confirm_or_propose(self):
        self.propose()
        for service in [self.colleague(), AccessPolicy(self.store, "a" * 48,
                {**self.context, "conversation": "", "caller": ""}, lambda: self.now)]:
            with self.assertRaises(AccessError): service.confirm("yes")
            with self.assertRaises(AccessError): service.propose({})

    def test_expired_proposal_cannot_approve(self):
        self.propose(); self.now += 901
        self.assertIn("expired", self.service.confirm("yes"))
        self.assertEqual([], self.service.inspect())

    def test_integrity_and_agent_binding_are_verified(self):
        self.grant()
        envelope = json.loads(self.store.policy)
        envelope["payload"]["grants"][0]["actions"].append("delete")
        self.store.policy = encoded(envelope)
        with self.assertRaises(AccessError): self.service.inspect()
        other = AccessPolicy(self.store, "a" * 48, {**self.context, "agent": "other"})
        with self.assertRaises(AccessError): other.inspect()

    def test_etag_conflict_never_claims_approval(self):
        self.propose(); self.store.conflict = True
        with self.assertRaises(AccessError): self.service.confirm("yes")
        self.assertEqual([], self.service.inspect())

    def test_grants_are_filtered_by_actual_caller(self):
        self.grant()
        self.assertEqual([], self.colleague("somebody-else").inspect())
        with self.assertRaises(AccessError): self.colleague("somebody-else").execute("GET", "me/drive/items/invoice/content")

    def test_read_allowed_but_edit_share_delete_denied(self):
        self.grant()
        self.assertEqual("file content", self.colleague().execute("GET", "me/drive/items/invoice/content"))
        for method, route, body in [("DELETE", "invoice", None), ("PATCH", "invoice", {"name": "new"}),
                ("POST", "invoice/invite", {"recipients": [{"email": "alex@example.com"}], "roles": ["read"]})]:
            with self.assertRaises(AccessError): self.colleague().execute(method, "me/drive/items/" + route, body)

    def test_search_filters_before_returning_data_or_download_urls(self):
        self.grant()
        self.store.resources["me/drive/root/children"]["value"] = [
            {**self.store.resources["me/drive/items/invoice"], "@microsoft.graph.downloadUrl": "secret"},
            self.store.resources["me/drive/items/private"], self.store.resources["me/drive/items/policy"]]
        response = self.colleague().execute("GET", "me/drive/root/children?$select=id&$top=10")
        value = json.loads(response)
        self.assertEqual(["invoice"], [x["id"] for x in value["value"]])
        self.assertNotIn("secret", response)
        self.assertNotIn("Private.txt", response)
        self.assertNotIn("@odata.nextLink", response)

    def test_direct_private_item_and_reserved_state_denied(self):
        self.grant(scope={"kind": "all"})
        with self.assertRaises(AccessError): self.colleague().execute("GET", "me/drive/items/policy/content")
        self.assertFalse(self.service.guard_owner_path("me/drive/items/policy/content"))
        self.assertFalse(self.service.guard_owner_path(POLICY_PATH))
        self.assertTrue(self.service.guard_owner_path("me/drive/items/invoice/content"))

    def test_specific_folder_blocks_other_folders(self):
        self.grant()
        with self.assertRaises(AccessError): self.colleague().execute("GET", "me/drive/items/private/content")

    def test_pattern_matches_current_metadata(self):
        self.grant(scope={"kind": "pattern", "pattern": "Invoice*.txt"})
        self.assertEqual("file content", self.colleague().execute("GET", "me/drive/items/invoice/content"))
        self.store.resources["me/drive/items/invoice"]["name"] = "Payroll.txt"
        with self.assertRaises(AccessError): self.colleague().execute("GET", "me/drive/items/invoice/content")

    def test_recursive_folder_checks_ancestors(self):
        self.grant(scope={"kind": "folder", "id": "folder", "recursive": True})
        self.store.resources["me/drive/items/invoice"]["parentReference"] = {"id": "subfolder"}
        self.store.resources["me/drive/items/subfolder"] = {"id": "subfolder", "name": "2026", "parentReference": {"id": "folder"}}
        self.assertEqual("file content", self.colleague().execute("GET", "me/drive/items/invoice/content"))

    def test_sharing_requires_explicit_recipient(self):
        self.grant(actions=["read", "share"], destinations=["alex@example.com"])
        body = {"recipients": [{"email": "alex@example.com"}], "roles": ["read"]}
        self.assertIn("done", self.colleague().execute("POST", "me/drive/items/invoice/invite", body))
        body["recipients"] = [{"email": "other@example.com"}]
        with self.assertRaises(AccessError): self.colleague().execute("POST", "me/drive/items/invoice/invite", body)

    def test_revoke_applies_only_after_confirmation(self):
        self.grant()
        grant_id = self.service.inspect()[0]["id"]
        self.service.propose({"operation": "revoke", "grant_id": grant_id})
        self.assertEqual(1, len(self.colleague().inspect()))
        self.service.confirm("yes")
        self.assertEqual([], self.colleague().inspect())

    def test_replacement_narrows_existing_grant_without_leaving_old_write_access(self):
        self.grant(actions=["read", "edit"])
        grant = self.service.inspect()[0]
        self.propose(operation="replace", grant_id=grant["id"], actions=["read"])
        self.assertIn("edit", self.service.inspect()[0]["actions"])
        self.service.confirm("yes")
        self.assertEqual(["read"], self.service.inspect()[0]["actions"])
        with self.assertRaises(AccessError): self.colleague().execute("PUT", "me/drive/items/invoice/content", "data", raw=True)

    def test_stale_replacement_does_not_overwrite_newer_grant(self):
        self.grant(actions=["read", "edit"])
        grant = self.service.inspect()[0]
        self.propose(operation="replace", grant_id=grant["id"], actions=["read", "delete"])
        other = AccessPolicy(self.store, "a" * 48, {**self.context, "conversation": "second"}, lambda: self.now)
        other.propose({"operation": "replace", "grant_id": grant["id"], "recipient": "alex@example.com",
                       "resource": "onedrive", "scope": {"kind": "all"}, "actions": ["read"]})
        other.confirm("yes")
        with self.assertRaises(AccessError): self.service.confirm("yes")
        self.assertEqual(["read"], self.service.inspect()[0]["actions"])

    def test_grant_expiry_enforced_at_tool_time(self):
        self.grant(expires="1970-01-01T00:30:00Z")
        self.now = 1801
        with self.assertRaises(AccessError): self.colleague().execute("GET", "me/drive/items/invoice/content")

    def test_unsupported_routes_and_expansions_fail_closed(self):
        self.grant(scope={"kind": "all"}, actions=["read", "edit", "delete"])
        for path in ["$batch", "users/alex/messages", "drives/other/items/private/content",
                     "me/drive/items/invoice?$expand=permissions", "https://evil.example.com/me"]:
            with self.subTest(path=path), self.assertRaises(AccessError): self.colleague().execute("GET", path)

    def test_email_subject_scope_and_reply_destination(self):
        self.grant(resource="email", scope={"kind": "pattern", "pattern": "Invoice*"},
                   actions=["read", "reply"], destinations=["alex@example.com"])
        self.assertIn("private mail", self.colleague().execute("GET", "me/messages/mail"))
        self.assertIn("done", self.colleague().execute("POST", "me/messages/mail/reply", {"comment": "thanks"}))
        self.store.resources["me/messages/mail"]["from"]["emailAddress"]["address"] = "other@example.com"
        with self.assertRaises(AccessError): self.colleague().execute("POST", "me/messages/mail/reply", {"comment": "thanks"})

    def test_guests_and_unsupported_resources_cannot_be_proposed(self):
        self.store.resources["users/alex@example.com"]["userType"] = "Guest"
        with self.assertRaises(AccessError): self.propose()
        with self.assertRaises(AccessError): self.propose(resource="skills")

    def test_binary_reads_and_writes_are_authorized(self):
        self.grant(actions=["read", "edit"])
        self.assertEqual({"base64": "ZmlsZSBjb250ZW50"},
            self.colleague().execute("GET", "me/drive/items/invoice/content", binary=True))
        self.colleague().execute("PUT", "me/drive/items/invoice/content", "eHl6", raw=True, base64_body=True)
        self.assertEqual(b"xyz", self.store.calls[-1][2])
        with self.assertRaises(AccessError):
            self.colleague().execute("PUT", "me/drive/items/private/content", "eHl6", raw=True, base64_body=True)

    def test_create_only_grant_cannot_overwrite_existing_file(self):
        self.grant(actions=["create"])
        self.store.resources["me/drive/root:/Invoices"] = self.store.resources["me/drive/items/folder"]
        self.colleague().execute("PUT", "me/drive/root:/Invoices/New.txt:/content", "new", raw=True)
        self.assertIn("conflictBehavior=fail", self.store.calls[-1][1])
        self.assertEqual("*", self.store.calls[-1][3]["If-None-Match"])
        self.store.resources["me/drive/root:/Invoices/New.txt"] = self.store.resources["me/drive/items/invoice"]
        with self.assertRaises(AccessError):
            self.colleague().execute("PUT", "me/drive/root:/Invoices/New.txt:/content", "replace", raw=True)

    def test_calendar_invitation_requires_attendee_grant(self):
        self.store.resources["me/calendar"] = {"id": "calendar"}
        self.store.resources["me/calendars/calendar"] = {"id": "calendar", "name": "Work"}
        self.grant(resource="calendar", scope={"kind": "calendar", "id": "calendar"}, actions=["create"])
        event = {"subject": "Meeting", "start": {"dateTime": "2026-10-01T10:00:00Z"},
                 "attendees": [{"emailAddress": {"address": "alex@example.com"}}]}
        with self.assertRaises(AccessError): self.colleague().execute("POST", "me/events", event)
        self.grant(resource="calendar", scope={"kind": "calendar", "id": "calendar"}, actions=["invite"], destinations=["alex@example.com"])
        self.assertIn("done", self.colleague().execute("POST", "me/events", event))

    def test_timezone_aware_email_date_filter(self):
        self.grant(resource="email", scope={"kind": "all", "after": "2026-01-01T10:00:00+02:00"}, actions=["read"])
        self.store.resources["me/messages/mail"]["receivedDateTime"] = "2026-01-01T08:30:00Z"
        self.assertIn("private mail", self.colleague().execute("GET", "me/messages/mail"))
        self.store.resources["me/messages/mail"]["receivedDateTime"] = "2026-01-01T07:30:00Z"
        with self.assertRaises(AccessError): self.colleague().execute("GET", "me/messages/mail")

    def test_folder_delete_and_parent_moves_cannot_bypass_scope(self):
        self.grant(scope={"kind": "all"}, actions=["edit", "delete"])
        with self.assertRaises(AccessError): self.colleague().execute("DELETE", "me/drive/items/folder")
        with self.assertRaises(AccessError): self.colleague().execute("PATCH", "me/drive/items/invoice", {"parentReference": {"id": "other"}})

    def test_corrupted_policy_and_different_instance_fail_closed(self):
        self.grant()
        other = AccessPolicy(self.store, "a" * 48, {**self.context, "agent": "other"})
        with self.assertRaises(AccessError): other.inspect()
        self.store.policy = b"not json"
        with self.assertRaises(AccessError): self.service.confirm("yes")


class GatewayApprovalTests(unittest.TestCase):
    def setUp(self):
        from test_gateway_privacy import _load_gateway
        self.gateway = _load_gateway()
        self.store = Store()
        self.context = dict(tenant="tenant", agent="agent", caller="owner", tier="owner", conversation="teams-one")
        self.gateway._access_context.update(self.context)
        self.gateway._agent_token.update(token="opaque", tier="owner")
        self.gateway._access_confirmation = ""
        self.service = AccessPolicy(self.store, "a" * 48, self.context)
        self.factory = mock.patch.object(self.gateway, "_access_service", side_effect=lambda:
            AccessPolicy(self.store, "a" * 48, dict(self.gateway._access_context)))
        self.factory.start(); self.addCleanup(self.factory.stop)

    def request(self, path, payload, tier="owner", caller="owner", conversation="teams-one"):
        handler = object.__new__(self.gateway.Handler)
        data = json.dumps(payload).encode()
        handler.path = path
        handler.headers = {"Content-Length": str(len(data)), "Authorization": "Bearer " + self.gateway.GATEWAY_KEY,
            "X-Caller-Tier": tier, "X-Caller-Object-Id": caller, "X-Access-Tenant": "tenant",
            "X-Access-Agent": "agent", "X-Access-Conversation": conversation, "x-agent-graph-token": "opaque"}
        handler.rfile = io.BytesIO(data)
        handler._send = mock.Mock()
        with mock.patch.object(self.gateway._state_sync, "prepare_turn"), mock.patch.object(self.gateway, "log"):
            handler._do_POST()
        return handler._send.call_args

    def test_propose_then_yes_is_handled_without_calling_hermes(self):
        status = self.request("/v1/access/propose", {"recipient": "alex@example.com", "resource": "onedrive",
            "scope": {"kind": "all"}, "actions": ["read"]})
        self.assertEqual(200, status.args[0])
        self.assertIn("Allow access through Hermes", self.gateway._access_confirmation)
        with mock.patch.object(self.gateway.urllib.request, "urlopen") as upstream:
            response = self.request("/v1/chat/completions", {"access_confirmation_input": "yes",
                "messages": [{"role": "user", "content": "unrelated generated prompt"}]})
        upstream.assert_not_called()
        self.assertIn(b"Permission saved", response.args[1])
        self.assertEqual(1, len(self.service.inspect()))

    def test_tool_endpoint_cannot_approve(self):
        response = self.request("/v1/access/confirm", {"answer": "yes"})
        self.assertEqual(404, response.args[0])

    def test_service_failure_on_yes_never_reaches_model(self):
        with mock.patch.object(self.gateway, "_access_service", side_effect=TimeoutError()), \
             mock.patch.object(self.gateway.urllib.request, "urlopen") as upstream:
            response = self.request("/v1/chat/completions", {"access_confirmation_input": "yes", "messages": []})
        upstream.assert_not_called()
        self.assertIn(b"No permission change is confirmed", response.args[1])

    def test_sse_confirmation_is_server_rendered_and_precedes_done(self):
        handler = object.__new__(self.gateway.Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
        class Upstream(list): status = 200
        self.gateway._access_confirmation = "Trusted confirmation"
        with mock.patch.object(self.gateway, "log"):
            handler._relay_sse(Upstream([b'data: {"choices":[]}\n\n', b'data: [DONE]\n\n']))
        output = handler.wfile.getvalue()
        self.assertLess(output.index(b"hermes.approval"), output.index(b"[DONE]"))
        self.assertIn(b"Trusted confirmation", output)

    def test_structured_model_error_is_visible_without_exposing_error_body(self):
        handler = object.__new__(self.gateway.Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
        class Upstream(list): status = 200
        with mock.patch.object(self.gateway, "log"):
            handler._relay_sse(Upstream([
                b'data: {"choices":[{"delta":{},"finish_reason":"error"}],"error":{"message":"private-provider-detail"}}\n\n',
                b'data: [DONE]\n\n']))
        output = handler.wfile.getvalue()
        self.assertIn(b"event: hermes.error", output)
        self.assertIn(b"completion is not confirmed", output)


class SidecarPermissionTests(unittest.TestCase):
    def setUp(self):
        from test_onedrive_graph import _load_sidecar
        self.sidecar = _load_sidecar()

    def test_public_fetch_checks_broker_before_network_and_retains_url_safety(self):
        with mock.patch.object(self.sidecar, "_caller_tier", return_value="colleague"), \
             mock.patch.object(self.sidecar, "_access_request", return_value={}) as broker, \
             mock.patch.object(self.sidecar, "_public_https_url", side_effect=ValueError("unsafe")) as validate:
            self.assertIn("ACCESS DENIED", self.sidecar.fetch_url("http://127.0.0.1"))
            validate.assert_not_called()
            broker.return_value = {"result": "Public research authorized"}
            self.assertIn("refusing to fetch non-public", self.sidecar.fetch_url("http://127.0.0.1"))
            validate.assert_called_once()
            broker.assert_called_with("research", {"tool": "fetch_url"})

    def test_owner_folder_access_proposal_does_not_share_in_onedrive(self):
        with mock.patch.object(self.sidecar, "_caller_tier", return_value="owner"), \
             mock.patch.object(self.sidecar, "_access_request", return_value={"result": "Confirm read/edit"}) as broker, \
             mock.patch.object(self.sidecar, "_graph") as graph:
            result = self.sidecar.propose_access_change(
                recipient="alex@example.com", resource="onedrive", actions=["read", "edit"],
                scope={"kind": "folder", "id": "folder"})
        self.assertIn("PENDING OWNER CONFIRMATION", result)
        self.assertEqual("propose", broker.call_args.args[0])
        self.assertEqual(["read", "edit"], broker.call_args.args[1]["actions"])
        graph.assert_not_called()

    def test_access_tool_descriptions_distinguish_ambiguous_sharing(self):
        # Prompt contract only; this does not prove a model follows the instruction.
        for tool in (self.sidecar.propose_access_change, self.sidecar.share_file):
            self.assertIn("Allow Alex to work", tool.__doc__)
        self.assertIn("ambiguous, ask", self.sidecar.propose_access_change.__doc__)
        self.assertIn("clarify which kind first", self.sidecar.share_file.__doc__)
        self.assertIn("Identifying the recipient alone", self.sidecar.share_file.__doc__)

    def test_colleague_graph_and_binary_paths_always_use_broker(self):
        with mock.patch.object(self.sidecar, "_caller_tier", return_value="colleague"), \
             mock.patch.object(self.sidecar, "_access_request", return_value={"error": "denied"}) as broker, \
             mock.patch.object(self.sidecar, "_token") as token:
            self.assertIn("ACCESS DENIED", self.sidecar._graph("GET", "me/messages"))
            self.assertIsNone(self.sidecar._graph_bytes("GET", "me/drive/items/item/content")[0])
            self.assertIn("ACCESS DENIED", self.sidecar._graph_put_bytes("me/drive/items/item/content", b"data"))
        self.assertEqual(3, broker.call_count)
        token.assert_not_called()

    def test_colleague_cannot_read_memory_or_propose_permissions(self):
        with mock.patch.object(self.sidecar, "_caller_tier", return_value="colleague"), \
             mock.patch.object(self.sidecar, "_access_request") as broker, \
             mock.patch.object(self.sidecar, "_graph") as graph:
            self.assertIn("ACCESS DENIED", self.sidecar.recall())
            self.assertIn("ACCESS DENIED", self.sidecar.propose_access_change())
        broker.assert_not_called(); graph.assert_not_called()

    def test_owner_cannot_write_policy_via_generic_tools(self):
        with mock.patch.object(self.sidecar, "_caller_tier", return_value="owner"), \
             mock.patch.object(self.sidecar, "_token") as token:
            self.assertIn("protected", self.sidecar._graph("PUT", POLICY_PATH, "forged"))
        token.assert_not_called()

    def test_generic_graph_cannot_create_indirect_policy_references(self):
        with mock.patch.object(self.sidecar, "_action_policy_guard", return_value=None), \
             mock.patch.object(self.sidecar, "_graph") as graph:
            for body in [{"remoteItem": {"id": "policy"}},
                         {"parentReference": {"id": "elsewhere"}},
                         {"sourceUrl": "https://example.com/data"}]:
                result = self.sidecar.graph_write("POST", "me/drive/root/children", json.dumps(body))
                self.assertIn("not supported", result)
        graph.assert_not_called()


if __name__ == "__main__":
    unittest.main()
