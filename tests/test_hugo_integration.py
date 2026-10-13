"""End-to-end checks using only a disposable site and an optional Hugo binary."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HUGO = os.environ.get("HUGO_TEST_BIN") or shutil.which("hugo")


@unittest.skipUnless(HUGO, "set HUGO_TEST_BIN to run isolated Hugo integration checks")
class HugoIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "site"
        self.data = self.root / "data"
        source = Path(__file__).resolve().parents[1] / "server.py"
        spec = importlib.util.spec_from_file_location("hugo_integration_server", source)
        self.server = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "HUGO_SITE_ROOT": str(self.site), "HUGO_MCP_DATA_ROOT": str(self.data),
            "HUGO_POSTS_DIR": "content/posts", "HUGO_PUBLIC_DIR": "public",
            "HUGO_MCP_TOKEN_FILE": str(self.data / "token"), "HUGO_BIN": HUGO,
        }):
            spec.loader.exec_module(self.server)
        self.server.ensure_roots()
        self.server.atomic_write(self.site / "hugo.toml", 'baseURL = "https://example.invalid/"\n')
        self.list_template = self.site / "layouts/_default/list.html"
        self.single_template = self.site / "layouts/_default/single.html"
        self.server.atomic_write(self.list_template, "{{ range (.Paginate .Pages 1).Pages }}{{ .Title }}{{ end }}")
        self.server.atomic_write(self.single_template, "{{ .Content }}{{ range .Resources }}{{ .RelPermalink }}{{ end }}")

    def seed(self, slug, draft=False, **front):
        path = self.server.bundle_post_path(slug)
        self.server.atomic_write(path, self.server.render_markdown(
            {"title": slug, "slug": slug, "draft": draft, **front}, "Original fixture body."
        ))
        return path

    def item(self, path, action):
        return {"slug": path.parent.name, "action": action,
                "expected_revision": self.server.sha256_file(path)}

    def public_files(self):
        return {str(path.relative_to(self.server.PUBLIC_ROOT)): path.read_bytes()
                for path in self.server.PUBLIC_ROOT.rglob("*") if path.is_file()}

    def test_batch_removes_custom_permalink_taxonomy_pagination_and_bundle_resource(self):
        first = self.seed("first", draft=True, tags=["exclusive"], url="/library/first/")
        second = self.seed("second", draft=True)
        third = self.seed("third", draft=True)
        self.server.atomic_write(first.parent / "attachment.txt", "Fixture bundle resource")
        unmanaged = self.server.PUBLIC_ROOT / "legacy/attachment.txt"
        self.server.atomic_write(unmanaged, "Unmanaged fixture resource")
        items = [self.item(path, "publish") for path in (first, second, third)]
        with mock.patch.object(self.server, "build_site", wraps=self.server.build_site) as build:
            preview = self.server.call_tool("hugo_batch_posts", {"items": items})
            self.assertEqual(preview["action"], "planned")
            build.assert_not_called()
            self.server.call_tool("hugo_batch_posts", {
                "items": items, "dry_run": False, "request_id": "publish-fixtures",
            })
            self.assertEqual(build.call_count, 2)
            self.assertEqual(build.call_args_list[0].kwargs, {"baseline_only": True})
        public = self.server.PUBLIC_ROOT
        for relative in ("library/first/index.html", "library/first/attachment.txt",
                         "tags/exclusive/index.html", "posts/page/3/index.html"):
            self.assertTrue((public / relative).exists(), relative)
        arguments = {"items": [self.item(first, "delete"), self.item(second, "unpublish")],
                     "dry_run": False, "request_id": "remove-fixtures"}
        with mock.patch.object(self.server, "build_site", wraps=self.server.build_site) as build:
            result = self.server.call_tool("hugo_batch_posts", arguments)
            replay = self.server.call_tool("hugo_batch_posts", arguments)
            build.assert_called_once_with()
        self.assertEqual(result, replay)
        self.assertGreater(result["build"]["removed_count"], 0)
        for relative in ("library/first", "posts/second", "tags/exclusive", "posts/page/2", "posts/page/3"):
            self.assertFalse((public / relative).exists(), relative)
        self.assertTrue((public / "posts/third/index.html").is_file())
        self.assertEqual(unmanaged.read_text(), "Unmanaged fixture resource")
        trashed = Path(result["items"][0]["trash_path"])
        self.assertTrue((trashed / "index.md").is_file())
        self.assertTrue((trashed / "attachment.txt").is_file())

    def test_failed_build_preserves_public_then_same_request_resumes(self):
        path = self.seed("example")
        self.server.call_tool("hugo_build", {})
        original = self.public_files()
        self.server.atomic_write(self.single_template, "{{ invalidFixtureFunction }}")
        arguments = {"slug": "example", "expected_revision": self.server.sha256_file(path),
                     "request_id": "unpublish-fixture"}
        with self.assertRaises(self.server.RetryableMutationError):
            self.server.call_tool("hugo_unpublish_post", arguments)
        self.assertEqual(self.public_files(), original)
        self.assertTrue(self.server.parse_markdown(path)[0]["draft"])
        after_source = path.read_bytes()
        self.server.atomic_write(self.single_template, "{{ .Content }}")
        result = self.server.call_tool("hugo_unpublish_post", arguments)
        self.assertEqual(result["action"], "unpublished")
        self.assertEqual(path.read_bytes(), after_source)
        self.assertFalse((self.server.PUBLIC_ROOT / "posts/example").exists())

    def test_first_baseline_does_not_publish_changed_source_if_final_build_fails(self):
        path = self.seed("example")
        public_page = self.server.PUBLIC_ROOT / "posts/example/index.html"
        self.server.atomic_write(public_page, "Previously published fixture")
        original = self.public_files()
        arguments = {"slug": "example", "expected_revision": self.server.sha256_file(path),
                     "request_id": "first-unpublish"}
        real_build = self.server.build_site

        def fail_after_baseline(*, baseline_only=False):
            if baseline_only:
                result = real_build(baseline_only=True)
                self.assertEqual(self.public_files(), original)
                return result
            raise RuntimeError("Fixture final build failure")

        with mock.patch.object(self.server, "build_site", side_effect=fail_after_baseline):
            with self.assertRaises(self.server.RetryableMutationError):
                self.server.call_tool("hugo_unpublish_post", arguments)
        self.assertEqual(self.public_files(), original)
        self.server.call_tool("hugo_unpublish_post", arguments)
        self.assertFalse(public_page.exists())


if __name__ == "__main__":
    unittest.main()
