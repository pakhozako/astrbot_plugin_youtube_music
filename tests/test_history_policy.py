"""Real repository-local Git fixtures for commit and SemVer release policy."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from scripts import build_release, check_history

ROOT = build_release.ROOT


def history_bytes(messages):
    records = []
    previous = ""
    for index, message in enumerate(messages, 1):
        commit = f"{index:040x}"
        records.append(f"{commit}\0{previous}\0{message}\0".encode())
        previous = commit
    return b"".join(records), previous


class CommitParsingTests(unittest.TestCase):
    def test_all_conventional_types_scopes_and_breaking_headers_are_supported(self):
        types = (
            "feat",
            "fix",
            "docs",
            "style",
            "refactor",
            "perf",
            "test",
            "build",
            "ci",
            "chore",
            "revert",
        )
        raw, head = history_bytes([f"{kind}(core/queue)!: A scoped change" for kind in types])
        commits = check_history._parse_history(raw, head)
        self.assertEqual([commit["type"] for commit in commits], list(types))
        self.assertTrue(all(commit["breaking"] for commit in commits))

    def test_invalid_titles_fail_closed(self):
        titles = (
            "",
            "Initial commit",
            "feature: Add music",
            "Feat: Add music",
            "fix:missing separator",
            "fix: ",
            "fix:  Leading space",
            "fix: Trailing space ",
            "fix(): Empty scope",
            "fix( worker): Padded scope",
            "fix(worker ): Padded scope",
            "fix((worker)): Nested scope",
            "fix(worker)! : Bad separator",
            "fix: Contains\ta tab",
        )
        for title in titles:
            with self.subTest(title=title):
                raw, head = history_bytes([title])
                with self.assertRaisesRegex(ValueError, "Conventional Commits"):
                    check_history._parse_history(raw, head)

    def test_breaking_headers_and_footers_have_major_priority(self):
        for message in (
            "fix!: Remove old behavior",
            "feat(worker)!: Replace playback",
            "refactor: Replace API\n\nBREAKING CHANGE: Old clients no longer work.\n",
            "chore: Replace API\n\nBREAKING-CHANGE: Old clients no longer work.\n",
        ):
            with self.subTest(message=message):
                raw, head = history_bytes(["feat: Add music", message])
                commits = check_history._parse_history(raw, head)
                self.assertEqual(check_history._next_version("0.1.9", commits), ("1.0.0", "major"))

    def test_body_mentions_and_subject_words_are_not_breaking_footers(self):
        raw, head = history_bytes(
            [
                "fix: Explain BREAKING CHANGE behavior\n\n"
                "This discusses BREAKING CHANGE: without declaring a footer.\n",
                "docs: Quote a change\n\n> BREAKING CHANGE: A quoted example.\n",
            ]
        )
        commits = check_history._parse_history(raw, head)
        self.assertFalse(any(commit["breaking"] for commit in commits))
        self.assertEqual(check_history._next_version("1.2.3", commits), ("1.2.4", "patch"))

    def test_minor_changes_reset_patch_and_empty_intervals_are_rejected(self):
        raw, head = history_bytes(["fix: Repair music", "feat: Add music"])
        commits = check_history._parse_history(raw, head)
        self.assertEqual(check_history._next_version("1.2.9", commits), ("1.3.0", "minor"))
        with self.assertRaisesRegex(ValueError, "new commits"):
            check_history._next_version("0.1.0", [])

    def test_malformed_objects_utf8_and_non_linear_history_are_rejected(self):
        raw, head = history_bytes(["chore: Establish music", "fix: Repair music"])
        cases = (
            (b"", head, "empty"),
            (b"invalid\0\0fix: Repair music\0", head, "identity"),
            (raw.replace(b"fix: Repair music", b"fix: \xff"), head, "UTF-8"),
            (raw + b"extra\0", head, "history"),
            (
                raw.replace(b"\0fix: Repair music", b" " + b"f" * 40 + b"\0fix: Repair music"),
                head,
                "Merge",
            ),
            (
                raw.replace(b"0000000000000000000000000000000000000001\0fix", b"f" * 40 + b"\0fix"),
                head,
                "linear",
            ),
            (raw, "f" * 40, "HEAD"),
        )
        for data, target, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    check_history._parse_history(data, target)

    def test_sha256_object_ids_are_supported(self):
        head = "a" * 64
        commits = check_history._parse_history(f"{head}\0\0chore: Establish music\0".encode(), head)
        self.assertEqual(commits[0]["commit"], head)


class HistoricalMetadataTests(unittest.TestCase):
    def metadata_bytes(self, **changes):
        return yaml.safe_dump(
            {
                "name": build_release.PLUGIN,
                "repo": build_release.REPOSITORY,
                "version": "0.1.0",
                **changes,
            }
        ).encode()

    def test_identity_version_does_not_depend_on_historical_runtime_support(self):
        for support in (
            {},
            {"astrbot_version": ">=4.29.4,<5", "support_platforms": ["aiocqhttp"]},
            {"astrbot_version": ">=5,<6", "support_platforms": ["new-adapter"]},
        ):
            with self.subTest(support=support):
                self.assertEqual(
                    check_history._historical_version(self.metadata_bytes(**support)), "0.1.0"
                )

    def test_wrong_or_missing_plugin_identity_is_rejected(self):
        for changes in (
            {"name": "another_plugin"},
            {"name": None},
            {"repo": "https://untrusted.invalid/project"},
            {"repo": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "identity"):
                    check_history._historical_version(self.metadata_bytes(**changes))
        for raw in (b"version: 0.1.0\n", b"[]\n", b"null\n"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "identity"):
                    check_history._historical_version(raw)

    def test_invalid_semantic_versions_are_rejected(self):
        for version in (None, True, 1, "v0.1.0", "0.01.0", "0.1.0-rc.1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "semantic version"):
                    check_history._historical_version(self.metadata_bytes(version=version))

    def test_duplicate_yaml_keys_are_rejected_even_outside_identity(self):
        for suffix in (
            b"version: 0.1.0\n",
            f"name: {build_release.PLUGIN}\n".encode(),
            f"repo: {build_release.REPOSITORY}\n".encode(),
            b"extra:\n  value: 1\n  value: 2\n",
        ):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(ValueError, "Duplicate.*YAML"):
                    check_history._historical_version(self.metadata_bytes() + suffix)

    def test_malformed_yaml_and_unsafe_object_constructors_are_rejected(self):
        for raw in (
            b"name: [\n",
            b"\xff\n",
            b"!!python/object/apply:os.system ['echo should-not-run']\n",
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "metadata YAML"):
                    check_history._historical_version(raw)


class HistoryPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = ROOT / ".verification" / "history-tests"
        cls.temp_root.resolve().relative_to(ROOT.resolve())
        cls.temp_root.mkdir(parents=True, exist_ok=True)
        cls.meta = build_release.metadata((ROOT / "metadata.yaml").read_bytes())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.relative_to(self.temp_root.resolve())
        self.counter = 0
        self.git("init", "--initial-branch=main", "--template=")

    def git(self, *arguments):
        return build_release._git_bytes(
            self.root,
            "-c",
            "user.name=History Tests",
            "-c",
            "user.email=history@example.test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            f"core.hooksPath={self.root / '.git' / 'no-hooks'}",
            "-c",
            "core.autocrlf=false",
            *arguments,
        )

    def commit(self, title, *, version=None, body=""):
        if version is not None:
            (self.root / "metadata.yaml").write_text(
                yaml.safe_dump({**self.meta, "version": version}, allow_unicode=True),
                encoding="utf-8",
            )
        self.counter += 1
        (self.root / "change.txt").write_text(f"Change {self.counter}\n", encoding="utf-8")
        self.git("add", "--", "metadata.yaml", "change.txt")
        arguments = ["commit", "--no-verify", "-m", title]
        if body:
            arguments.extend(("-m", body))
        self.git(*arguments)
        return self.git("rev-parse", "HEAD").decode("ascii").strip()

    def tag(self, name, *, annotated=True):
        arguments = ["tag"]
        if annotated:
            arguments.extend(("-a", "-m", f"Release {name}"))
        self.git(*arguments, name)

    def initial_release(self):
        head = self.commit("feat: Establish music", version="0.1.0")
        self.tag("v0.1.0")
        return head

    def next_release(self, title, version, *, body=""):
        self.initial_release()
        head = self.commit(title, version=version, body=body)
        tag = f"v{version}"
        self.tag(tag)
        return head, tag

    def test_plain_check_does_not_require_tags_or_read_worktree_metadata(self):
        self.commit("chore: Establish music", version="0.1.0")
        head = self.commit("fix(worker): Repair music")
        (self.root / "metadata.yaml").write_text("invalid dirty metadata\n", encoding="utf-8")
        result = check_history.check_history(self.root)
        self.assertEqual(result["head"], head)
        self.assertEqual(result["source_commit"], head)
        self.assertEqual(result["commit_count"], 2)
        self.assertTrue(result["conventional_commits_verified"])
        self.assertTrue(result["linear_history_verified"])
        self.assertNotIn("tag_object", result)

    def test_first_annotated_v010_release_is_verified(self):
        head = self.initial_release()
        result = check_history.check_history(self.root, tag="v0.1.0")
        self.assertEqual(result["head"], head)
        self.assertEqual(result["tag_object"], self.git("rev-parse", "v0.1.0").decode().strip())
        self.assertIsNone(result["previous_tag"])
        self.assertEqual(result["release_commit_count"], 1)
        self.assertEqual(result["bump"], "initial")
        self.assertTrue(result["semver_verified"])

    def test_initial_release_must_not_skip_v010(self):
        self.commit("feat: Establish music", version="0.2.0")
        self.tag("v0.2.0")
        with self.assertRaisesRegex(ValueError, "first release.*v0.1.0"):
            check_history.check_history(self.root, tag="v0.2.0")

    def test_lightweight_version_tags_are_rejected(self):
        self.commit("feat: Establish music", version="0.1.0")
        self.tag("v0.1.0", annotated=False)
        with self.assertRaisesRegex(ValueError, "annotated"):
            check_history.check_history(self.root, tag="v0.1.0")

    def test_requested_release_tag_must_point_to_current_head(self):
        self.initial_release()
        self.commit("docs: Clarify music")
        with self.assertRaisesRegex(ValueError, "point to HEAD"):
            check_history.check_history(self.root, tag="v0.1.0")

    def test_requested_tag_must_exist(self):
        self.commit("feat: Establish music", version="0.1.0")
        with self.assertRaisesRegex(ValueError, "must exist"):
            check_history.check_history(self.root, tag="v0.1.0")

    def test_committed_metadata_must_match_release_tag(self):
        self.commit("feat: Establish music", version="0.2.0")
        self.tag("v0.1.0")
        with self.assertRaisesRegex(ValueError, "committed metadata"):
            check_history.check_history(self.root, tag="v0.1.0")

    def test_major_support_change_does_not_apply_current_schema_to_old_tags(self):
        self.initial_release()
        (self.root / "metadata.yaml").write_text(
            yaml.safe_dump(
                {
                    **self.meta,
                    "version": "1.0.0",
                    "astrbot_version": ">=5,<6",
                    "support_platforms": ["new-adapter"],
                }
            ),
            encoding="utf-8",
        )
        self.commit("feat!: Target the next AstrBot platform API")
        self.tag("v1.0.0")
        with mock.patch.object(
            build_release, "metadata", side_effect=ValueError("Legacy runtime is unsupported")
        ) as current_schema:
            result = check_history.check_history(self.root, tag="v1.0.0")
        current_schema.assert_not_called()
        self.assertEqual(result["previous_tag"], "v0.1.0")
        self.assertEqual(result["release_count"], 2)
        self.assertEqual(result["bump"], "major")

    def test_historical_wrong_identity_cannot_be_hidden_by_current_valid_metadata(self):
        valid_meta = self.meta
        self.meta = {**valid_meta, "repo": "https://untrusted.invalid/project"}
        self.initial_release()
        self.meta = valid_meta
        self.commit("fix: Repair playback", version="0.1.1")
        self.tag("v0.1.1")
        with self.assertRaisesRegex(ValueError, "metadata identity"):
            check_history.check_history(self.root, tag="v0.1.1")

    def test_historical_duplicate_yaml_cannot_be_hidden_by_current_valid_metadata(self):
        self.commit("feat: Establish music", version="0.1.0")
        path = self.root / "metadata.yaml"
        path.write_bytes(path.read_bytes() + b"version: 0.1.0\n")
        self.commit("chore: Preserve published metadata")
        self.tag("v0.1.0")
        self.commit("fix: Repair playback", version="0.1.1")
        self.tag("v0.1.1")
        with self.assertRaisesRegex(ValueError, "Duplicate.*YAML"):
            check_history.check_history(self.root, tag="v0.1.1")

    def test_annotated_internal_name_must_match_release_ref(self):
        self.commit("feat: Establish music", version="0.1.0")
        self.tag("archive")
        tag_object = self.git("rev-parse", "archive").decode().strip()
        self.git("update-ref", "refs/tags/v0.1.0", tag_object)
        with self.assertRaisesRegex(ValueError, "Annotated tag name"):
            check_history.check_history(self.root, tag="v0.1.0")

    def test_patch_release_uses_exact_next_patch(self):
        head, tag = self.next_release("fix(worker): Repair music", "0.1.1")
        result = check_history.check_history(self.root, tag=tag)
        self.assertEqual(result["head"], head)
        self.assertEqual(result["previous_tag"], "v0.1.0")
        self.assertEqual(result["bump"], "patch")
        self.assertEqual(result["release_commit_count"], 1)

    def test_feature_release_uses_exact_next_minor(self):
        _, tag = self.next_release("feat(console): Add batch playback", "0.2.0")
        result = check_history.check_history(self.root, tag=tag)
        self.assertEqual(result["bump"], "minor")
        self.assertEqual(result["version"], "0.2.0")

    def test_breaking_header_release_uses_next_major_even_before_v1(self):
        _, tag = self.next_release("refactor(worker)!: Replace playback", "1.0.0")
        result = check_history.check_history(self.root, tag=tag)
        self.assertEqual(result["bump"], "major")

    def test_breaking_footer_release_uses_next_major(self):
        _, tag = self.next_release(
            "chore: Replace playback", "1.0.0", body="BREAKING CHANGE: The old API is removed."
        )
        result = check_history.check_history(self.root, tag=tag)
        self.assertEqual(result["bump"], "major")

    def test_feature_changes_cannot_be_published_as_patch(self):
        _, tag = self.next_release("feat: Add batch playback", "0.1.1")
        with self.assertRaisesRegex(ValueError, "must be v0.2.0.*MINOR"):
            check_history.check_history(self.root, tag=tag)

    def test_fix_changes_cannot_be_published_as_minor(self):
        _, tag = self.next_release("fix: Repair playback", "0.2.0")
        with self.assertRaisesRegex(ValueError, "must be v0.1.1.*PATCH"):
            check_history.check_history(self.root, tag=tag)

    def test_patch_versions_must_not_skip(self):
        _, tag = self.next_release("fix: Repair playback", "0.1.2")
        with self.assertRaisesRegex(ValueError, "must be v0.1.1"):
            check_history.check_history(self.root, tag=tag)

    def test_breaking_changes_cannot_be_published_as_minor(self):
        _, tag = self.next_release("fix!: Replace playback", "0.2.0")
        with self.assertRaisesRegex(ValueError, "must be v1.0.0.*MAJOR"):
            check_history.check_history(self.root, tag=tag)

    def test_previous_release_is_the_last_reachable_tag_in_commit_order(self):
        self.initial_release()
        self.commit("fix: Repair music", version="0.1.1")
        self.tag("v0.1.1")
        self.commit("feat: Add batch playback", version="0.2.0")
        self.tag("v0.2.0")
        self.commit("docs: Document batch playback")
        self.commit("chore(release): Prepare release", version="0.2.1")
        self.tag("v0.2.1")
        result = check_history.check_history(self.root, tag="v0.2.1")
        self.assertEqual(result["previous_tag"], "v0.2.0")
        self.assertEqual(result["release_commit_count"], 2)
        self.assertEqual(result["release_count"], 4)

    def test_historical_version_skip_cannot_be_hidden_by_a_valid_latest_bump(self):
        self.initial_release()
        self.commit("fix: Repair playback", version="0.1.2")
        self.tag("v0.1.2")
        self.commit("feat: Add batch playback", version="0.2.0")
        self.tag("v0.2.0")
        with self.assertRaisesRegex(ValueError, "Release v0.1.2 must be v0.1.1"):
            check_history.check_history(self.root, tag="v0.2.0")

    def test_multiple_release_versions_on_same_commit_are_rejected(self):
        self.initial_release()
        self.tag("v0.1.1")
        with self.assertRaises(ValueError):
            check_history.check_history(self.root, tag="v0.1.1")

    def test_unreachable_release_tags_do_not_select_the_previous_version(self):
        self.initial_release()
        self.git("checkout", "-b", "archive")
        self.commit("feat: Archived experiment", version="9.0.0")
        self.tag("v9.0.0")
        self.git("checkout", "main")
        self.commit("feat: Add batch playback", version="0.2.0")
        self.tag("v0.2.0")
        result = check_history.check_history(self.root, tag="v0.2.0")
        self.assertEqual(result["previous_tag"], "v0.1.0")
        self.assertEqual(result["release_count"], 2)

    def test_non_release_tags_do_not_require_annotation_or_affect_the_chain(self):
        self.initial_release()
        self.tag("archive-current", annotated=False)
        self.tag("v0.1.0-rc.1", annotated=False)
        result = check_history.check_history(self.root, tag="v0.1.0")
        self.assertEqual(result["release_count"], 1)

    def test_merge_commits_are_rejected_even_with_conventional_title(self):
        self.initial_release()
        self.git("checkout", "-b", "feature")
        self.git("commit", "--allow-empty", "--no-verify", "-m", "docs: Clarify feature")
        self.git("checkout", "main")
        self.git("commit", "--allow-empty", "--no-verify", "-m", "fix: Repair music")
        self.git("merge", "--no-ff", "feature", "-m", "chore: Merge feature")
        with self.assertRaisesRegex(ValueError, "Merge commits"):
            check_history.check_history(self.root)

    def test_shallow_history_is_rejected_without_attempting_network_fetch(self):
        head = self.initial_release()
        (self.root / ".git" / "shallow").write_text(f"{head}\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "non-shallow"):
            check_history.check_history(self.root)

    def test_a_git_repository_subdirectory_is_not_an_accepted_root(self):
        self.initial_release()
        child = self.root / "child"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "root does not match"):
            check_history.check_history(child)

    def test_git_environment_redirects_are_ignored(self):
        head = self.initial_release()
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.root / "not-a-repository"),
                "GIT_WORK_TREE": str(self.root / "not-a-worktree"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            result = check_history.check_history(self.root, tag="v0.1.0")
        self.assertEqual(result["head"], head)

    def test_invalid_tag_inputs_are_rejected_before_git_reads(self):
        for tag in (
            "",
            "--help",
            "HEAD",
            "refs/tags/v0.1.0",
            "v0.01.0",
            "v0.1.0-rc.1",
            "v0.1.0\n",
            "v0.1.0; echo unsafe",
            1,
        ):
            with (
                self.subTest(tag=tag),
                mock.patch.object(build_release, "_git_bytes") as read,
                self.assertRaisesRegex(ValueError, "exact vX.Y.Z"),
            ):
                check_history.check_history(self.root, tag=tag)
            read.assert_not_called()


class HistoryCliTests(unittest.TestCase):
    def test_plain_cli_uses_only_fixed_repository_root_and_prints_json(self):
        expected = {"head": "a" * 40, "commit_count": 1}
        output = io.StringIO()
        with (
            mock.patch.object(check_history, "check_history", return_value=expected) as check,
            redirect_stdout(output),
        ):
            result = check_history.main([])
        check.assert_called_once_with(check_history.ROOT, tag=None)
        self.assertEqual(result, expected)
        self.assertEqual(json.loads(output.getvalue()), expected)

    def test_tag_cli_passes_exact_tag_to_read_only_gate(self):
        expected = {"tag": "v0.1.0", "tag_object": "b" * 40}
        with (
            mock.patch.object(check_history, "check_history", return_value=expected) as check,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(check_history.main(["--tag", "v0.1.0"]), expected)
        check.assert_called_once_with(check_history.ROOT, tag="v0.1.0")

    def test_cli_does_not_accept_an_external_root_or_unknown_arguments(self):
        for arguments in (["--root", "elsewhere"], ["--unknown"], ["--tag"]):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(check_history, "check_history") as check,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as failure,
            ):
                check_history.main(arguments)
            self.assertEqual(failure.exception.code, 2)
            check.assert_not_called()

    def test_validation_and_io_failures_report_clean_nonzero_exit_without_success(self):
        for error in (ValueError("Invalid history"), OSError("Git is unavailable")):
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(error=type(error)),
                mock.patch.object(check_history, "check_history", side_effect=error),
                redirect_stdout(output),
                redirect_stderr(errors),
                self.assertRaises(SystemExit) as failure,
            ):
                check_history.main([])
            self.assertEqual(failure.exception.code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("History validation failed:", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())

    def test_interrupted_verification_does_not_report_success(self):
        output = io.StringIO()
        with (
            mock.patch.object(check_history, "check_history", side_effect=KeyboardInterrupt),
            redirect_stdout(output),
            self.assertRaises(KeyboardInterrupt),
        ):
            check_history.main(["--tag", "v0.1.0"])
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
