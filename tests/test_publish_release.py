"""Offline publication checks: never contact GitHub or change real Git history."""

import asyncio
import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from scripts import publish_release as release

TAG = "v0.1.0"
COMMIT = "a" * 40
TAG_OBJECT = "b" * 40
RELEASE_ID = 42
ASSET_ID = 73
INVENTORY = "releases?per_page=100"
ENDPOINT = f"releases/{RELEASE_ID}"


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.process = self.enterContext(
            mock.patch.object(
                release.subprocess,
                "run",
                side_effect=AssertionError("Real subprocess calls are forbidden in these tests"),
            )
        )
        self.async_process = self.enterContext(
            mock.patch.object(
                release.asyncio,
                "create_subprocess_exec",
                new=mock.AsyncMock(
                    side_effect=AssertionError("Real async subprocess calls are forbidden")
                ),
            )
        )


class PublicationTests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = release.ROOT / ".verification" / "publish-release-tests"
        cls.temp_root.resolve().relative_to(release.ROOT.resolve())
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.root = Path(directory.name).resolve()
        self.root.relative_to(self.temp_root.resolve())
        self.addCleanup(directory.cleanup)
        self.path = self.root / "dist" / f"astrbot_plugin_youtube_music-{TAG}.zip"
        self.path.parent.mkdir()
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("fixture.txt", date_time=(2026, 11, 18, 0, 0, 0)),
                b"Synthetic release fixture.\n",
            )
        self.raw = self.path.read_bytes()
        self.package = {
            "path": str(self.path),
            "version": "0.1.0",
            "source_commit": COMMIT,
            "bytes": len(self.raw),
            "sha256": hashlib.sha256(self.raw).hexdigest(),
        }
        self.history_check = self.enterContext(
            mock.patch.object(release, "check_history", side_effect=self.history_result)
        )
        self.package_check = self.enterContext(
            mock.patch.object(release, "check_built", side_effect=self.package_result)
        )
        self.git = self.enterContext(
            mock.patch.object(release, "_git_bytes", side_effect=self.git_bytes)
        )
        self.blob = self.enterContext(
            mock.patch.object(release, "_git_blob", side_effect=self.git_blob)
        )
        self.api = self.enterContext(
            mock.patch.object(release, "_api", side_effect=self.api_result)
        )
        self.gh = self.enterContext(mock.patch.object(release, "_gh", side_effect=self.gh_result))
        self.uploader = self.enterContext(
            mock.patch.object(release, "_upload_asset", side_effect=self.upload_result)
        )
        self.downloader = self.enterContext(
            mock.patch.object(release, "_download_asset", side_effect=self.download_result)
        )
        self.validator = self.enterContext(
            mock.patch.object(release, "validate", side_effect=self.validation_result)
        )
        self.reset_protocol()

    def reset_protocol(self):
        self.events = []
        self.operations = []
        self.release_id = RELEASE_ID
        self.asset_id = ASSET_ID
        self.history = {"head": COMMIT, "tag_object": TAG_OBJECT}
        self.status = b""
        self.origin = (release.REPOSITORY + ".git\n").encode()
        self.changelog = (
            b"# Changelog\n\n## [Unreleased]\n\n"
            b"## [0.1.0] - 2026-11-18\n\n### Fixed\n\n- Verified release change.\n\n"
            b"## [0.0.9] - 2026-11-17\n\n- Older change.\n\n"
            b"[0.1.0]: https://example.invalid/compare\n"
        )
        self.responses = {
            f"git/ref/tags/{TAG}": [
                {"object": {"type": "tag", "sha": TAG_OBJECT}},
                {"object": {"type": "tag", "sha": TAG_OBJECT}},
            ],
            "git/ref/heads/main": [
                {"object": {"type": "commit", "sha": COMMIT}},
                {"object": {"type": "commit", "sha": COMMIT}},
            ],
            INVENTORY: [[[], [{"tag_name": "v0.0.9"}]]],
            "releases": [{**self.record(draft=True), "assets": []}],
            ENDPOINT: [
                self.record(draft=True),
                self.record(draft=True),
                self.record(draft=False),
            ],
        }
        self.upload_response = copy.deepcopy(self.record(draft=True)["assets"][0])
        self.download_bytes = self.raw
        self.operation_errors = {}
        self.validation_error = None
        self.validated_path = None
        self.downloaded_path = None
        for mocked in (
            self.history_check,
            self.package_check,
            self.git,
            self.blob,
            self.api,
            self.gh,
            self.uploader,
            self.downloader,
            self.validator,
        ):
            mocked.reset_mock()

    def record(self, *, draft):
        return {
            "id": self.release_id,
            "tag_name": TAG,
            "target_commitish": COMMIT,
            "draft": draft,
            "prerelease": False,
            "html_url": (
                f"{release.REPOSITORY}/releases/untagged-{RELEASE_ID}"
                if draft
                else f"{release.REPOSITORY}/releases/tag/{TAG}"
            ),
            "assets": [
                {
                    "id": self.asset_id,
                    "name": self.path.name,
                    "size": self.package["bytes"],
                    "state": "uploaded",
                    "digest": f"sha256:{self.package['sha256']}",
                }
            ],
        }

    def history_result(self, root, *, tag):
        self.assertEqual(root, self.root)
        self.assertEqual(tag, TAG)
        self.events.append(("history", tag))
        return copy.deepcopy(self.history)

    def package_result(self, root, *, ref):
        self.assertEqual(root, self.root)
        self.assertEqual(ref, TAG)
        self.events.append(("package", ref))
        return copy.deepcopy(self.package)

    def git_bytes(self, root, *arguments):
        self.assertEqual(root, self.root)
        self.events.append(("git", arguments))
        if arguments == ("status", "--porcelain"):
            return self.status
        self.assertEqual(arguments, ("remote", "get-url", "--push", "origin"))
        return self.origin

    def git_blob(self, root, commit, name):
        self.assertEqual((root, commit, name), (self.root, COMMIT, "CHANGELOG.md"))
        self.events.append(("blob", name))
        return self.changelog

    def api_result(self, root, endpoint, *, paginated=False, method="GET", payload=None):
        self.assertEqual(root, self.root)
        self.assertEqual(paginated, endpoint == INVENTORY)
        self.assertEqual(method, "POST" if endpoint == "releases" else "GET")
        if endpoint == "releases":
            self.operations.append("create")
            self.assertEqual(
                set(payload),
                {
                    "tag_name",
                    "target_commitish",
                    "name",
                    "body",
                    "draft",
                    "prerelease",
                    "make_latest",
                },
            )
            self.assertEqual(payload["tag_name"], TAG)
            self.assertEqual(payload["target_commitish"], COMMIT)
            self.assertEqual(payload["name"], TAG)
            self.assertIs(payload["draft"], True)
            self.assertIs(payload["prerelease"], False)
            self.assertEqual(payload["make_latest"], "false")
            if "create" in self.operation_errors:
                raise self.operation_errors["create"]
        else:
            self.assertIsNone(payload)
        self.assertIn(endpoint, self.responses, "Only fixed-ID release reads are allowed")
        self.assertTrue(self.responses[endpoint], f"Unexpected repeated API call: {endpoint}")
        self.events.append(("api", "POST releases" if method == "POST" else endpoint))
        response = self.responses[endpoint].pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def gh_result(self, root, *arguments, input_text=None):
        self.assertEqual(root, self.root)
        self.assertEqual(
            arguments,
            (
                "api",
                "--hostname",
                "github.com",
                "--method",
                "PATCH",
                f"repos/{release.SLUG}/releases/{self.release_id}",
                "--input",
                "-",
            ),
            "No tag-based creation, download, publication, replacement or deletion is allowed",
        )
        self.assertEqual(json.loads(input_text), {"draft": False, "make_latest": "true"})
        self.operations.append("publish")
        self.events.append(("gh", "publish"))
        if "publish" in self.operation_errors:
            raise self.operation_errors["publish"]
        return ""

    def upload_result(self, root, release_id, package):
        self.assertEqual((root, release_id, package), (self.root, self.release_id, self.package))
        self.operations.append("upload")
        self.events.append(("upload", release_id))
        if "upload" in self.operation_errors:
            raise self.operation_errors["upload"]
        return copy.deepcopy(self.upload_response)

    def download_result(self, root, asset_id, target, expected_bytes):
        self.assertEqual(
            (root, asset_id, expected_bytes), (self.root, self.asset_id, self.package["bytes"])
        )
        self.assertEqual(target.name, self.path.name)
        target.resolve().relative_to(self.root / ".verification")
        self.assertTrue(target.parent.name.startswith("release-download-"))
        self.downloaded_path = target
        self.operations.append("download")
        self.events.append(("download", asset_id))
        if "download" in self.operation_errors:
            raise self.operation_errors["download"]
        with target.open("xb") as output:
            output.write(self.download_bytes)

    def validation_result(self, path, *, ref, root):
        self.assertEqual((ref, root), (TAG, self.root))
        self.assertEqual(path.name, self.path.name)
        path.resolve().relative_to(self.root / ".verification")
        self.validated_path = path
        self.events.append(("validate", ref))
        if self.validation_error is not None:
            raise self.validation_error
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in self.root.rglob("*")
        }

    def assert_no_publication(self):
        self.assertNotIn("publish", self.operations)
        self.process.assert_not_called()
        self.async_process.assert_not_called()

    def test_verified_draft_is_downloaded_then_published_by_fixed_id(self):
        result = release.publish(self.root, tag=TAG)
        self.assertEqual(
            result,
            {
                **self.package,
                "tag_object": TAG_OBJECT,
                "release_id": RELEASE_ID,
                "asset_id": ASSET_ID,
                "release_url": f"{release.REPOSITORY}/releases/tag/{TAG}",
                "publication": "published",
            },
        )
        self.assertEqual(
            self.events,
            [
                ("history", TAG),
                ("package", TAG),
                ("git", ("status", "--porcelain")),
                ("git", ("remote", "get-url", "--push", "origin")),
                ("api", f"git/ref/tags/{TAG}"),
                ("api", "git/ref/heads/main"),
                ("api", INVENTORY),
                ("blob", "CHANGELOG.md"),
                ("api", "POST releases"),
                ("upload", RELEASE_ID),
                ("api", ENDPOINT),
                ("download", ASSET_ID),
                ("validate", TAG),
                ("api", f"git/ref/tags/{TAG}"),
                ("api", "git/ref/heads/main"),
                ("api", ENDPOINT),
                ("gh", "publish"),
                ("api", ENDPOINT),
            ],
        )
        creation = next(call for call in self.api.call_args_list if call.args[1] == "releases")
        self.assertEqual(creation.args, (self.root, "releases"))
        self.assertEqual(creation.kwargs["method"], "POST")
        self.assertIn("- Verified release change.", creation.kwargs["payload"]["body"])
        self.uploader.assert_called_once_with(self.root, RELEASE_ID, self.package)
        self.downloader.assert_called_once_with(
            self.root, ASSET_ID, self.validated_path, self.package["bytes"]
        )
        publication = self.gh.call_args
        self.gh.assert_called_once()
        self.assertEqual(
            publication.args,
            (
                self.root,
                "api",
                "--hostname",
                "github.com",
                "--method",
                "PATCH",
                f"repos/{release.SLUG}/{ENDPOINT}",
                "--input",
                "-",
            ),
        )
        self.assertEqual(
            json.loads(publication.kwargs["input_text"]),
            {"draft": False, "make_latest": "true"},
        )
        self.assertFalse(self.validated_path.parent.exists())
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.process.assert_not_called()
        self.async_process.assert_not_called()

    def test_dry_run_is_read_only_and_does_not_create_a_download_directory(self):
        before = self.snapshot()
        result = release.publish(self.root, tag=TAG, dry_run=True)
        self.assertEqual(
            result, {**self.package, "tag_object": TAG_OBJECT, "publication": "dry-run"}
        )
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / ".verification").exists())
        self.gh.assert_not_called()
        self.uploader.assert_not_called()
        self.downloader.assert_not_called()
        self.validator.assert_not_called()
        self.assert_no_publication()

    def test_allowed_origin_forms_are_qualified_by_metadata(self):
        for origin in (
            release.REPOSITORY,
            release.REPOSITORY + ".git",
            f"git@github.com:{release.SLUG}.git",
            f"ssh://git@github.com/{release.SLUG}.git",
        ):
            with self.subTest(origin=origin):
                self.reset_protocol()
                self.origin = (origin + "\n").encode()
                release.publish(self.root, tag=TAG, dry_run=True)
                self.gh.assert_not_called()

    def test_mismatched_checked_out_source_stops_before_git_or_github_calls(self):
        self.history["head"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "checked-out source"):
            release.publish(self.root, tag=TAG)
        self.git.assert_not_called()
        self.api.assert_not_called()
        self.gh.assert_not_called()

    def test_dirty_worktree_or_wrong_origin_stops_before_github_calls(self):
        for status, origin, message in (
            (b" M README.md\n", self.origin, "clean source worktree"),
            (b"?? untracked.txt\n", self.origin, "clean source worktree"),
            (b"", b"https://github.com/another/repository.git\n", "origin repository"),
            (b"", (release.REPOSITORY + ".git.attacker\n").encode(), "origin repository"),
        ):
            with self.subTest(status=status, origin=origin):
                self.reset_protocol()
                self.status, self.origin = status, origin
                with self.assertRaisesRegex(ValueError, message):
                    release.publish(self.root, tag=TAG)
                self.api.assert_not_called()
                self.gh.assert_not_called()

    def test_remote_annotated_tag_and_main_must_match_before_creation(self):
        for endpoint, record in (
            (f"git/ref/tags/{TAG}", {"object": {"type": "commit", "sha": TAG_OBJECT}}),
            (f"git/ref/tags/{TAG}", {"object": {"type": "tag", "sha": COMMIT}}),
            ("git/ref/heads/main", {"object": {"type": "commit", "sha": "c" * 40}}),
            ("git/ref/heads/main", {"object": []}),
            ("git/ref/heads/main", []),
        ):
            with self.subTest(endpoint=endpoint, record=record):
                self.reset_protocol()
                self.responses[endpoint][0] = record
                with self.assertRaisesRegex(ValueError, "Remote .*verified source"):
                    release.publish(self.root, tag=TAG)
                self.gh.assert_not_called()
                self.assert_no_publication()

    def test_existing_release_in_any_inventory_page_is_never_overwritten(self):
        self.responses[INVENTORY][0] = [[], [{"tag_name": "v0.0.9"}], [self.record(draft=True)]]
        with self.assertRaisesRegex(ValueError, "already exists; nothing overwritten"):
            release.publish(self.root, tag=TAG)
        self.gh.assert_not_called()
        self.blob.assert_not_called()

    def test_malformed_paginated_inventory_fails_closed(self):
        for inventory in (
            None,
            {},
            [{"tag_name": TAG}],
            [[], {}],
            [[None]],
            [[{}]],
            [[{"tag_name": None}]],
            [[{"tag_name": 42}]],
            [[{"tag_name": ""}]],
            [[{"tag_name": " \r\n "}]],
        ):
            with self.subTest(inventory=inventory):
                self.reset_protocol()
                self.responses[INVENTORY][0] = inventory
                with self.assertRaisesRegex(ValueError, "existing release inventory"):
                    release.publish(self.root, tag=TAG)
                self.gh.assert_not_called()
                self.assert_no_publication()

    def test_creation_response_is_authoritative_when_release_inventory_stays_empty(self):
        self.responses[INVENTORY] = [[[]]]
        result = release.publish(self.root, tag=TAG)
        self.assertEqual((result["release_id"], result["asset_id"]), (RELEASE_ID, ASSET_ID))
        self.assertEqual([call.args[1] for call in self.api.call_args_list].count(INVENTORY), 1)
        self.assertEqual(self.operations, ["create", "upload", "download", "publish"])

    def test_created_and_uploaded_ids_are_propagated_without_tag_resolution(self):
        self.release_id, self.asset_id = RELEASE_ID + 7, ASSET_ID + 11
        endpoint = f"releases/{self.release_id}"
        self.responses["releases"] = [{**self.record(draft=True), "assets": []}]
        self.responses.pop(ENDPOINT)
        self.responses[endpoint] = [
            self.record(draft=True),
            self.record(draft=True),
            self.record(draft=False),
        ]
        self.upload_response = self.record(draft=True)["assets"][0]
        result = release.publish(self.root, tag=TAG)
        self.assertEqual(
            (result["release_id"], result["asset_id"]), (self.release_id, self.asset_id)
        )
        self.uploader.assert_called_once_with(self.root, self.release_id, self.package)
        self.downloader.assert_called_once_with(
            self.root, self.asset_id, self.validated_path, self.package["bytes"]
        )
        self.assertEqual([call.args[1] for call in self.api.call_args_list].count(endpoint), 3)

    def test_malformed_creation_response_stops_before_upload_and_never_relists(self):
        for record in (None, [], {}, {"id": RELEASE_ID}):
            with self.subTest(record=record):
                self.reset_protocol()
                self.responses["releases"][0] = record
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create"])
                self.uploader.assert_not_called()
                self.downloader.assert_not_called()
                self.assertEqual(
                    [call.args[1] for call in self.api.call_args_list].count(INVENTORY), 1
                )
                self.assert_no_publication()

    def test_created_draft_must_have_verified_identity_and_no_assets_before_upload(self):
        for key, value in (
            ("draft", False),
            ("id", True),
            ("id", 0),
            ("id", "42"),
            ("target_commitish", "main"),
            ("assets", None),
            ("assets", {}),
            ("assets", self.record(draft=True)["assets"]),
        ):
            with self.subTest(key=key, value=value):
                self.reset_protocol()
                self.responses["releases"][0][key] = value
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create"])
                self.uploader.assert_not_called()
                self.downloader.assert_not_called()
                self.assert_no_publication()

    def test_invalid_upload_response_never_downloads_or_publishes(self):
        original = self.upload_response
        for asset in (
            None,
            {},
            *(
                {**original, key: value}
                for key, value in (
                    ("id", True),
                    ("id", 0),
                    ("id", -1),
                    ("id", "73"),
                    ("name", "another.zip"),
                    ("size", original["size"] + 1),
                    ("state", "new"),
                    ("digest", None),
                )
            ),
        ):
            with self.subTest(asset=asset):
                self.reset_protocol()
                self.upload_response = asset
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload"])
                self.downloader.assert_not_called()
                self.assert_no_publication()

    def test_uploaded_asset_must_belong_to_the_created_draft_before_download(self):
        for key, value in (
            ("id", RELEASE_ID + 1),
            ("draft", False),
            ("tag_name", "v0.1.1"),
            ("assets", []),
            (
                "assets",
                [{**self.upload_response, "id": ASSET_ID + 1}],
            ),
            ("assets", [self.upload_response, self.upload_response]),
        ):
            with self.subTest(key=key, value=value):
                self.reset_protocol()
                self.responses[ENDPOINT][0][key] = value
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload"])
                self.downloader.assert_not_called()
                self.assert_no_publication()

    def test_download_must_pass_exact_tag_source_validation_and_match_local_sha256(self):
        for error, raw, message in (
            (ValueError("ZIP source bytes do not match tag"), self.raw, "source bytes"),
            (None, b"Different downloaded bytes", "differs from the local verified ZIP"),
        ):
            with self.subTest(message=message):
                self.reset_protocol()
                self.validation_error = error
                self.download_bytes = raw
                with self.assertRaisesRegex(ValueError, message):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload", "download"])
                self.assertFalse(self.validated_path.parent.exists())
                self.assert_no_publication()

    def test_remote_tag_or_main_drift_after_download_prevents_publication(self):
        for endpoint in (f"git/ref/tags/{TAG}", "git/ref/heads/main"):
            with self.subTest(endpoint=endpoint):
                self.reset_protocol()
                self.responses[endpoint][1]["object"]["sha"] = "c" * 40
                with self.assertRaisesRegex(ValueError, "Remote .*verified source"):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload", "download"])
                self.assert_no_publication()

    def test_changed_draft_id_state_or_asset_prevents_fixed_id_patch(self):
        for key, value in (
            ("id", RELEASE_ID + 1),
            ("draft", False),
            ("tag_name", "v0.1.1"),
            ("assets", []),
            ("assets", [{**self.upload_response, "id": ASSET_ID + 1}]),
        ):
            with self.subTest(key=key, value=value):
                self.reset_protocol()
                self.responses[ENDPOINT][1][key] = value
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload", "download"])
                self.assert_no_publication()

    def test_github_failures_stop_without_retry_replacement_or_cleanup(self):
        for operation, expected in (
            ("create", ["create"]),
            ("upload", ["create", "upload"]),
            ("download", ["create", "upload", "download"]),
            ("publish", ["create", "upload", "download", "publish"]),
        ):
            with self.subTest(operation=operation):
                self.reset_protocol()
                self.operation_errors[operation] = ValueError("GitHub operation failed")
                with self.assertRaisesRegex(ValueError, "GitHub operation failed"):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, expected)
                self.process.assert_not_called()
                self.async_process.assert_not_called()
                if self.downloaded_path is not None:
                    self.assertFalse(self.downloaded_path.parent.exists())

    def test_fixed_id_api_failures_stop_at_their_stage_without_mutation_retries(self):
        for index, operations in (
            (0, ["create", "upload"]),
            (1, ["create", "upload", "download"]),
            (2, ["create", "upload", "download", "publish"]),
        ):
            with self.subTest(index=index):
                self.reset_protocol()
                self.responses[ENDPOINT][index] = ValueError(
                    "Cannot read a valid GitHub API response"
                )
                with self.assertRaisesRegex(ValueError, "valid GitHub API response"):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, operations)
                self.process.assert_not_called()
                self.async_process.assert_not_called()

    def test_final_public_release_must_be_the_same_verified_draft(self):
        for key, value in (
            ("id", RELEASE_ID + 1),
            ("draft", True),
            ("html_url", f"{release.REPOSITORY}/releases/untagged-{RELEASE_ID}"),
            ("assets", []),
            ("assets", [{**self.upload_response, "id": ASSET_ID + 1}]),
        ):
            with self.subTest(key=key, value=value):
                self.reset_protocol()
                self.responses[ENDPOINT][2][key] = value
                with self.assertRaises(ValueError):
                    release.publish(self.root, tag=TAG)
                self.assertEqual(self.operations, ["create", "upload", "download", "publish"])
                self.process.assert_not_called()

    def test_release_identity_uses_strict_booleans_positive_ids_and_repository_urls(self):
        original = self.record(draft=True)
        for record in (
            None,
            [],
            {},
            *(
                {**original, key: value}
                for key, value in (
                    ("tag_name", "v0.1.1"),
                    ("target_commitish", "main"),
                    ("draft", 1),
                    ("prerelease", 0),
                    ("id", True),
                    ("id", 0),
                    ("id", -1),
                    ("id", "42"),
                    ("html_url", None),
                    ("html_url", "https://github.com/another/repository/releases/untagged-42"),
                )
            ),
        ):
            with self.subTest(record=record):
                with self.assertRaisesRegex(ValueError, "identity or publication state"):
                    release._verify_record(record, self.package, TAG, COMMIT, draft=True)
        self.assertEqual(
            release._verify_record(original, self.package, TAG, COMMIT, draft=True), RELEASE_ID
        )
        published = self.record(draft=False)
        self.assertEqual(
            release._verify_record(published, self.package, TAG, COMMIT, draft=False), RELEASE_ID
        )
        for suffix in ("/extra", "?query=1", "#fragment"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    release._verify_record(
                        {**published, "html_url": published["html_url"] + suffix},
                        self.package,
                        TAG,
                        COMMIT,
                        draft=False,
                    )

    def test_asset_verification_requires_one_uploaded_exact_size_and_digest_zip(self):
        record = self.record(draft=True)
        asset = record["assets"][0]
        for assets in (
            None,
            {},
            [],
            [asset, asset],
            [None],
            [{}],
            *(
                [{**asset, key: value}]
                for key, value in (
                    ("id", True),
                    ("id", 0),
                    ("id", -1),
                    ("id", "73"),
                    ("name", "another.zip"),
                    ("size", True),
                    ("size", str(asset["size"])),
                    ("size", asset["size"] + 1),
                    ("state", "new"),
                    ("digest", None),
                    ("digest", self.package["sha256"]),
                    ("digest", "sha256:" + "c" * 64),
                )
            ),
        ):
            with self.subTest(assets=assets):
                with self.assertRaises(ValueError):
                    release._verify_record(
                        {**record, "assets": assets}, self.package, TAG, COMMIT, draft=True
                    )

    def test_notes_use_committed_bytes_not_worktree_and_exclude_other_sections(self):
        (self.root / "CHANGELOG.md").write_text("Untrusted working tree.", encoding="utf-8")
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                self.changelog = self.changelog.decode().replace("\r\n", "\n")
                self.changelog = self.changelog.replace("\n", ending).encode()
                notes = release._notes(self.root, self.package, TAG, TAG_OBJECT)
                self.assertIn("- Verified release change.", notes)
                self.assertNotIn("Untrusted working tree", notes)
                self.assertNotIn("Older change", notes)
                self.assertNotIn("[0.1.0]:", notes)
                self.assertNotIn("\r", notes)
                for value in (TAG, COMMIT, TAG_OBJECT, self.path.name, self.package["sha256"]):
                    self.assertIn(f"`{value}`", notes)
                self.assertIn("Offline tests do not prove live YouTube", notes)
        self.blob.assert_called_with(self.root, COMMIT, "CHANGELOG.md")

    def test_missing_committed_changelog_section_prevents_creation(self):
        self.changelog = b"# Changelog\n\n## [0.0.9] - 2026-11-18\n\n- Older change.\n"
        with self.assertRaisesRegex(ValueError, "CHANGELOG section"):
            release.publish(self.root, tag=TAG)
        self.gh.assert_not_called()
        self.assert_no_publication()

    def test_nondirectory_verification_path_prevents_any_github_operation(self):
        (self.root / ".verification").write_text("Not a directory.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe local release verification directory"):
            release.publish(self.root, tag=TAG)
        self.api.assert_not_called()
        self.gh.assert_not_called()

    def test_symlink_junction_and_escaping_verification_paths_are_rejected_without_following(self):
        for name in ("is_symlink", "is_junction"):
            with self.subTest(name=name), mock.patch.object(Path, name, return_value=True):
                with self.assertRaisesRegex(ValueError, "Unsafe local"):
                    release._verification_directory(self.root)
        with mock.patch.object(Path, "resolve", return_value=self.root.parent):
            with self.assertRaisesRegex(ValueError, "Unsafe local"):
                release._verification_directory(self.root)
        for error in (OSError("Denied"), RuntimeError("Path loop")):
            with self.subTest(error=error), mock.patch.object(Path, "resolve", side_effect=error):
                with self.assertRaisesRegex(ValueError, "Unsafe local"):
                    release._verification_directory(self.root)

    def test_verification_directory_is_rechecked_after_draft_creation(self):
        with mock.patch.object(
            release,
            "_verification_directory",
            side_effect=[
                self.root / ".verification",
                ValueError("Unsafe local release verification directory"),
            ],
        ) as directory:
            with self.assertRaisesRegex(ValueError, "Unsafe local"):
                release.publish(self.root, tag=TAG)
        self.assertEqual(directory.call_count, 2)
        self.assertEqual(self.operations, ["create", "upload"])
        self.validator.assert_not_called()
        self.assert_no_publication()


class GitHubHelperTests(OfflineTests):
    def test_gh_is_fixed_host_bounded_noninteractive_utf8_and_shell_free(self):
        response = subprocess.CompletedProcess([], 0, stdout="Verified response", stderr="")
        with (
            mock.patch.dict(
                release.os.environ,
                {"GH_REPO": "another/repository", "GH_HOST": "attacker.invalid"},
            ),
            mock.patch.object(release.subprocess, "run", return_value=response) as run,
        ):
            result = release._gh(release.ROOT, "api", "--hostname", "github.com", input_text="{}")
        self.assertEqual(result, response.stdout)
        arguments, options = run.call_args
        self.assertEqual(arguments[0], ["gh", "api", "--hostname", "github.com"])
        self.assertEqual(options["cwd"], release.ROOT)
        self.assertEqual(options["input"], "{}")
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["timeout"], release.GH_TIMEOUT)
        self.assertTrue(options["text"])
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["check"])
        self.assertFalse(options["shell"])
        self.assertNotIn("GH_REPO", options["env"])
        self.assertEqual(options["env"]["GH_HOST"], "github.com")
        self.assertEqual(options["env"]["GH_PROMPT_DISABLED"], "1")

    def test_gh_errors_are_sanitized_without_token_or_subprocess_output_leaks(self):
        secret = "synthetic-private-token"
        for error in (
            OSError(secret),
            UnicodeError(secret),
            subprocess.CalledProcessError(1, ["gh"], output=secret, stderr=secret),
            subprocess.TimeoutExpired(["gh"], release.GH_TIMEOUT, output=secret),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(release.subprocess, "run", side_effect=error),
            ):
                with self.assertRaisesRegex(ValueError, "^GitHub operation failed: api$") as raised:
                    release._gh(release.ROOT, "api", "repos/example")
                self.assertNotIn(secret, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_api_qualifies_repository_host_and_requests_full_paginated_inventory(self):
        for paginated, response in ((False, '{"id":42}'), (True, '[[],[{"tag_name":"v0.1.0"}]]')):
            with (
                self.subTest(paginated=paginated),
                mock.patch.object(release, "_gh", return_value=response) as gh,
            ):
                result = release._api(release.ROOT, INVENTORY, paginated=paginated)
                self.assertEqual(result, json.loads(response))
                arguments = [
                    "api",
                    "--hostname",
                    "github.com",
                    "--header",
                    "Cache-Control: no-cache",
                ]
                if paginated:
                    arguments.extend(("--paginate", "--slurp"))
                arguments.append(f"repos/{release.SLUG}/{INVENTORY}")
                gh.assert_called_once_with(release.ROOT, *arguments, input_text=None)

    def test_api_writes_json_stdin_with_explicit_method_repository_and_no_cache(self):
        for method, payload in (
            ("POST", {"tag_name": TAG, "draft": True}),
            ("PATCH", {"draft": False, "make_latest": "true"}),
            ("PATCH", {}),
        ):
            with (
                self.subTest(method=method, payload=payload),
                mock.patch.object(release, "_gh", return_value='{"id":42}') as gh,
            ):
                self.assertEqual(
                    release._api(release.ROOT, ENDPOINT, method=method, payload=payload),
                    {"id": RELEASE_ID},
                )
                arguments, options = gh.call_args
                self.assertEqual(
                    arguments,
                    (
                        release.ROOT,
                        "api",
                        "--hostname",
                        "github.com",
                        "--header",
                        "Cache-Control: no-cache",
                        "--method",
                        method,
                        f"repos/{release.SLUG}/{ENDPOINT}",
                        "--input",
                        "-",
                    ),
                )
                self.assertEqual(json.loads(options["input_text"]), payload)
        self.process.assert_not_called()
        self.async_process.assert_not_called()

    def test_upload_uses_fixed_release_id_binary_file_and_encoded_filename(self):
        path = release.ROOT / ".verification" / "package &#+.zip"
        package = {"path": str(path)}
        response = {"id": ASSET_ID}
        with mock.patch.object(release, "_gh", return_value=json.dumps(response)) as gh:
            self.assertEqual(release._upload_asset(release.ROOT, RELEASE_ID, package), response)
        gh.assert_called_once_with(
            release.ROOT,
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST",
            f"https://uploads.github.com/repos/{release.SLUG}/releases/{RELEASE_ID}/assets?"
            + urlencode({"name": path.name}),
            "--header",
            "Content-Type: application/zip",
            "--input",
            str(path),
        )
        self.assertIn("name=package+%26%23%2B.zip", gh.call_args.args[6])
        self.process.assert_not_called()

    def test_upload_rejects_invalid_release_ids_before_any_gh_call(self):
        for release_id in (None, True, 0, -1, "42", 42.0):
            with self.subTest(release_id=release_id), mock.patch.object(release, "_gh") as gh:
                with self.assertRaisesRegex(ValueError, "Invalid GitHub release identity"):
                    release._upload_asset(
                        release.ROOT, release_id, {"path": str(release.ROOT / "fixture.zip")}
                    )
                gh.assert_not_called()

    def test_upload_malformed_json_or_operation_failure_never_retries(self):
        for response in ("{", '{"id":73,"id":74}', '{"size":Infinity}'):
            with (
                self.subTest(response=response),
                mock.patch.object(release, "_gh", return_value=response) as gh,
            ):
                with self.assertRaisesRegex(ValueError, "valid GitHub API response"):
                    release._upload_asset(
                        release.ROOT, RELEASE_ID, {"path": str(release.ROOT / "fixture.zip")}
                    )
                gh.assert_called_once()
        with mock.patch.object(
            release, "_gh", side_effect=ValueError("GitHub operation failed: api")
        ) as gh:
            with self.assertRaisesRegex(ValueError, "GitHub operation failed"):
                release._upload_asset(
                    release.ROOT, RELEASE_ID, {"path": str(release.ROOT / "fixture.zip")}
                )
            gh.assert_called_once()

    def test_api_rejects_malformed_duplicate_nonfinite_and_deep_json_cleanly(self):
        for response in (
            "{",
            '{"tag_name":"v0.1.0","tag_name":"v0.1.1"}',
            '{"size":NaN}',
            '{"size":Infinity}',
            '{"size":1e309}',
            "[" * 3000 + "0" + "]" * 3000,
        ):
            with (
                self.subTest(response=response[:80]),
                mock.patch.object(release, "_gh", return_value=response),
            ):
                with self.assertRaisesRegex(
                    ValueError, "^Cannot read a valid GitHub API response$"
                ):
                    release._api(release.ROOT, ENDPOINT)
        for error in (ValueError("Private operation error"), UnicodeError("Private decode error")):
            with self.subTest(error=error), mock.patch.object(release, "_gh", side_effect=error):
                with self.assertRaisesRegex(
                    ValueError, "^Cannot read a valid GitHub API response$"
                ):
                    release._api(release.ROOT, ENDPOINT)


class BinaryProcess:
    def __init__(self, chunks, *, exit_code=0):
        self.returncode = None
        self.exit_code = exit_code
        self.stdout = mock.Mock()
        self.stdout.read = mock.AsyncMock(side_effect=chunks)
        self.wait = mock.AsyncMock(side_effect=self.finish)
        self.kill = mock.Mock()
        self.communicate = mock.AsyncMock(side_effect=self.drain)

    async def finish(self):
        self.returncode = self.exit_code
        return self.returncode

    async def drain(self):
        self.returncode = -9
        return b"", b""


class AssetDownloadTests(OfflineTests):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = release.ROOT / ".verification" / "publish-stream-tests"
        cls.temp_root.resolve().relative_to(release.ROOT.resolve())
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.root = Path(directory.name).resolve()
        self.root.relative_to(self.temp_root.resolve())
        self.addCleanup(directory.cleanup)
        self.target = self.root / "download.zip"

    def child_process(self, chunks, *, exit_code=0):
        child = BinaryProcess(chunks, exit_code=exit_code)
        self.async_process.side_effect = None
        self.async_process.return_value = child
        return child

    def test_binary_download_preserves_nul_non_utf8_and_escape_bytes_without_a_shell(self):
        raw = b"\x00\xff\x1b[31mZIP\x80\r\n\x00"
        child = self.child_process((raw[:5], raw[5:], b""))
        with mock.patch.dict(
            release.os.environ,
            {"GH_REPO": "another/repository", "GH_HOST": "attacker.invalid"},
        ):
            release._download_asset(self.root, ASSET_ID, self.target, len(raw))
        self.assertEqual(self.target.read_bytes(), raw)
        arguments, options = self.async_process.await_args
        self.assertEqual(
            arguments,
            (
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--header",
                "Accept: application/octet-stream",
                f"repos/{release.SLUG}/releases/assets/{ASSET_ID}",
            ),
        )
        self.assertEqual(options["cwd"], self.root)
        self.assertEqual(options["stdout"], asyncio.subprocess.PIPE)
        self.assertEqual(options["stderr"], asyncio.subprocess.DEVNULL)
        self.assertEqual(
            options["creationflags"],
            subprocess.CREATE_NO_WINDOW if release.os.name == "nt" else 0,
        )
        self.assertNotIn("shell", options)
        self.assertNotIn("encoding", options)
        self.assertNotIn("text", options)
        self.assertNotIn("GH_REPO", options["env"])
        self.assertEqual(options["env"]["GH_HOST"], "github.com")
        self.assertEqual(options["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(
            [call.args[0] for call in child.stdout.read.await_args_list],
            [len(raw) + 1, len(raw) - 4, 1],
        )
        child.wait.assert_awaited_once()
        child.kill.assert_not_called()
        child.communicate.assert_not_awaited()
        self.process.assert_not_called()

    def test_multichunk_reads_are_bounded_by_expected_remaining_bytes_plus_one(self):
        raw = b"\x80" * 70_000
        child = self.child_process((raw[:65_536], raw[65_536:], b""))
        release._download_asset(self.root, ASSET_ID, self.target, len(raw))
        self.assertEqual(self.target.read_bytes(), raw)
        self.assertEqual(
            [call.args[0] for call in child.stdout.read.await_args_list], [65_536, 4465, 1]
        )
        child.kill.assert_not_called()

    def test_invalid_asset_ids_and_sizes_are_rejected_before_opening_or_spawning(self):
        for asset_id, expected_bytes in (
            (None, 1),
            (True, 1),
            (0, 1),
            (-1, 1),
            ("73", 1),
            (73.0, 1),
            (ASSET_ID, True),
            (ASSET_ID, 0),
            (ASSET_ID, -1),
            (ASSET_ID, "1"),
            (ASSET_ID, 1.0),
            (ASSET_ID, release.MAX_ZIP_BYTES),
            (ASSET_ID, release.MAX_ZIP_BYTES + 1),
        ):
            with self.subTest(asset_id=asset_id, expected_bytes=expected_bytes):
                with self.assertRaisesRegex(ValueError, "Invalid GitHub release asset download"):
                    release._download_asset(self.root, asset_id, self.target, expected_bytes)
                self.assertFalse(self.target.exists())
        self.async_process.assert_not_awaited()
        self.process.assert_not_called()

    def test_existing_download_target_is_never_overwritten_or_followed(self):
        original = b"Existing local file."
        self.target.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "^GitHub release asset download failed$"):
            release._download_asset(self.root, ASSET_ID, self.target, 1)
        self.assertEqual(self.target.read_bytes(), original)
        self.async_process.assert_not_awaited()

    def test_spawn_failure_is_sanitized_and_does_not_try_to_kill_an_unknown_process(self):
        for error in (
            OSError("synthetic-private-token"),
            subprocess.SubprocessError("synthetic-private-token"),
        ):
            with self.subTest(error=error):
                self.target = self.root / f"spawn-{type(error).__name__}.zip"
                self.async_process.side_effect = error
                with self.assertRaisesRegex(
                    ValueError, "^GitHub release asset download failed$"
                ) as raised:
                    release._download_asset(self.root, ASSET_ID, self.target, 1)
                self.assertNotIn("synthetic-private-token", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(self.target.read_bytes(), b"")

    def test_short_eof_and_nonzero_exit_are_rejected_without_publication_or_retry(self):
        for chunks, expected, exit_code, message in (
            ((b"x", b""), 2, 0, "unexpected size"),
            ((b"",), 1, 0, "unexpected size"),
            ((b"x", b""), 1, 22, "download failed"),
        ):
            with self.subTest(chunks=chunks, expected=expected, exit_code=exit_code):
                self.target = self.root / f"eof-{expected}-{exit_code}-{len(chunks)}.zip"
                child = self.child_process(chunks, exit_code=exit_code)
                with self.assertRaisesRegex(ValueError, message):
                    release._download_asset(self.root, ASSET_ID, self.target, expected)
                self.assertLessEqual(self.target.stat().st_size, expected)
                child.wait.assert_awaited_once()
                child.kill.assert_not_called()
                child.communicate.assert_not_awaited()

    def test_oversized_stdout_is_not_written_and_live_process_is_killed_then_drained(self):
        child = self.child_process((b"ab", b"cd"))
        with self.assertRaisesRegex(ValueError, "unexpected size"):
            release._download_asset(self.root, ASSET_ID, self.target, 3)
        self.assertEqual(self.target.read_bytes(), b"ab")
        child.wait.assert_not_awaited()
        child.kill.assert_called_once()
        child.communicate.assert_awaited_once()
        self.assertEqual(child.returncode, -9)
        self.assertEqual([call.args[0] for call in child.stdout.read.await_args_list], [4, 2])

    def test_missing_process_during_kill_still_drains_and_preserves_size_error(self):
        child = self.child_process((b"ab",))
        child.kill.side_effect = ProcessLookupError("Process already exited")
        with self.assertRaisesRegex(ValueError, "unexpected size"):
            release._download_asset(self.root, ASSET_ID, self.target, 1)
        self.assertEqual(self.target.read_bytes(), b"")
        child.kill.assert_called_once()
        child.communicate.assert_awaited_once()

    def test_read_or_exit_stall_times_out_and_kills_then_drains(self):
        async def stall(*_args):
            await asyncio.Event().wait()

        for phase in ("read", "wait"):
            with self.subTest(phase=phase):
                self.target = self.root / f"stall-{phase}.zip"
                child = self.child_process((b"x", b""))
                if phase == "read":
                    child.stdout.read.side_effect = stall
                else:
                    child.wait.side_effect = stall
                with mock.patch.object(release, "GH_TIMEOUT", 0.02):
                    with self.assertRaisesRegex(
                        ValueError, "^GitHub release asset download failed$"
                    ):
                        release._download_asset(self.root, ASSET_ID, self.target, 1)
                child.kill.assert_called_once()
                child.communicate.assert_awaited_once()
                self.assertLessEqual(self.target.stat().st_size, 1)

    def test_cleanup_drain_deadline_is_bounded_to_five_seconds(self):
        child = self.child_process((b"ab",))
        timeouts = []
        wait_for = asyncio.wait_for

        async def stall():
            await asyncio.Event().wait()

        async def bounded_wait(awaitable, *, timeout):
            timeouts.append(timeout)
            return await wait_for(awaitable, timeout=0.02 if timeout == 5 else timeout)

        child.communicate.side_effect = stall
        with mock.patch.object(release.asyncio, "wait_for", side_effect=bounded_wait):
            with self.assertRaisesRegex(ValueError, "^GitHub release asset download failed$"):
                release._download_asset(self.root, ASSET_ID, self.target, 1)
        self.assertEqual(timeouts, [release.GH_TIMEOUT, 5])
        child.kill.assert_called_once()
        child.communicate.assert_awaited_once()
        self.assertEqual(self.target.read_bytes(), b"")

    def test_cancelled_stream_kills_and_drains_before_cancellation_is_returned(self):
        child = self.child_process(())

        async def cancellation():
            entered = asyncio.Event()

            async def read(_size):
                entered.set()
                await asyncio.Event().wait()

            child.stdout.read.side_effect = read
            task = asyncio.create_task(release._stream_asset(self.root, ASSET_ID, self.target, 1))
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(child.returncode, -9)

        asyncio.run(cancellation())
        child.kill.assert_called_once()
        child.communicate.assert_awaited_once()
        self.assertEqual(self.target.read_bytes(), b"")

    def test_binary_read_oserror_is_sanitized_after_kill_and_drain(self):
        child = self.child_process((OSError("synthetic-private-token"),))
        with self.assertRaisesRegex(ValueError, "^GitHub release asset download failed$") as raised:
            release._download_asset(self.root, ASSET_ID, self.target, 1)
        self.assertNotIn("synthetic-private-token", str(raised.exception))
        child.kill.assert_called_once()
        child.communicate.assert_awaited_once()


class PublicationCliTests(OfflineTests):
    def test_cli_passes_exact_tag_and_dry_run_and_prints_result_json(self):
        result = {"tag": TAG, "publication": "dry-run"}
        with (
            mock.patch.object(release, "publish", return_value=result) as publisher,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(release.main(["--tag", TAG, "--dry-run"]), result)
        publisher.assert_called_once_with(tag=TAG, dry_run=True)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_cli_missing_tag_stops_before_publication(self):
        with (
            mock.patch.object(release, "publish") as publisher,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                release.main([])
        self.assertEqual(raised.exception.code, 2)
        publisher.assert_not_called()

    def test_cli_reports_errors_without_traceback_or_success_output(self):
        for error in (
            ValueError("Verification failed"),
            OSError("Unavailable"),
            KeyError("missing"),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(release, "publish", side_effect=error),
                redirect_stderr(io.StringIO()) as stderr,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                with self.assertRaises(SystemExit) as raised:
                    release.main(["--tag", TAG])
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("Release publication failed:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
