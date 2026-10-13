import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import output_build


class OutputBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.site = root / "site"
        self.public = self.site / "public"
        self.data = root / "private"
        self.site.mkdir()
        self.public.mkdir()
        self.data.mkdir()

    def write_public(self, relative, content):
        path = self.public / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def build(self, outputs, **options):
        def run(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(command[1:3], ["--source", str(self.site)])
            destination = Path(command[command.index("--destination") + 1])
            self.assertIn(self.data, destination.parents)
            self.assertNotIn(self.public, destination.parents)
            for relative, content in outputs.items():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            return subprocess.CompletedProcess(command, 0, "built", "")

        with patch("output_build.subprocess.run", side_effect=run):
            return output_build.build_site(self.site, self.public, self.data, "hugo", 5, **options)

    def test_baseline_adopts_public_hashes_without_publishing_source_changes(self):
        self.write_public("index.html", b"previously published")
        self.write_public("posts/example/index.html", b"old public article")
        self.write_public("legacy/image.jpg", b"unknown attachment")
        result = self.build({
            "index.html": b"source changes not published",
            "posts/example/index.html": b"new source article",
            "not-yet-public.html": b"not published",
        }, baseline_only=True)
        self.assertTrue(result["baseline_only"])
        self.assertEqual(result["tracked_count"], 2)
        self.assertEqual((self.public / "index.html").read_bytes(), b"previously published")
        self.assertEqual((self.public / "posts/example/index.html").read_bytes(), b"old public article")
        self.assertEqual((self.public / "legacy/image.jpg").read_bytes(), b"unknown attachment")
        self.assertFalse((self.public / "not-yet-public.html").exists())
        state = json.loads(output_build.manifest_path(self.data).read_text())
        self.assertEqual(state["files"], {
            "index.html": hashlib.sha256(b"previously published").hexdigest(),
            "posts/example/index.html": hashlib.sha256(b"old public article").hexdigest(),
        })
        result = self.build({"index.html": b"successful new build"})
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse((self.public / "posts/example/index.html").exists())
        self.assertEqual((self.public / "legacy/image.jpg").read_bytes(), b"unknown attachment")

    def test_baseline_does_not_create_public_directory(self):
        self.public.rmdir()
        self.build({"index.html": b"not published"}, baseline_only=True)
        self.assertFalse(self.public.exists())
        self.assertEqual(json.loads(output_build.manifest_path(self.data).read_text())["files"], {})

    def test_baseline_does_not_replace_existing_manifest(self):
        self.build({"index.html": b"published", "old.html": b"old"})
        before = output_build.manifest_path(self.data).read_bytes()
        self.build({"index.html": b"not published"}, baseline_only=True)
        self.assertEqual(output_build.manifest_path(self.data).read_bytes(), before)
        self.assertEqual((self.public / "index.html").read_bytes(), b"published")
        self.assertTrue((self.public / "old.html").is_file())

    def test_baseline_keeps_interrupted_initial_sync_ownership(self):
        copy_output = output_build._copy_output

        def copy_then_fail(source, target):
            copy_output(source, target)
            raise OSError("fixture interrupted first sync")

        with patch("output_build._copy_output", side_effect=copy_then_fail):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.build({"old-partial.html": b"partially deployed"})
        pending = self.data / "output-pending.json"
        before = pending.read_bytes()
        self.assertFalse(output_build.has_manifest(self.data))
        self.build({"index.html": b"current source"}, baseline_only=True)
        self.assertEqual(pending.read_bytes(), before)
        self.assertTrue((self.public / "old-partial.html").is_file())
        self.assertFalse((self.public / "index.html").exists())
        result = self.build({"index.html": b"current source"})
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse((self.public / "old-partial.html").exists())

    def test_first_build_tracks_only_generated_outputs(self):
        self.write_public("legacy/image.jpg", b"keep")
        self.write_public("old/index.html", b"unknown")
        self.assertFalse(output_build.has_manifest(self.data))
        result = self.build({"index.html": b"home"})
        self.assertTrue(output_build.has_manifest(self.data))
        self.assertEqual(result["generated_count"], 1)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual((self.public / "legacy/image.jpg").read_bytes(), b"keep")
        self.assertEqual((self.public / "old/index.html").read_bytes(), b"unknown")
        state = json.loads(output_build.manifest_path(self.data).read_text())
        self.assertEqual(set(state["files"]), {"index.html"})
        self.assertEqual(state["files"]["index.html"], hashlib.sha256(b"home").hexdigest())

    def test_stale_pages_feeds_and_media_derivatives_removed(self):
        generated = {
            "index.html": b"home",
            "posts/example/index.html": b"post",
            "posts/example/image.png": b"image",
            "posts/example/image_small.png": b"derivative",
            "tags/example/index.html": b"tag",
            "tags/example/index.xml": b"rss",
            "page/2/index.html": b"page",
        }
        self.write_public("legacy/image.jpg", b"unmanaged")
        self.build(generated)
        result = self.build({"index.html": b"updated"})
        self.assertEqual(result["removed_count"], 6)
        self.assertEqual((self.public / "index.html").read_bytes(), b"updated")
        self.assertFalse((self.public / "posts").exists())
        self.assertFalse((self.public / "tags").exists())
        self.assertFalse((self.public / "page").exists())
        self.assertEqual((self.public / "legacy/image.jpg").read_bytes(), b"unmanaged")

    def test_externally_modified_obsolete_output_is_preserved(self):
        self.build({"index.html": b"home", "posts/old/index.html": b"post"})
        self.write_public("posts/old/index.html", b"external change")
        result = self.build({"index.html": b"new"})
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["preserved_modified"], ["posts/old/index.html"])
        self.assertTrue(result["warnings"])
        self.assertEqual((self.public / "posts/old/index.html").read_bytes(), b"external change")
        self.assertEqual(set(json.loads(output_build.manifest_path(self.data).read_text())["files"]),
                         {"index.html"})

    def test_failed_hugo_keeps_public_and_manifest_unchanged(self):
        self.build({"index.html": b"home", "old.html": b"old"})
        before = output_build.manifest_path(self.data).read_bytes()
        completed = subprocess.CompletedProcess([], 1, "", "template failure")
        with patch("output_build.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "template failure"):
                output_build.build_site(self.site, self.public, self.data, "hugo", 5)
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")
        self.assertEqual((self.public / "old.html").read_bytes(), b"old")
        self.assertEqual(output_build.manifest_path(self.data).read_bytes(), before)
        self.assertFalse((self.data / "output-pending.json").exists())
        self.assertFalse(list(self.data.glob("hugo-output-*")))

    def test_hugo_timeout_keeps_public_unchanged(self):
        self.write_public("index.html", b"home")
        with patch("output_build.subprocess.run", side_effect=subprocess.TimeoutExpired("hugo", 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                output_build.build_site(self.site, self.public, self.data, "hugo", 5)
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")
        self.assertFalse(output_build.has_manifest(self.data))

    def test_sync_retry_retains_ownership_of_new_partial_outputs(self):
        self.build({"index.html": b"old", "stale.html": b"old"})
        original = output_build._copy_output
        calls = []

        def fail_second(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("disk full")
            original(source, target)

        with patch("output_build._copy_output", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.build({"added.html": b"partial", "index.html": b"new"})
        self.assertTrue((self.data / "output-pending.json").exists())
        self.assertEqual((self.public / "added.html").read_bytes(), b"partial")
        self.assertFalse((self.public / "stale.html").exists())
        result = self.build({"index.html": b"recovered"})
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse((self.public / "added.html").exists())
        self.assertEqual((self.public / "index.html").read_bytes(), b"recovered")
        self.assertFalse((self.data / "output-pending.json").exists())

    def test_manifest_write_failure_is_recoverable(self):
        self.build({"index.html": b"old"})
        original = output_build._atomic_json

        def fail_manifest(path, value):
            if path == output_build.manifest_path(self.data):
                raise OSError("manifest unavailable")
            original(path, value)

        with patch("output_build._atomic_json", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "manifest unavailable"):
                self.build({"new.html": b"new"})
        self.assertEqual((self.public / "new.html").read_bytes(), b"new")
        self.build({"index.html": b"restored"})
        self.assertFalse((self.public / "new.html").exists())

    def test_public_symlink_is_rejected_before_any_output_changes(self):
        self.write_public("index.html", b"home")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.public / "posts").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build({"index.html": b"new", "posts/example/index.html": b"escape"})
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")
        self.assertFalse(list(outside.iterdir()))

    def test_generated_symlink_is_rejected(self):
        self.write_public("index.html", b"home")
        outside = Path(self.temp.name) / "secret"
        outside.write_bytes(b"not an output")

        def run(command, **kwargs):
            destination = Path(command[command.index("--destination") + 1])
            (destination / "index.html").symlink_to(outside)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("output_build.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "symlink"):
                output_build.build_site(self.site, self.public, self.data, "hugo", 5)
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")

    def test_manifest_path_escape_is_rejected(self):
        self.build({"index.html": b"home"})
        path = output_build.manifest_path(self.data)
        state = json.loads(path.read_text())
        state["files"]["../outside.txt"] = hashlib.sha256(b"outside").hexdigest()
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "output path"):
            self.build({"index.html": b"new"})
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")

    def test_manifest_for_another_public_root_is_rejected(self):
        self.build({"index.html": b"home"})
        path = output_build.manifest_path(self.data)
        state = json.loads(path.read_text())
        state["public_root"] = str(self.site / "elsewhere")
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "different site"):
            self.build({"index.html": b"new"})
        self.assertEqual((self.public / "index.html").read_bytes(), b"home")

    def test_unmanaged_file_directory_conflicts_are_rejected(self):
        for old, new in (("asset", "asset/image.png"), ("asset/image.png", "asset")):
            with self.subTest(old=old, new=new):
                self.write_public(old, b"unmanaged")
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    self.build({"index.html": b"new", new: b"generated"})
                self.assertEqual((self.public / old).read_bytes(), b"unmanaged")
                self.assertFalse((self.public / "index.html").exists())
                (self.public / old).unlink()
                if (self.public / "asset").is_dir():
                    (self.public / "asset").rmdir()

    def test_owned_file_directory_transitions_are_supported(self):
        self.build({"asset": b"file"})
        result = self.build({"asset/image.png": b"image"})
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual((self.public / "asset/image.png").read_bytes(), b"image")
        result = self.build({"asset": b"file again"})
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual((self.public / "asset").read_bytes(), b"file again")

    def test_private_data_inside_public_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "private build data"):
            output_build.build_site(self.site, self.public, self.public / "private", "hugo", 5)

    def test_private_data_cannot_be_copied_from_site_static_sources(self):
        for data in (self.site / "static/private", self.site, self.site.parent):
            with self.subTest(data=data), patch("output_build.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "separate from the site source tree"):
                    output_build.build_site(self.site, self.public, data, "hugo", 5)
                run.assert_not_called()
        self.assertFalse((self.site / "static").exists())

    @unittest.skipUnless(os.environ.get("HUGO_TEST_BIN") or shutil.which("hugo"),
                         "set HUGO_TEST_BIN to run the isolated real-Hugo check")
    def test_real_hugo_cleans_removed_bundle_and_taxonomy(self):
        hugo = os.environ.get("HUGO_TEST_BIN") or shutil.which("hugo")
        fixtures = {
            "hugo.toml": 'baseURL = "https://example.invalid/"\ntitle = "Test site"\n',
            "layouts/_default/list.html": "{{ range .Pages }}{{ .Title }}{{ end }}",
            "layouts/_default/single.html": "{{ .Content }}{{ range .Resources }}{{ .RelPermalink }}{{ end }}",
            "content/posts/example/index.md": '---\ntitle: Example\ntags: [sample]\n---\nExample body.\n',
            "content/posts/example/attachment.txt": "bundle resource",
        }
        for relative, content in fixtures.items():
            target = self.site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.write_public("migrated/attachment.txt", b"unmanaged attachment")
        output_build.build_site(self.site, self.public, self.data, hugo, 30)
        self.assertTrue((self.public / "posts/example/index.html").is_file())
        self.assertTrue((self.public / "posts/example/attachment.txt").is_file())
        self.assertTrue((self.public / "tags/sample/index.html").is_file())
        shutil.rmtree(self.site / "content/posts/example")
        output_build.build_site(self.site, self.public, self.data, hugo, 30)
        self.assertFalse((self.public / "posts/example").exists())
        self.assertFalse((self.public / "tags/sample").exists())
        self.assertEqual((self.public / "migrated/attachment.txt").read_bytes(), b"unmanaged attachment")


if __name__ == "__main__":
    unittest.main()
