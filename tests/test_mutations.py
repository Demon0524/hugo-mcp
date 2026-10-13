import base64
import concurrent.futures
import http.client
import http.server
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SERVER_FILE = Path(__file__).resolve().parents[1] / "server.py"
BUILD_RESULT = {"returncode": 0, "output": "test build"}


class MutationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        self.data = self.root / "data"
        self.server = self.load_server()

    def load_server(self):
        environment = {
            "HUGO_SITE_ROOT": str(self.site),
            "HUGO_POSTS_DIR": "content/posts",
            "HUGO_PUBLIC_DIR": "public",
            "HUGO_MCP_DATA_ROOT": str(self.data),
            "HUGO_MCP_TOKEN_FILE": str(self.data / "token"),
            "HUGO_BIN": "/bin/true",
        }
        spec = importlib.util.spec_from_file_location("mutation_test_server", SERVER_FILE)
        server = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, environment):
            spec.loader.exec_module(server)
        server.ensure_roots()
        return server

    def seed_post(self, slug="sample-post", draft=True, bundle=True):
        path = self.server.bundle_post_path(slug) if bundle else self.server.post_path(slug)
        self.server.atomic_write(
            path,
            self.server.render_markdown(
                {"title": slug, "slug": slug, "draft": draft}, "Fixture article body."
            ),
        )
        return path

    def test_overlapping_roots_are_rejected_before_directory_creation_or_build(self):
        site = self.root / "root-validation" / "site"
        posts = site / "content" / "posts"
        public = site / "public"
        data = self.root / "root-validation" / "data"
        cases = (
            {"PUBLIC_ROOT": posts},
            {"PUBLIC_ROOT": posts / "public"},
            {"POSTS_ROOT": public / "posts"},
            {"DATA_ROOT": public / "data"},
            {"DATA_ROOT": posts / "data"},
            {"DATA_ROOT": data, "PUBLIC_ROOT": data / "public"},
            {"DATA_ROOT": data, "POSTS_ROOT": data / "posts"},
            {"DATA_ROOT": site / "static" / "private"},
            {"DATA_ROOT": site / ".private-data"},
            {"TOKEN_FILE": site / "static" / "token"},
            {"TOKEN_FILE": site},
            {"POSTS_ROOT": site},
            {"PUBLIC_ROOT": site},
            {"DATA_ROOT": site},
        )
        for changes in cases:
            roots = {"SITE_ROOT": site, "POSTS_ROOT": posts, "PUBLIC_ROOT": public, "DATA_ROOT": data}
            with self.subTest(changes=changes), mock.patch.multiple(
                self.server, **{**roots, **changes}
            ), mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(
                self.server.output_build, "build_site"
            ) as build:
                with self.assertRaises(ValueError):
                    self.server.ensure_roots()
                with self.assertRaises(ValueError):
                    self.server.build_site()
                mkdir.assert_not_called()
                build.assert_not_called()

    def arguments(self, path, request_id="mutation-001"):
        return {
            "slug": path.parent.name if path.name == "index.md" else path.stem,
            "expected_revision": self.server.sha256_file(path),
            "request_id": request_id,
        }

    def batch_item(self, path, action):
        item = self.arguments(path)
        item.pop("request_id")
        return {"action": action, **item}

    def files_under(self, root):
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def http_call(self, payload):
        self.server.atomic_write(self.server.TOKEN_FILE, "unit-test-token")
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        try:
            connection.request(
                "POST", "/mcp", body=json.dumps(payload),
                headers={"Authorization": "Bearer unit-test-token", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

    def fail_build(self, tool, arguments):
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", side_effect=RuntimeError("fixture build failed")
        ) as build:
            with self.assertRaisesRegex(RuntimeError, "fixture build failed"):
                self.server.call_tool(tool, arguments)
        self.assertEqual(build.call_count, 1)

    def retry_after_restart(self, tool, arguments):
        source_before = self.files_under(self.site / "content")
        trash_before = self.files_under(self.server.TRASH_ROOT)
        self.server = self.load_server()
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", return_value=BUILD_RESULT
        ) as build:
            result = self.server.call_tool(tool, arguments)
            replay = self.server.call_tool(tool, arguments)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(replay, result)
        self.assertEqual(self.files_under(self.site / "content"), source_before)
        self.assertEqual(self.files_under(self.server.TRASH_ROOT), trash_before)
        return result

    def test_failed_publish_resumes_after_restart_without_rewriting_source(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        self.fail_build("hugo_publish_post", arguments)
        self.assertFalse(self.server.parse_markdown(path)[0]["draft"])
        result = self.retry_after_restart("hugo_publish_post", arguments)
        self.assertEqual(result["action"], "published")
        self.assertEqual(result["post"]["revision"], self.server.sha256_file(path))

    def test_interruption_after_source_write_resumes_from_persisted_intent(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        atomic_write = self.server.atomic_write_bytes

        def write_then_interrupt(target, content):
            atomic_write(target, content)
            if Path(target) == path:
                raise OSError("fixture interruption after source write")

        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "atomic_write_bytes", side_effect=write_then_interrupt
        ), mock.patch.object(self.server, "build_site") as build:
            with self.assertRaises((OSError, RuntimeError)):
                self.server.call_tool("hugo_publish_post", arguments)
        build.assert_not_called()
        self.assertFalse(self.server.parse_markdown(path)[0]["draft"])
        result = self.retry_after_restart("hugo_publish_post", arguments)
        self.assertEqual(result["action"], "published")

    def test_failed_unpublish_preserves_public_output_until_success(self):
        path = self.seed_post(draft=False)
        public = self.server.PUBLIC_ROOT / "posts" / "sample-post" / "index.html"
        self.server.atomic_write(public, "Previous successful build")
        arguments = self.arguments(path)
        self.fail_build("hugo_unpublish_post", arguments)
        self.assertTrue(self.server.parse_markdown(path)[0]["draft"])
        self.assertEqual(public.read_text(), "Previous successful build")
        result = self.retry_after_restart("hugo_unpublish_post", arguments)
        self.assertEqual(result["action"], "unpublished")

    def test_failed_delete_resumes_with_same_trash_and_old_revision(self):
        path = self.seed_post(draft=False)
        self.server.atomic_write_bytes(path.parent / "photo.png", b"fixture attachment")
        original = self.files_under(path.parent)
        arguments = self.arguments(path)
        self.fail_build("hugo_delete_post", arguments)
        self.assertFalse(path.exists())
        result = self.retry_after_restart("hugo_delete_post", arguments)
        self.assertEqual(result["action"], "soft_deleted")
        self.assertEqual(self.files_under(Path(result["trash_path"])), original)

    def test_failed_media_delete_resumes_without_moving_media_twice(self):
        path = self.seed_post()
        media = path.parent / "photo.png"
        self.server.atomic_write_bytes(media, b"fixture image")
        arguments = {
            "post_slug": "sample-post",
            "filename": "photo.png",
            "expected_revision": self.server.sha256_file(media),
            "request_id": "delete-media-001",
        }
        self.fail_build("hugo_delete_media", arguments)
        self.assertFalse(media.exists())
        result = self.retry_after_restart("hugo_delete_media", arguments)
        self.assertEqual(result["action"], "soft_deleted")
        self.assertEqual(Path(result["trash_path"]).read_bytes(), b"fixture image")

    def test_failed_migration_resumes_after_flat_file_has_moved(self):
        path = self.seed_post(bundle=False)
        original = path.read_bytes()
        arguments = self.arguments(path)
        self.fail_build("hugo_migrate_post_bundle", arguments)
        self.assertFalse(path.exists())
        result = self.retry_after_restart("hugo_migrate_post_bundle", arguments)
        self.assertEqual(result["action"], "migrated_to_bundle")
        self.assertEqual(self.server.bundle_post_path("sample-post").read_bytes(), original)

    def test_interruption_after_source_move_resumes_from_persisted_intent(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        move = self.server.shutil.move

        def move_then_interrupt(source, target):
            move(source, target)
            raise OSError("fixture interruption after source move")

        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server.shutil, "move", side_effect=move_then_interrupt
        ), mock.patch.object(self.server, "build_site") as build:
            with self.assertRaises(RuntimeError):
                self.server.call_tool("hugo_delete_post", arguments)
        build.assert_not_called()
        self.assertFalse(path.exists())
        result = self.retry_after_restart("hugo_delete_post", arguments)
        self.assertEqual(result["action"], "soft_deleted")

    def test_pending_retry_preserves_external_source_edit_and_does_not_build(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        self.fail_build("hugo_publish_post", arguments)
        self.server.atomic_write(path, "Fixture external edit after failed build.\n")
        before = path.read_bytes()
        self.server = self.load_server()
        with mock.patch.object(self.server, "build_site") as build:
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                self.server.call_tool("hugo_publish_post", arguments)
        build.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_retry_after_audit_failure_does_not_repeat_successful_build(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", return_value=BUILD_RESULT
        ) as build, mock.patch.object(self.server, "audit", side_effect=OSError("fixture audit failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture audit failure"):
                self.server.call_tool("hugo_publish_post", arguments)
        self.assertEqual(build.call_count, 1)
        source_after_build = path.read_bytes()
        self.server = self.load_server()
        with mock.patch.object(self.server, "build_site") as build:
            result = self.server.call_tool("hugo_publish_post", arguments)
        build.assert_not_called()
        self.assertEqual(result["build"], BUILD_RESULT)
        self.assertEqual(path.read_bytes(), source_after_build)

    def test_baseline_failure_does_not_mutate_source_or_create_pending_request(self):
        path = self.seed_post()
        before = self.files_under(self.root)
        with mock.patch.object(
            self.server, "ensure_output_baseline", side_effect=RuntimeError("fixture baseline failure")
        ), self.assertRaisesRegex(RuntimeError, "fixture baseline failure"):
            self.server.call_tool("hugo_delete_post", self.arguments(path))
        self.assertEqual(self.files_under(self.root), before)

    def test_pending_operation_blocks_other_writes_and_build_but_allows_reads(self):
        path = self.seed_post()
        self.fail_build("hugo_publish_post", self.arguments(path))
        for tool, arguments in (
            ("hugo_create_draft", {"title": "Unrelated", "request_id": "unrelated-001"}),
            ("hugo_build", {}),
        ):
            with self.subTest(tool=tool), self.assertRaises(RuntimeError):
                self.server.call_tool(tool, arguments)
        self.assertEqual(self.server.call_tool("hugo_list_posts", {})["total"], 1)
        self.assertEqual(
            self.server.call_tool("hugo_get_post", {"slug": "sample-post"})["post"]["slug"],
            "sample-post",
        )

    def test_pending_request_id_cannot_be_reused_with_changed_arguments(self):
        path = self.seed_post()
        arguments = self.arguments(path)
        self.fail_build("hugo_publish_post", arguments)
        with self.assertRaises(ValueError):
            self.server.call_tool("hugo_publish_post", {**arguments, "slug": "different-post"})
        with self.assertRaises(ValueError):
            self.server.call_tool("hugo_unpublish_post", arguments)

    def test_http_build_failure_tells_client_it_is_retryable(self):
        path = self.seed_post()
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", side_effect=RuntimeError("fixture build failed")
        ):
            status, _, result = self.http_call({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "hugo_publish_post", "arguments": self.arguments(path)},
            })
        self.assertEqual(status, 409)
        self.assertTrue(result["error"]["retryable"])
        self.assertEqual(result["error"]["details"]["request_id"], "mutation-001")

    def test_http_rate_limit_is_retryable_with_retry_after(self):
        with mock.patch.object(self.server.Handler, "_rate_limited", return_value=True):
            status, headers, result = self.http_call({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 429)
        self.assertTrue(result["error"]["retryable"])
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_completed_request_id_is_bound_to_tool_and_arguments(self):
        arguments = {"title": "Sample", "slug": "sample-post", "request_id": "create-001"}
        created = self.server.call_tool("hugo_create_draft", arguments)
        self.assertEqual(self.server.call_tool("hugo_create_draft", dict(reversed(list(arguments.items())))), created)
        with self.assertRaises(ValueError):
            self.server.call_tool("hugo_create_draft", {**arguments, "title": "Changed"})
        with self.assertRaises(ValueError):
            self.server.call_tool("hugo_delete_post", arguments)

    def test_legacy_plain_idempotency_result_is_replayed(self):
        expected = {"action": "created", "post": {"slug": "legacy-post"}}
        self.server.remember_request("legacy-request", expected)
        actual = self.server.call_tool(
            "hugo_create_draft", {"title": "Legacy", "request_id": "legacy-request"}
        )
        self.assertEqual(actual, expected)
        self.assertEqual(self.server.call_tool("hugo_list_posts", {})["total"], 0)

    def test_revision_conflict_has_no_side_effects_or_build(self):
        path = self.seed_post()
        before = self.files_under(self.root)
        arguments = {**self.arguments(path), "expected_revision": "outdated"}
        with mock.patch.object(self.server, "ensure_output_baseline") as baseline, mock.patch.object(
            self.server, "build_site"
        ) as build, self.assertRaisesRegex(ValueError, "revision mismatch"):
            self.server.call_tool("hugo_delete_post", arguments)
        baseline.assert_not_called()
        build.assert_not_called()
        self.assertEqual(self.files_under(self.root), before)

    def duplicate_requests(self, tool, arguments):
        # Hold the real lock until every request reaches it, exercising the
        # pre-lock lookup race without timing-sensitive sleeps.
        underlying = self.server.MUTATION_LOCK
        arrived = threading.Event()
        counter_lock = threading.Lock()
        count = 0

        class ObservedLock:
            def __enter__(self):
                nonlocal count
                with counter_lock:
                    count += 1
                    if count == 4:
                        arrived.set()
                underlying.acquire()
                return self

            def __exit__(self, *args):
                underlying.release()

        with mock.patch.object(self.server, "MUTATION_LOCK", ObservedLock()):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                underlying.acquire()
                try:
                    futures = [executor.submit(self.server.call_tool, tool, arguments) for _ in range(4)]
                    self.assertTrue(arrived.wait(timeout=5), "duplicate requests did not reach the mutation lock")
                finally:
                    underlying.release()
                results = [future.result(timeout=5) for future in futures]
        self.assertTrue(all(result == results[0] for result in results))
        return results[0]

    def test_concurrent_duplicate_create_returns_one_result(self):
        self.duplicate_requests(
            "hugo_create_draft",
            {"title": "Concurrent", "slug": "concurrent", "request_id": "concurrent-create"},
        )
        self.assertEqual(self.server.call_tool("hugo_list_posts", {})["total"], 1)

    def test_concurrent_duplicate_upload_without_filename_creates_one_file(self):
        self.seed_post()
        uploaded = self.duplicate_requests(
            "hugo_upload_media",
            {
                "post_slug": "sample-post",
                "mime_type": "image/png",
                "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii"),
                "request_id": "concurrent-upload",
            },
        )
        media = self.server.call_tool("hugo_list_media", {"post_slug": "sample-post"})
        self.assertEqual(media["total"], 1)
        self.assertEqual(media["media"][0]["filename"], uploaded["media"]["filename"])

    def test_batch_dry_run_defaults_true_and_does_not_write_or_build(self):
        paths = [self.seed_post("one"), self.seed_post("two", draft=False)]
        arguments = {"items": [self.batch_item(paths[0], "publish"), self.batch_item(paths[1], "delete")]}
        before = self.files_under(self.root)
        with mock.patch.object(self.server, "ensure_output_baseline") as baseline, mock.patch.object(
            self.server, "build_site"
        ) as build:
            result = self.server.call_tool("hugo_batch_posts", arguments)
        self.assertEqual(result["action"], "planned")
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["build_required"])
        baseline.assert_not_called()
        build.assert_not_called()
        self.assertEqual(self.files_under(self.root), before)

    def test_batch_checks_every_revision_before_any_side_effect(self):
        first = self.seed_post("first")
        second = self.seed_post("second")
        items = [self.batch_item(first, "delete"), self.batch_item(second, "publish")]
        items[1]["expected_revision"] = "outdated"
        before = self.files_under(self.root)
        with mock.patch.object(self.server, "ensure_output_baseline") as baseline, mock.patch.object(
            self.server, "build_site"
        ) as build, self.assertRaisesRegex(ValueError, "revision mismatch"):
            self.server.call_tool(
                "hugo_batch_posts", {"items": items, "dry_run": False, "request_id": "batch-invalid"}
            )
        baseline.assert_not_called()
        build.assert_not_called()
        self.assertEqual(self.files_under(self.root), before)

    def test_batch_rejects_invalid_items_without_writes(self):
        path = self.seed_post()
        item = self.batch_item(path, "delete")
        invalid_items = (
            [],
            [item] * 51,
            [item, {**item, "action": "publish"}],
            [{**item, "action": "erase-all"}],
            [{**item, "action": []}],
            [{**item, "action": {}}],
            [{"action": "delete", "slug": "sample-post"}],
            [{**item, "slug": "*"}],
            [{**item, "request_id": "per-item-id"}],
            [{**item, "unexpected": True}],
            ["sample-post"],
        )
        before = self.files_under(self.root)
        for items in invalid_items:
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.server.call_tool("hugo_batch_posts", {"items": items})
        self.assertEqual(self.files_under(self.root), before)

    def test_batch_rejects_same_post_selected_by_filename_and_frontmatter_slug(self):
        path = self.seed_post("physical-name")
        self.server.atomic_write(path, self.server.render_markdown(
            {"title": "Fixture", "slug": "canonical-name", "draft": True}, "Fixture body."
        ))
        first = self.batch_item(path, "publish")
        before = self.files_under(self.root)
        with self.assertRaisesRegex(ValueError, "only once"):
            self.server.call_tool("hugo_batch_posts", {
                "items": [first, {**first, "slug": "canonical-name", "action": "delete"}],
            })
        self.assertEqual(self.files_under(self.root), before)

    def test_batch_resolves_distinct_frontmatter_aliases_without_mixing_results(self):
        publish = self.seed_post("physical-publish")
        self.server.atomic_write(publish, self.server.render_markdown(
            {"title": "Fixture", "slug": "canonical-publish", "draft": True}, "Fixture body."
        ))
        delete = self.seed_post("physical-delete")
        self.server.atomic_write(delete, self.server.render_markdown(
            {"title": "Fixture", "slug": "canonical-delete", "draft": True}, "Fixture body."
        ))
        arguments = {
            "items": [
                {**self.batch_item(publish, "publish"), "slug": "canonical-publish"},
                self.batch_item(delete, "delete"),
            ],
            "dry_run": False,
            "request_id": "batch-aliases",
        }
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", return_value=BUILD_RESULT
        ) as build:
            result = self.server.call_tool("hugo_batch_posts", arguments)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(result["items"][0]["post"]["slug"], "canonical-publish")
        self.assertFalse(self.server.parse_markdown(publish)[0]["draft"])
        self.assertEqual(result["items"][1]["slug"], "physical-delete")
        self.assertFalse(delete.exists())

    def test_batch_rejects_non_boolean_dry_run_without_writes(self):
        path = self.seed_post()
        before = self.files_under(self.root)
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(dry_run=value), self.assertRaises(ValueError):
                self.server.call_tool("hugo_batch_posts", {
                    "items": [self.batch_item(path, "delete")],
                    "dry_run": value,
                    "request_id": "invalid-dry-run",
                })
        self.assertEqual(self.files_under(self.root), before)

    def test_batch_execution_requires_request_id(self):
        path = self.seed_post()
        with self.assertRaises(ValueError):
            self.server.call_tool(
                "hugo_batch_posts", {"items": [self.batch_item(path, "delete")], "dry_run": False}
            )
        self.assertTrue(path.exists())

    def test_batch_executes_mixed_actions_with_one_build(self):
        publish = self.seed_post("publish-me")
        unpublish = self.seed_post("unpublish-me", draft=False)
        delete = self.seed_post("delete-me")
        arguments = {
            "items": [
                self.batch_item(publish, "publish"),
                self.batch_item(unpublish, "unpublish"),
                self.batch_item(delete, "delete"),
            ],
            "dry_run": False,
            "request_id": "batch-mixed",
        }
        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "build_site", return_value=BUILD_RESULT
        ) as build:
            result = self.server.call_tool("hugo_batch_posts", arguments)
            replay = self.server.call_tool("hugo_batch_posts", arguments)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(result, replay)
        self.assertEqual(result["action"], "batch_completed")
        self.assertEqual([item["action"] for item in result["items"]], ["published", "unpublished", "soft_deleted"])
        self.assertEqual(result["build"], BUILD_RESULT)
        self.assertFalse(self.server.parse_markdown(publish)[0]["draft"])
        self.assertTrue(self.server.parse_markdown(unpublish)[0]["draft"])
        self.assertFalse(delete.exists())

    def test_failed_batch_resumes_after_restart_without_repeating_items(self):
        publish = self.seed_post("publish-me")
        delete = self.seed_post("delete-me")
        arguments = {
            "items": [self.batch_item(publish, "publish"), self.batch_item(delete, "delete")],
            "dry_run": False,
            "request_id": "batch-retry",
        }
        self.fail_build("hugo_batch_posts", arguments)
        self.assertFalse(self.server.parse_markdown(publish)[0]["draft"])
        self.assertFalse(delete.exists())
        result = self.retry_after_restart("hugo_batch_posts", arguments)
        self.assertEqual(result["action"], "batch_completed")
        self.assertEqual(len(result["items"]), 2)

    def test_partial_batch_resumes_remaining_items_without_rewriting_completed_source(self):
        publish = self.seed_post("publish-me")
        delete = self.seed_post("delete-me")
        arguments = {
            "items": [self.batch_item(publish, "publish"), self.batch_item(delete, "delete")],
            "dry_run": False,
            "request_id": "batch-interrupted",
        }
        atomic_write = self.server.atomic_write_bytes

        def write_then_interrupt(target, content):
            atomic_write(target, content)
            if Path(target) == publish:
                raise OSError("fixture interruption after first item")

        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "atomic_write_bytes", side_effect=write_then_interrupt
        ), mock.patch.object(self.server, "build_site") as build:
            with self.assertRaises((OSError, RuntimeError)):
                self.server.call_tool("hugo_batch_posts", arguments)
        build.assert_not_called()
        self.assertFalse(self.server.parse_markdown(publish)[0]["draft"])
        self.assertTrue(delete.exists())

        self.server = self.load_server()
        atomic_write = self.server.atomic_write_bytes

        def do_not_repeat_source_write(target, content):
            self.assertNotEqual(Path(target), publish, "completed source write was repeated")
            return atomic_write(target, content)

        with mock.patch.object(self.server, "ensure_output_baseline"), mock.patch.object(
            self.server, "atomic_write_bytes", side_effect=do_not_repeat_source_write
        ), mock.patch.object(self.server, "build_site", return_value=BUILD_RESULT) as build:
            result = self.server.call_tool("hugo_batch_posts", arguments)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(result["action"], "batch_completed")
        self.assertFalse(delete.exists())


if __name__ == "__main__":
    unittest.main()
