import importlib.util
import inspect
import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

try:
    from docx import Document
except ImportError:
    Document = None


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        return lambda fn: fn


def _load_sidecar():
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FakeFastMCP
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp
    mcp = types.ModuleType("mcp")
    mcp.server = server
    with mock.patch.dict(sys.modules, {
        "mcp": mcp,
        "mcp.server": server,
        "mcp.server.fastmcp": fastmcp,
    }):
        path = pathlib.Path(__file__).parents[1] / "src" / "sidecar" / "agent365_mcp.py"
        spec = importlib.util.spec_from_file_location("hfull_agent365_mcp_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _drive_404(url):
    return urllib.error.HTTPError(
        url, 404, "not found", {}, io.BytesIO(b'{"error":{"code":"itemNotFound"}}')
    )


class GraphOneDriveTests(unittest.TestCase):
    def setUp(self):
        self.authority = mock.patch.dict(os.environ, {"AGENT_CALLER_TIER": "owner"})
        self.authority.start()
        self.addCleanup(self.authority.stop)
        self.m = _load_sidecar()

    def test_unproven_native_provisioning_tools_are_not_exposed(self):
        self.assertFalse(hasattr(self.m, "provision_onedrive"))
        self.assertFalse(hasattr(self.m, "list_files_mcp"))
        self.assertFalse(hasattr(self.m, "_odsp_tool_call"))

    def test_state_sync_config_is_owner_only_before_gateway_call(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="colleague"), \
             mock.patch.object(self.m, "_state_sync_config_request") as request:
            result = self.m.state_sync_config("get")

        request.assert_not_called()
        self.assertIn("only the verified owner", result)

    def test_state_sync_config_get_returns_gateway_validated_view(self):
        value = {
            "include_paths": ["memories", "skills"],
            "exclude_paths": [],
            "available_unlisted_paths": ["future-state"],
        }
        with mock.patch.object(
            self.m, "_state_sync_config_request", return_value=(200, value)
        ) as request:
            result = self.m.state_sync_config("get")

        request.assert_called_once_with("GET")
        self.assertEqual(value, json.loads(result))

    def test_state_sync_config_set_requires_complete_configuration(self):
        with mock.patch.object(self.m, "_state_sync_config_request") as request:
            result = self.m.state_sync_config("set", include_paths=["memories"])

        request.assert_not_called()
        self.assertIn("requires the complete configuration", result)

    def test_state_sync_config_set_preserves_explicit_nondefault_values(self):
        with mock.patch.object(
            self.m, "_state_sync_config_request", return_value=(200, {"enabled": False}),
        ) as request:
            result = self.m.state_sync_config(
                "set",
                include_paths=["memories"],
                exclude_paths=["memories/private"],
                enabled=False,
                max_archive_bytes=10_000_000,
                post_turn_settle_seconds=7,
            )

        request.assert_called_once_with("POST", {
            "version": 1,
            "enabled": False,
            "include_paths": ["memories"],
            "exclude_paths": ["memories/private"],
            "max_archive_bytes": 10_000_000,
            "post_turn_settle_seconds": 7,
        })
        self.assertIn("STATE SYNC CONFIG UPDATED", result)

    def test_create_routine_returns_complete_task_to_edge_without_identity_fields(self):
        with mock.patch.object(self.m, "_routine_control", return_value="created") as control:
            result = self.m.create_routine(
                "daily-mail",
                "Summarize my own inbox and email the summary to the owner.",
                "0 7 * * *",
                "Europe/London",
            )

        control.assert_called_once_with("create", {
            "name": "daily-mail",
            "task": "Summarize my own inbox and email the summary to the owner.",
            "cronExpression": "0 7 * * *",
            "timeZone": "Europe/London",
        })
        self.assertEqual(result, "created")

    def test_create_routine_documents_foundry_only_scheduling_and_fresh_obo(self):
        guidance = self.m.create_routine.__doc__ or ""
        self.assertIn("Cron jobs", guidance)
        self.assertIn("Foundry Routines API", guidance)
        self.assertIn("fresh short-lived downstream token", guidance)
        self.assertIn("Never ask for or put", guidance)
        self.assertIn("call recall", guidance)
        self.assertIn("Owner scheduling timezone (IANA)", guidance)

    def test_create_routine_requires_an_explicit_timezone(self):
        parameter = inspect.signature(self.m.create_routine).parameters["time_zone"]
        self.assertIs(parameter.default, inspect.Signature.empty)

    def test_routine_management_fails_closed_without_owner_capability(self):
        with mock.patch.object(self.m, "_routine_capability", return_value=""), \
             mock.patch.object(self.m.urllib.request, "urlopen") as urlopen:
            result = self.m._routine_control("list", {})

        urlopen.assert_not_called()
        self.assertIn("ROUTINE NOT CHANGED", result)
        self.assertIn("verified owner", result)

    def test_remove_routine_uses_name_only_and_never_accepts_identity_ids(self):
        with mock.patch.object(self.m, "_routine_control", return_value="removed") as control:
            result = self.m.remove_routine("daily-mail")

        control.assert_called_once_with("delete", {"name": "daily-mail"})
        self.assertEqual(result, "removed")

    def test_list_files_uses_graph_and_clamps_page_size(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[]}') as graph:
            result = self.m.list_files(top=500)

        graph.assert_called_once_with(
            "GET",
            "me/drive/root/children?$top=100"
            "&$select=id,name,size,lastModifiedDateTime,webUrl,folder,file",
        )
        self.assertEqual(result, '{"value":[]}')

    def test_tool_outcomes_reach_telemetry_without_arguments_or_results(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[]}'), \
             mock.patch.object(self.m, "_edge_log") as edge_log:
            self.m.list_files(top=3)

        edge_log.assert_has_calls([
            mock.call("tool.start", "name=list_files"),
            mock.call("tool.outcome", mock.ANY),
        ])
        outcome = edge_log.call_args_list[-1].args[1]
        self.assertIn("name=list_files status=ok durationMs=", outcome)
        self.assertNotIn('{"value":[]}', outcome)
        self.assertNotIn("top=3", outcome)

    def test_tool_failure_taxonomy_marks_truthful_terminal_results_failed(self):
        for result in (
            "EVENT NOT CREATED — nothing was scheduled",
            "SHARE FAILED — the file was not shared",
            "DOCX NOT CHANGED — item was locked",
            "ROUTINE NOT CHANGED — owner capability missing",
        ):
            with self.subTest(result=result):
                self.assertTrue(self.m._tool_result_failed(result))
        self.assertFalse(self.m._tool_result_failed("EVENT CREATED"))

    def test_search_files_uses_graph_and_escapes_odata_query(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[]}') as graph:
            result = self.m.search_files("John's plan", top=7)

        graph.assert_called_once_with(
            "GET",
            "me/drive/root/search(q='John%27%27s%20plan')?$top=7"
            "&$select=id,name,size,lastModifiedDateTime,webUrl,folder,file",
        )
        self.assertEqual(result, '{"value":[]}')

    def test_send_email_normalizes_display_name_mailbox_for_graph(self):
        with mock.patch.object(self.m, "_graph", return_value="") as graph:
            result = self.m.send_email(
                "John Doe <john@example.com>", "Routine test", "Test"
            )

        graph.assert_called_once_with("POST", "me/sendMail", {
            "message": {
                "subject": "Routine test",
                "body": {"contentType": "Text", "content": "Test"},
                "toRecipients": [{"emailAddress": {
                    "address": "john@example.com", "name": "John Doe",
                }}],
            },
            "saveToSentItems": True,
        })
        self.assertEqual(result, "Sent.")

    def test_outsider_graph_mutations_are_blocked_before_token_or_network(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="outsider"), \
             mock.patch.object(self.m, "_token") as token, \
             mock.patch.object(self.m.urllib.request, "urlopen") as urlopen:
            results = [
                self.m._graph(method, "me/example", {"value": "untrusted"})
                for method in ("POST", "PUT", "PATCH", "DELETE")
            ]

        token.assert_not_called()
        urlopen.assert_not_called()
        for result in results:
            self.assertIn("ACTION NOT PERFORMED", result)
            self.assertIn("No request was sent to Graph", result)

    def test_owner_only_outbound_policy_blocks_colleague_before_graph(self):
        with mock.patch.dict(os.environ, {
            "AGENT_CALLER_TIER": "colleague",
            "M365_OUTBOUND_POLICY": "owner-only",
        }), mock.patch.object(self.m, "_graph") as graph:
            result = self.m.send_email("person@example.com", "Subject", "Body")

        graph.assert_not_called()
        self.assertIn("verified owner conversation", result)
        self.assertIn("No request was sent to Graph", result)

    def test_default_outbound_policy_preserves_internal_colleague_actions(self):
        with mock.patch.dict(os.environ, {
            "AGENT_CALLER_TIER": "colleague",
            "M365_OUTBOUND_POLICY": "internal",
        }), mock.patch.object(self.m, "_graph", return_value="OK (202)") as graph:
            result = self.m.send_email("person@example.com", "Subject", "Body")

        graph.assert_called_once()
        self.assertEqual("Sent.", result)

    def test_invalid_write_policy_fails_closed_before_token_or_network(self):
        with mock.patch.dict(os.environ, {
            "AGENT_CALLER_TIER": "owner",
            "M365_WRITE_POLICY": "typo",
        }), mock.patch.object(self.m, "_token") as token, \
             mock.patch.object(self.m.urllib.request, "urlopen") as urlopen:
            result = self.m._graph("POST", "me/sendMail", {})

        token.assert_not_called()
        urlopen.assert_not_called()
        self.assertIn("M365_WRITE_POLICY is invalid", result)
        self.assertIn("No request was sent to Graph", result)

    def test_action_policies_flow_to_initial_and_upgrade_versions(self):
        root = pathlib.Path(__file__).parents[1]
        initial = (root / "scripts/create-hosted-blueprint.sh").read_text()
        upgrade = (root / "new-version.sh").read_text()
        for setting in ("M365_WRITE_POLICY", "M365_OUTBOUND_POLICY"):
            self.assertIn(f'"{setting}"', initial)
            self.assertIn(f'"{setting}":', initial)
            self.assertIn(f'"{setting}"', upgrade)
            self.assertIn(f'env["{setting}"]', upgrade)

    def test_outsider_binary_upload_is_blocked_before_token_or_network(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="outsider"), \
             mock.patch.object(self.m, "_token") as token, \
             mock.patch.object(self.m.urllib.request, "urlopen") as urlopen:
            result = self.m._graph_put_bytes("me/drive/items/item-1/content", b"bytes")

        token.assert_not_called()
        urlopen.assert_not_called()
        self.assertIn("ACTION NOT PERFORMED", result)

    def test_outsider_tool_results_never_claim_mutation_success(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="outsider"), \
             mock.patch.object(self.m.urllib.request, "urlopen") as urlopen:
            email = self.m.send_email("john@example.com", "Test", "Body")
            event = self.m.create_event(
                "Test", "2026-09-17T09:00:00", "2026-09-17T09:30:00"
            )
            share = self.m.share_file("item-1", "john@example.com")

        urlopen.assert_not_called()
        self.assertNotEqual("Sent.", email)
        self.assertIn("ACTION NOT PERFORMED", email)
        self.assertIn("EVENT NOT CREATED", event)
        self.assertIn("SHARE FAILED", share)

    def test_mailbox_parser_handles_quoted_commas_and_multiple_recipients(self):
        self.assertEqual(self.m._mailboxes(
            '\"Doe, John\" <john@example.com>, jane@example.com'
        ), [
            {"emailAddress": {
                "address": "john@example.com", "name": "Doe, John",
            }},
            {"emailAddress": {"address": "jane@example.com"}},
        ])

    def test_event_attendees_normalize_display_name_mailboxes(self):
        self.assertEqual(
            self.m._attendees(
                "John Doe <john@example.com>", "jane@example.com"
            ),
            [
                {"emailAddress": {
                    "address": "john@example.com", "name": "John Doe",
                }, "type": "required"},
                {"emailAddress": {"address": "jane@example.com"}, "type": "optional"},
            ],
        )

    def test_read_file_by_item_id_uses_graph_content_endpoint(self):
        with mock.patch.object(self.m, "_graph", return_value="version 33 test") as graph:
            result = self.m.read_file(item_id="item-1")

        graph.assert_called_once_with("GET", "me/drive/items/item-1/content")
        self.assertEqual(result, "version 33 test")

    def test_read_file_supports_nested_and_reserved_path_characters(self):
        with mock.patch.object(self.m, "_graph", return_value="nested") as graph:
            result = self.m.read_file(path="Projects/Quarter 1/#notes?.txt")

        graph.assert_called_once_with(
            "GET", "me/drive/root:/Projects/Quarter%201/%23notes%3F.txt:/content"
        )
        self.assertEqual(result, "nested")

    def test_create_file_uses_graph_put_and_supports_nested_paths(self):
        with mock.patch.object(self.m, "_graph", return_value='{"id":"item-2"}') as graph:
            result = self.m.create_file("Projects/Quarter 1/notes.txt", "version 33 test")

        graph.assert_called_once_with(
            "PUT",
            "me/drive/root:/Projects/Quarter%201/notes.txt:/content",
            "version 33 test",
            raw_body=True,
        )
        self.assertEqual(result, '{"id":"item-2"}')

    def test_create_file_still_refuses_fake_binary_documents(self):
        with mock.patch.object(self.m, "_graph") as graph:
            result = self.m.create_file("fake.docx", "not a real docx")

        graph.assert_not_called()
        self.assertIn("FILE NOT CREATED", result)
        self.assertIn("create_docx", result)

    @unittest.skipUnless(Document, "python-docx is installed in the production image")
    def test_docx_replacement_handles_text_split_across_word_runs(self):
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("before TES").bold = True
        paragraph.add_run("T 123 after").italic = True

        count = self.m._replace_docx_paragraph(paragraph, "TEST 123", "DONE")

        self.assertEqual(count, 1)
        self.assertEqual(paragraph.text, "before DONE after")

    @unittest.skipUnless(Document, "python-docx is installed in the production image")
    def test_edit_docx_updates_same_item_with_etag_guard(self):
        doc = Document()
        doc.add_paragraph("Existing paragraph")
        original = io.BytesIO()
        doc.save(original)

        with mock.patch.object(self.m, "_graph", return_value=json.dumps({
            "id": "item-1", "name": "Report.docx", "eTag": "etag-1",
        })) as graph, \
             mock.patch.object(self.m, "_graph_bytes",
                               return_value=(original.getvalue(), None)) as download, \
             mock.patch.object(self.m, "_graph_put_bytes",
                               return_value='{"id":"item-1"}') as upload:
            result = self.m.edit_docx(item_id="item-1", append_content="## Update\nTEST 123")

        graph.assert_called_once_with(
            "GET", "me/drive/items/item-1?$select=id,name,eTag"
        )
        download.assert_called_once_with("GET", "me/drive/items/item-1/content")
        upload_path, updated = upload.call_args.args
        self.assertEqual(upload_path, "me/drive/items/item-1/content")
        self.assertEqual(upload.call_args.kwargs["if_match"], "etag-1")
        reopened = Document(io.BytesIO(updated))
        text = "\n".join(p.text for p in reopened.paragraphs)
        self.assertIn("Existing paragraph", text)
        self.assertIn("Update", text)
        self.assertIn("TEST 123", text)
        self.assertIn("DOCX UPDATED", result)

    @unittest.skipUnless(Document, "python-docx is installed in the production image")
    def test_edit_docx_does_not_upload_when_find_text_is_missing(self):
        doc = Document()
        doc.add_paragraph("Original")
        original = io.BytesIO()
        doc.save(original)

        with mock.patch.object(self.m, "_graph", return_value=json.dumps({
            "id": "item-1", "name": "Report.docx", "eTag": "etag-1",
        })), \
             mock.patch.object(self.m, "_graph_bytes",
                               return_value=(original.getvalue(), None)), \
             mock.patch.object(self.m, "_graph_put_bytes") as upload:
            result = self.m.edit_docx(
                item_id="item-1", find_text="not present", replace_text="replacement"
            )

        upload.assert_not_called()
        self.assertIn("DOCX NOT CHANGED", result)

    def test_create_file_cannot_overwrite_reserved_owner_memory(self):
        with mock.patch.object(self.m, "_graph") as graph:
            result = self.m.create_file("./SHARED_MEMORY.md", "forged permission")

        graph.assert_not_called()
        self.assertIn("FILE NOT CHANGED", result)
        self.assertIn("remember", result)

    def test_uploaded_file_reader_accepts_only_current_turn_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            inside = inbox / "notes.txt"
            outside = root / "private.txt"
            inside.write_text("current turn content")
            outside.write_text("must not escape")

            with mock.patch.object(self.m, "_INBOUND_UPLOAD_ROOT", str(inbox.resolve())):
                accepted = self.m.read_uploaded_file(str(inside))
                refused = self.m.read_uploaded_file(str(outside))

        self.assertEqual("current turn content", accepted)
        self.assertIn("ERROR reading uploaded file", refused)
        self.assertIn("The file was not reviewed", refused)
        self.assertNotIn(str(outside), refused)
        self.assertNotIn("must not escape", refused)

    def test_uploaded_file_reader_resolves_symlinks_before_boundary_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside data")
            link = inbox / "linked.txt"
            link.symlink_to(outside)

            with mock.patch.object(self.m, "_INBOUND_UPLOAD_ROOT", str(inbox.resolve())):
                result = self.m.read_uploaded_file(str(link))

        self.assertIn("ERROR reading uploaded file", result)
        self.assertNotIn("outside data", result)

    def test_uploaded_file_parser_failure_has_stable_non_path_error(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = pathlib.Path(directory)
            broken = inbox / "broken.docx"
            broken.write_bytes(b"not an OOXML file")

            with mock.patch.object(self.m, "_INBOUND_UPLOAD_ROOT", str(inbox.resolve())):
                result = self.m.read_uploaded_file(str(broken))

        self.assertIn("ERROR reading uploaded file", result)
        self.assertIn("The file was not reviewed", result)
        self.assertNotIn(str(broken), result)

    def test_recall_frames_memory_as_authoritative_owner_data(self):
        with mock.patch.object(self.m, "_graph", return_value="# Shared owner memory\n\nAllowed") as graph:
            result = self.m.recall()

        graph.assert_called_once_with("GET", "me/drive/root:/SHARED_MEMORY.md:/content")
        self.assertIn("AUTHORITATIVE OWNER MEMORY", result)
        self.assertIn("Allowed", result)

    def test_recall_empty_memory_does_not_infer_permission(self):
        with mock.patch.object(self.m, "_graph", return_value="ERROR 404 from Graph: missing"):
            result = self.m.recall()

        self.assertIn("OWNER MEMORY IS EMPTY", result)
        self.assertIn("Do not infer permission", result)

    def test_remember_hard_refuses_non_owner_without_touching_graph(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="colleague"), \
             mock.patch.object(self.m, "_graph") as graph:
            result = self.m.remember("John may ask me to share reports")

        graph.assert_not_called()
        self.assertIn("MEMORY NOT CHANGED", result)
        self.assertIn("verified owner", result)

    def test_remember_initialises_agent_drive_file_and_appends_owner_entry(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="owner"), \
             mock.patch.object(self.m, "_graph", side_effect=[
                 "ERROR 404 from Graph: missing",
                 '{"id":"memory-item"}',
             ]) as graph:
            result = self.m.remember("John may ask me to share the weekly report")

        self.assertEqual(graph.call_count, 2)
        self.assertEqual(
            graph.call_args_list[0],
            mock.call("GET", "me/drive/root:/SHARED_MEMORY.md:/content"),
        )
        method, path, content = graph.call_args_list[1].args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "me/drive/root:/SHARED_MEMORY.md:/content")
        self.assertIn("# Shared owner memory", content)
        self.assertIn("John may ask me", content)
        self.assertTrue(graph.call_args_list[1].kwargs["raw_body"])
        self.assertIn("REMEMBERED", result)

    def test_remember_never_overwrites_when_existing_memory_read_fails(self):
        with mock.patch.object(self.m, "_caller_tier", return_value="owner"), \
             mock.patch.object(self.m, "_graph", return_value="ERROR 403 from Graph: denied") as graph:
            result = self.m.remember("permission")

        graph.assert_called_once()
        self.assertIn("MEMORY NOT CHANGED", result)
        self.assertIn("risk data loss", result)

    def test_drive_404_does_not_claim_provisioning_or_suggest_native_fallback(self):
        with mock.patch.object(self.m, "_token", return_value="graph-token"), \
             mock.patch.object(self.m.urllib.request, "urlopen",
                               side_effect=_drive_404("https://graph.microsoft.com/v1.0/me/drive")):
            result = self.m._graph("GET", "me/drive")

        self.assertIn("requested OneDrive or item was not found", result)
        self.assertIn("NOTHING was created or changed", result)
        self.assertNotIn("provision", result.lower())
        self.assertNotIn("native", result.lower())

    def test_share_file_remains_graph_backed(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[{"id":"permission-1"}]}') as graph:
            result = self.m.share_file("item-1", "john@example.com", role="read", message="Test")

        graph.assert_called_once_with("POST", "me/drive/items/item-1/invite", {
            "recipients": [{"email": "john@example.com"}],
            "message": "Test",
            "requireSignIn": True,
            "sendInvitation": True,
            "roles": ["read"],
        })
        self.assertIn("SHARED OK", result)

    def test_resolve_person_uses_tenant_directory_and_prefers_exact_name(self):
        directory = '''{"value":[
            {"id":"user-1","displayName":"JohnDow","mail":"john@example.com",
             "userPrincipalName":"john@example.com","accountEnabled":true},
            {"id":"user-2","displayName":"JohnDowling","mail":"dowling@example.com",
             "userPrincipalName":"dowling@example.com","accountEnabled":true}
        ]}'''
        with mock.patch.object(self.m, "_graph", return_value=directory) as graph:
            result = self.m.resolve_person("JohnDow")

        method, path = graph.call_args.args
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("users?$filter="))
        self.assertIn("displayName", path)
        self.assertNotIn("me/contacts", path)
        self.assertIn("PERSON RESOLVED", result)
        self.assertIn("john@example.com", result)
        self.assertNotIn("dowling@example.com", result)

    def test_resolve_person_reports_ambiguous_matches_without_guessing(self):
        directory = '''{"value":[
            {"id":"user-1","displayName":"Alex Smith","mail":"alex1@example.com",
             "userPrincipalName":"alex1@example.com","accountEnabled":true},
            {"id":"user-2","displayName":"Alex Jones","mail":"alex2@example.com",
             "userPrincipalName":"alex2@example.com","accountEnabled":true}
        ]}'''
        with mock.patch.object(self.m, "_graph", return_value=directory):
            result = self.m.resolve_person("Alex")

        self.assertIn("AMBIGUOUS DIRECTORY MATCH", result)
        self.assertIn("alex1@example.com", result)
        self.assertIn("alex2@example.com", result)
        self.assertIn("Do not guess", result)

    def test_resolve_person_does_not_fallback_to_contacts_after_graph_error(self):
        with mock.patch.object(self.m, "_graph", return_value="ERROR 403 from Graph: denied") as graph:
            result = self.m.resolve_person("JohnDow")

        graph.assert_called_once()
        self.assertIn("PERSON LOOKUP FAILED", result)
        self.assertIn("Do not retry through me/contacts", result)

    def test_resolve_person_rejects_empty_query_without_calling_graph(self):
        with mock.patch.object(self.m, "_graph") as graph:
            result = self.m.resolve_person("  ")

        graph.assert_not_called()
        self.assertIn("ERROR", result)


if __name__ == "__main__":
    unittest.main()
