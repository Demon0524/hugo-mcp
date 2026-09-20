import base64
import importlib
import os
import tempfile
import unittest
from pathlib import Path


class HugoMcpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.site = root / "site"
        cls.data = root / "data"
        (cls.site / "content" / "posts").mkdir(parents=True)
        cls.data.mkdir()
        os.environ["HUGO_SITE_ROOT"] = str(cls.site)
        os.environ["HUGO_MCP_DATA_ROOT"] = str(cls.data)
        os.environ["HUGO_MCP_TOKEN_FILE"] = str(cls.data / "token")
        os.environ["HUGO_BIN"] = "/bin/true"
        cls.server = importlib.import_module("server")
        cls.server.ensure_roots()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_create_bundle_and_upload_media(self):
        created = self.server.call_tool(
            "hugo_create_draft",
            {"title": "TrueNAS Guide", "slug": "truenas-guide", "request_id": "create-001"},
        )
        self.assertTrue(created["post"]["bundle"])
        self.assertEqual(created["post"]["path"], "content/posts/truenas-guide/index.md")

        png = b"\x89PNG\r\n\x1a\nminimal-test-payload"
        uploaded = self.server.call_tool(
            "hugo_upload_media",
            {
                "post_slug": "truenas-guide",
                "filename": "step 01.png",
                "mime_type": "image/png",
                "data_base64": base64.b64encode(png).decode("ascii"),
                "alt": "Pool layout",
                "role": "inline",
                "request_id": "upload-001",
            },
        )
        self.assertEqual(uploaded["media"]["filename"], "step-01.png")
        self.assertEqual(uploaded["media"]["markdown"], "![Pool layout](step-01.png)")

        listed = self.server.call_tool("hugo_list_media", {"post_slug": "truenas-guide"})
        self.assertEqual(listed["total"], 1)
        deleted = self.server.call_tool(
            "hugo_delete_media",
            {
                "post_slug": "truenas-guide",
                "filename": "step-01.png",
                "expected_revision": listed["media"][0]["revision"],
                "request_id": "delete-media-001",
            },
        )
        self.assertEqual(deleted["action"], "soft_deleted")

        self.assertFalse((self.server.bundle_root("truenas-guide") / "step-01.png").exists())

        post = self.server.call_tool("hugo_get_post", {"slug": "truenas-guide"})
        self.server.call_tool(
            "hugo_delete_post",
            {
                "slug": "truenas-guide",
                "expected_revision": post["post"]["revision"],
                "request_id": "delete-post-001",
            },
        )
        self.assertFalse((self.server.bundle_root("truenas-guide") / "index.md").exists())

    def test_migrate_flat_post_to_bundle(self):
        flat = self.server.post_path("legacy-post")
        self.server.atomic_write(
            flat,
            self.server.render_markdown(
                {"title": "Legacy", "slug": "legacy-post", "draft": True}, "old body"
            ),
        )
        before = self.server.sha256_file(flat)
        migrated = self.server.call_tool(
            "hugo_migrate_post_bundle",
            {"slug": "legacy-post", "expected_revision": before, "request_id": "migrate-001"},
        )
        self.assertTrue(migrated["post"]["bundle"])
        self.assertFalse(flat.exists())
        self.assertTrue((self.server.bundle_root("legacy-post") / "index.md").exists())


if __name__ == "__main__":
    unittest.main()
