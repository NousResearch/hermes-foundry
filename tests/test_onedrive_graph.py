import importlib.util
import io
import pathlib
import sys
import types
import unittest
import urllib.error
from unittest import mock


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
        spec = importlib.util.spec_from_file_location("full_foundry_agent365_mcp_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _drive_404(url):
    return urllib.error.HTTPError(
        url, 404, "not found", {}, io.BytesIO(b'{"error":{"code":"itemNotFound"}}')
    )


class GraphOneDriveTests(unittest.TestCase):
    def setUp(self):
        self.m = _load_sidecar()

    def test_unproven_native_provisioning_tools_are_not_exposed(self):
        self.assertFalse(hasattr(self.m, "provision_onedrive"))
        self.assertFalse(hasattr(self.m, "list_files_mcp"))
        self.assertFalse(hasattr(self.m, "_odsp_tool_call"))

    def test_list_files_uses_graph_and_clamps_page_size(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[]}') as graph:
            result = self.m.list_files(top=500)

        graph.assert_called_once_with(
            "GET",
            "me/drive/root/children?$top=100"
            "&$select=id,name,size,lastModifiedDateTime,webUrl,folder,file",
        )
        self.assertEqual(result, '{"value":[]}')

    def test_search_files_uses_graph_and_escapes_odata_query(self):
        with mock.patch.object(self.m, "_graph", return_value='{"value":[]}') as graph:
            result = self.m.search_files("Alex's plan", top=7)

        graph.assert_called_once_with(
            "GET",
            "me/drive/root/search(q='Alex%27%27s%20plan')?$top=7"
            "&$select=id,name,size,lastModifiedDateTime,webUrl,folder,file",
        )
        self.assertEqual(result, '{"value":[]}')

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
            result = self.m.share_file("item-1", "user@example.com", role="read", message="Test")

        graph.assert_called_once_with("POST", "me/drive/items/item-1/invite", {
            "recipients": [{"email": "user@example.com"}],
            "message": "Test",
            "requireSignIn": True,
            "sendInvitation": True,
            "roles": ["read"],
        })
        self.assertIn("SHARED OK", result)


if __name__ == "__main__":
    unittest.main()
