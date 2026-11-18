"""Release ZIP integrity, reproducibility and development artifact exclusion."""

import hashlib
import io
import json
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

import yaml

from scripts import build_release
from scripts.build_release import (
    FILES,
    PLUGIN,
    REPOSITORY,
    ROOT,
    VERSION_FILES,
    build,
    check_built,
    main,
    metadata,
    validate,
    validate_versions,
)


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = ROOT / ".verification" / "release-tests"
        cls.temp_root.resolve().relative_to(ROOT.resolve())
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def temporary_directory(self):
        return tempfile.TemporaryDirectory(dir=self.temp_root)

    def copy_source(self, directory):
        root = Path(directory) / "source"
        for name in (*FILES, *VERSION_FILES):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        return root

    def source_bytes(self, root):
        return {name: (root / name).read_bytes() for name in (*FILES, *VERSION_FILES)}

    def set_source_version(self, root, version, changelog=None):
        meta = metadata((root / "metadata.yaml").read_bytes())
        previous = meta["version"]
        (root / "metadata.yaml").write_text(
            yaml.safe_dump({**meta, "version": version}, allow_unicode=True), encoding="utf-8"
        )
        for name in VERSION_FILES:
            path = root / name
            package = json.loads(path.read_bytes())
            package["version"] = version
            if name == "package-lock.json":
                package["packages"][""]["version"] = version
            path.write_text(json.dumps(package), encoding="utf-8")
        manager = root / "core/dependency_manager.py"
        manager.write_text(
            manager.read_text(encoding="utf-8").replace(
                f"Dependency-Manager/{previous}", f"Dependency-Manager/{version}"
            ),
            encoding="utf-8",
        )
        readme = root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                f"badge/version-{previous}-", f"badge/version-{version}-"
            ),
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            changelog
            or (
                f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-11-18\n\n"
                "### Fixed\n\n- Synthetic release fixture.\n"
            ),
            encoding="utf-8",
        )

    def git_source(self, root, *, source=None, commit="a" * 40, repository=None):
        snapshot = self.source_bytes(root) if source is None else source

        def read(actual_root, *arguments):
            self.assertEqual(actual_root, root.resolve())
            if arguments == ("rev-parse", "--show-toplevel"):
                return (str(repository or root.resolve()) + "\n").encode("utf-8")
            if arguments[0] == "rev-parse":
                self.assertEqual(arguments[1], "--verify")
                self.assertRegex(arguments[2], r"^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+\^\{commit\}$")
                return (commit + "\n").encode("ascii")
            self.assertEqual(arguments[0], "cat-file")
            prefix, name = arguments[2].split(":", 1)
            self.assertEqual(prefix, commit)
            if arguments[1] == "-s":
                return (str(len(snapshot[name])) + "\n").encode("ascii")
            self.assertEqual(arguments[1], "blob")
            return snapshot[name]

        return mock.patch.object(build_release, "_git_bytes", side_effect=read)

    def altered_manifest(self, target, mutate):
        original = target.with_name("original.zip")
        build(ROOT, original)
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(target, "w") as output:
            for entry in source.infolist():
                data = source.read(entry.filename)
                if entry.filename == "RELEASE_MANIFEST.json":
                    changed = mutate(json.loads(data))
                    data = changed if isinstance(changed, bytes) else json.dumps(changed).encode()
                output.writestr(entry, data)

    def test_public_documentation_local_links_resolve_inside_the_repository(self):
        names = (
            "README.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
            *(path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "docs").rglob("*.md"))),
        )
        for name in names:
            source = ROOT / name
            for link in re.findall(r"\]\(([^)\s]+)\)", source.read_text(encoding="utf-8")):
                if "://" in link or link.startswith("#"):
                    continue
                target = (source.parent / link.split("#", 1)[0]).resolve()
                target.relative_to(ROOT)
                self.assertTrue(target.is_file(), f"{name}: {link}")

    def test_view_references_are_local_packaged_assets_and_locale_keys_match(self):
        class Assets(HTMLParser):
            def __init__(self):
                super().__init__()
                self.paths = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "script":
                    self.paths.append(attributes["src"])
                elif tag == "link" and attributes.get("rel") == "stylesheet":
                    self.paths.append(attributes["href"])

        parser = Assets()
        parser.feed((ROOT / "views/console/index.html").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(parser.paths), 3)
        for asset in parser.paths:
            self.assertTrue(asset.startswith("./"))
            self.assertNotIn("..", asset)
            name = f"views/console/{asset.removeprefix('./')}"
            self.assertIn(name, FILES)
            self.assertTrue((ROOT / name).is_file())
        locales = [
            json.loads((ROOT / f".astrbot-plugin/i18n/{locale}.json").read_text(encoding="utf-8"))[
                "views"
            ]["console"]
            for locale in ("zh-CN", "en-US")
        ]
        self.assertEqual(set(locales[0]), set(locales[1]))
        self.assertTrue(
            all(isinstance(value, str) and value for data in locales for value in data.values())
        )

    def test_packaged_documentation_links_do_not_require_development_files(self):
        with self.temporary_directory() as directory:
            target = Path(directory) / "candidate.zip"
            build(ROOT, target)
            with zipfile.ZipFile(target) as archive:
                for name in FILES:
                    if not name.endswith(".md"):
                        continue
                    text = archive.read(name).decode("utf-8")
                    for link in re.findall(r"\]\(([^)\s]+)\)", text):
                        if "://" in link or link.startswith("#"):
                            continue
                        resolved = (ROOT / name).parent / link.split("#", 1)[0]
                        packaged = resolved.resolve().relative_to(ROOT).as_posix()
                        self.assertIn(packaged, archive.namelist(), f"{name}: {link}")

    def test_allowlist_manifest_and_source_bytes_match(self):
        with self.temporary_directory() as directory:
            target = Path(directory) / "candidate.zip"
            result = build(ROOT, target)
            with zipfile.ZipFile(target) as archive:
                expected = {*FILES, "RELEASE_MANIFEST.json"}
                self.assertEqual(set(archive.namelist()), expected)
                for name in FILES:
                    self.assertEqual(archive.read(name), (ROOT / name).read_bytes())
            self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(result["repository"], REPOSITORY)

    def test_release_identity_is_consistent_across_metadata_frontend_and_updater(self):
        meta = metadata((ROOT / "metadata.yaml").read_bytes())
        self.assertEqual(validate_versions(self.source_bytes(ROOT))["version"], meta["version"])
        self.assertEqual(meta["author"], "pakhozako")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["version"], meta["version"])
        self.assertEqual(lock["version"], meta["version"])
        self.assertEqual(lock["packages"][""]["version"], meta["version"])
        self.assertIn(
            f"Dependency-Manager/{meta['version']}",
            (ROOT / "core/dependency_manager.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(package["repository"]["url"], REPOSITORY + ".git")
        self.assertEqual(package["author"], meta["author"])
        self.assertEqual(package["license"], "MIT")
        self.assertEqual(lock["packages"][""]["license"], "MIT")
        self.assertIn("LICENSE", FILES)

    def test_project_mit_license_and_third_party_texts_are_present(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n\n"))
        self.assertIn("Copyright (c) 2026 pakhozako", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)
        for name in (
            "views/console/licenses/material-MIT.txt",
            "views/console/licenses/material-NOTICE.txt",
            "views/console/licenses/material-symbols-Apache-2.0.txt",
        ):
            self.assertIn(name, FILES)
            self.assertGreater((ROOT / name).stat().st_size, 100)
        third_party = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 pakhozako", third_party)
        self.assertIn("license does not replace or relicense", third_party)

    def test_missing_repository_and_invalid_version_are_rejected(self):
        meta = yaml.safe_load((ROOT / "metadata.yaml").read_bytes())
        for version in ("v0.1.0", "0.01.0", "0.1.0-preview", True):
            with self.assertRaisesRegex(ValueError, "semantic version"):
                metadata(yaml.safe_dump({**meta, "version": version}).encode())
        with self.assertRaisesRegex(ValueError, "repository"):
            metadata(yaml.safe_dump({**meta, "repo": ""}).encode())

    def test_same_source_creates_identical_zip_without_overwriting_existing_file(self):
        with self.temporary_directory() as directory:
            first, second = Path(directory) / "one.zip", Path(directory) / "two.zip"
            build(ROOT, first)
            build(ROOT, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with self.assertRaises(FileExistsError):
                build(ROOT, first)

    def test_added_development_or_secret_file_is_rejected(self):
        with self.temporary_directory() as directory:
            target = Path(directory) / "candidate.zip"
            build(ROOT, target)
            with zipfile.ZipFile(target, "a") as archive:
                archive.writestr("tests/credentials.json", "{}")
            with self.assertRaisesRegex(ValueError, "allowlist"):
                validate(target)

    def test_modified_payload_is_rejected_by_manifest(self):
        with self.temporary_directory() as directory:
            original, altered = Path(directory) / "original.zip", Path(directory) / "altered.zip"
            build(ROOT, original)
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(altered, "w") as target:
                for name in source.namelist():
                    data = source.read(name)
                    target.writestr(name, data + b"\n# altered\n" if name == "main.py" else data)
            with self.assertRaisesRegex(ValueError, "digest"):
                validate(altered)

    def test_duplicate_entry_is_rejected(self):
        with self.temporary_directory() as directory:
            target = Path(directory) / "candidate.zip"
            build(ROOT, target)
            with zipfile.ZipFile(target, "a") as archive:
                with self.assertWarns(UserWarning):
                    archive.writestr("main.py", b"")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                validate(target)

    def test_mismatched_repository_identity_is_rejected(self):
        with self.temporary_directory() as directory:
            original, altered = Path(directory) / "original.zip", Path(directory) / "altered.zip"
            build(ROOT, original)
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(altered, "w") as target:
                for name in source.namelist():
                    data = source.read(name)
                    if name == "RELEASE_MANIFEST.json":
                        manifest = json.loads(data)
                        manifest["repository"] = "https://github.com/example/other"
                        data = json.dumps(manifest).encode()
                    target.writestr(name, data)
            with self.assertRaisesRegex(ValueError, "identity"):
                validate(altered)

    def test_malformed_manifest_identity_and_records_are_rejected_cleanly(self):
        for mutate in (
            lambda manifest: [],
            lambda manifest: {**manifest, "schema_version": True},
            lambda manifest: {**manifest, "unexpected": True},
            lambda manifest: {**manifest, "files": [None] * len(FILES)},
            lambda manifest: {
                **manifest,
                "files": [{**row, "bytes": True} for row in manifest["files"]],
            },
            lambda manifest: {
                **manifest,
                "files": [{**row, "path": []} for row in manifest["files"]],
            },
            lambda manifest: {
                **manifest,
                "files": [{**row, "sha256": "not-a-digest"} for row in manifest["files"]],
            },
            lambda manifest: {
                **manifest,
                "files": [{**row, "unexpected": True} for row in manifest["files"]],
            },
        ):
            with self.subTest(mutate=mutate), self.temporary_directory() as directory:
                target = Path(directory) / "altered.zip"
                self.altered_manifest(target, mutate)
                with self.assertRaises(ValueError):
                    validate(target)

    def test_non_regular_runtime_entries_are_rejected(self):
        with self.temporary_directory() as directory:
            original = Path(directory) / "original.zip"
            build(ROOT, original)
            for kind in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO):
                target = Path(directory) / f"{kind}.zip"
                with zipfile.ZipFile(original) as source, zipfile.ZipFile(target, "w") as output:
                    for entry in source.infolist():
                        data = source.read(entry.filename)
                        if entry.filename == "main.py":
                            entry.create_system = 3
                            entry.external_attr = (kind | 0o644) << 16
                        output.writestr(entry, data)
                with self.assertRaisesRegex(ValueError, "non-regular"):
                    validate(target)

    def test_invalid_source_is_rejected_before_creating_a_release(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            (root / "main.py").write_text("def invalid(:", encoding="utf-8")
            output = Path(directory) / "invalid.zip"
            with self.assertRaises(SyntaxError):
                build(root, output)
            self.assertFalse(output.exists())

    def test_ambiguous_or_nonfinite_source_json_is_rejected_before_creating_a_release(self):
        for raw in (
            '{"value":1e309}',
            '{"value":-1e309}',
            '{"value":1,"value":2}',
            '{"value":' + "[" * 3000 + "0" + "]" * 3000 + "}",
        ):
            with self.subTest(raw=raw), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                (root / "_conf_schema.json").write_text(raw, encoding="utf-8")
                output = Path(directory) / "invalid.zip"
                with self.assertRaises(ValueError):
                    build(root, output)
                self.assertFalse(output.exists())

    def test_invalid_manifest_json_is_rejected_cleanly(self):
        for raw in (b"{", b"[" * 3000 + b"0" + b"]" * 3000):
            with self.subTest(raw=raw[:80]), self.temporary_directory() as directory:
                target = Path(directory) / "altered.zip"
                self.altered_manifest(target, lambda _manifest, raw=raw: raw)
                with self.assertRaisesRegex(ValueError, "manifest JSON"):
                    validate(target)

    def test_inconsistent_source_versions_are_rejected_before_creating_a_zip(self):
        for name, raw, message in (
            ("metadata.yaml", b"version: 9.9.9", "metadata"),
            ("package.json", b'{"version":"9.9.9"}', "package.json version"),
            (
                "package-lock.json",
                b'{"version":"9.9.9","packages":{"":{"version":"1.2.3"}}}',
                "package-lock.json version",
            ),
            (
                "package-lock.json",
                b'{"version":"1.2.3","packages":{"":{"version":"9.9.9"}}}',
                "root package version",
            ),
            (
                "core/dependency_manager.py",
                b'class DependencyManager:\n    USER_AGENT = "Dependency-Manager/9.9.9"\n',
                "USER_AGENT version",
            ),
            (
                "README.md",
                b"![Version](https://img.shields.io/badge/version-9.9.9-green)",
                "README version badge",
            ),
        ):
            with self.subTest(name=name, raw=raw), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                self.set_source_version(root, "1.2.3")
                (root / name).write_bytes(raw)
                output = Path(directory) / "output" / "candidate.zip"
                with self.assertRaisesRegex(ValueError, message):
                    build(root, output)
                self.assertFalse(output.exists())
                self.assertFalse(output.parent.exists())

    def test_invalid_package_version_structures_are_rejected_cleanly(self):
        for name, raw in (
            ("package.json", b"[]"),
            ("package.json", b"null"),
            ("package.json", b'{"version":"1.2.3","version":"1.2.3"}'),
            ("package-lock.json", b"true"),
            ("package-lock.json", b'{"version":"1.2.3"}'),
            ("package-lock.json", b'{"version":"1.2.3","packages":[]}'),
            ("package-lock.json", b'{"version":"1.2.3","packages":{"":[]}}'),
            ("package-lock.json", b'{"version":"1.2.3","packages":{"":null}}'),
            ("package-lock.json", b'{"version":"1.2.3","packages":{"":{"version":true}}}'),
        ):
            with self.subTest(name=name, raw=raw), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                self.set_source_version(root, "1.2.3")
                (root / name).write_bytes(raw)
                output = Path(directory) / "invalid.zip"
                with self.assertRaisesRegex(ValueError, name):
                    build(root, output)
                self.assertFalse(output.exists())

    def test_missing_package_files_and_version_declarations_are_rejected_cleanly(self):
        for name in VERSION_FILES:
            with self.subTest(name=name), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                source = self.source_bytes(root)
                del source[name]
                with self.assertRaisesRegex(ValueError, "Missing release version declaration"):
                    validate_versions(source)
                (root / name).unlink()
                output = Path(directory) / "invalid.zip"
                with self.assertRaisesRegex(ValueError, "Missing or unsafe"):
                    build(root, output)
                self.assertFalse(output.exists())

    def test_source_reads_one_bounded_snapshot_without_an_unbounded_read(self):
        for raw in (b"123", b"1234"):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.read.return_value = raw
            with (
                self.subTest(raw=raw),
                mock.patch.object(build_release, "MAX_ZIP_BYTES", 3),
                mock.patch.object(Path, "open", return_value=stream) as opened,
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")),
            ):
                if len(raw) == 3:
                    self.assertEqual(build_release._read_source(ROOT, "README.md"), raw)
                else:
                    with self.assertRaisesRegex(ValueError, "Runtime file exceeds"):
                        build_release._read_source(ROOT, "README.md")
                opened.assert_called_once_with("rb")
                stream.read.assert_called_once_with(4)

    def test_metadata_entrypoints_reject_oversize_before_validating_or_creating_a_zip(self):
        with (
            mock.patch.object(build_release, "MAX_ZIP_BYTES", 3),
            mock.patch.object(build_release, "build") as builder,
            mock.patch.object(build_release, "validate") as checker,
        ):
            with self.assertRaisesRegex(ValueError, "Runtime file exceeds.*metadata.yaml"):
                check_built(ROOT)
            with redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main([])
            self.assertEqual(raised.exception.code, 1)
            self.assertIn(
                "Runtime file exceeds release size limit: metadata.yaml", error.getvalue()
            )
            builder.assert_not_called()
            checker.assert_not_called()

    def test_duplicate_metadata_mapping_keys_are_rejected_before_creating_a_zip(self):
        for extra in (
            b"\nversion: 9.9.9\n",
            f"\nname: {PLUGIN}\n".encode(),
            f"\nrepo: {REPOSITORY}\n".encode(),
            b"\nextra:\n  duplicate: one\n  duplicate: two\n",
            b"\n? [one, two]\n: value\n",
        ):
            with self.subTest(extra=extra), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                path = root / "metadata.yaml"
                path.write_bytes(path.read_bytes() + extra)
                output = Path(directory) / "invalid.zip"
                with self.assertRaisesRegex(ValueError, "metadata YAML"):
                    build(root, output)
                self.assertFalse(output.exists())

    def test_user_agent_is_an_unambiguous_ast_literal_not_a_matching_decoy(self):
        for declaration in (
            'USER_AGENT = "AstrBot-YouTube-Music-Dependency-Manager/9.9.9"\n'
            '    DECOY = "AstrBot-YouTube-Music-Dependency-Manager/1.2.3"',
            'USER_AGENT = "AstrBot-YouTube-Music-Dependency-Manager/" + "1.2.3"',
            'USER_AGENT = "AstrBot-YouTube-Music-Dependency-Manager/1.2.3"\n'
            '    USER_AGENT = "AstrBot-YouTube-Music-Dependency-Manager/1.2.3"',
            "USER_AGENT: str",
        ):
            with self.subTest(declaration=declaration), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                self.set_source_version(root, "1.2.3")
                (root / "core/dependency_manager.py").write_text(
                    f"class DependencyManager:\n    {declaration}\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "USER_AGENT"):
                    validate_versions(self.source_bytes(root))
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            path = root / "core/dependency_manager.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace("USER_AGENT = ", "USER_AGENT: str = ", 1),
                encoding="utf-8",
            )
            self.assertEqual(validate_versions(self.source_bytes(root))["version"], "1.2.3")

    def test_missing_or_duplicate_readme_version_badges_are_rejected(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            version = metadata((root / "metadata.yaml").read_bytes())["version"]
            badge = f"![Version](https://img.shields.io/badge/version-{version}-green)"
            for text in ("# No badge", badge + "\n" + badge):
                with self.subTest(text=text):
                    (root / "README.md").write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "README version badge"):
                        validate_versions(self.source_bytes(root))

    def test_invalid_changelog_versions_dates_and_unarchived_changes_are_rejected(self):
        entry = "### Fixed\n\n- Synthetic release fixture.\n"
        for headings, message in (
            ("## [Unreleased]\n", "Latest CHANGELOG release version"),
            (f"## [1.2.2] - 2026-11-18\n{entry}", "Latest CHANGELOG release version"),
            (f"## [1.2.3]\n{entry}", "release date"),
            (f"## [1.2.3] - 2026-02-30\n{entry}", "release date"),
            (f"## [01.2.3] - 2026-11-18\n{entry}", "release version"),
            (
                f"## [1.2.3] - 2026-11-18\n{entry}\n## [1.2.3] - 2026-11-17\n",
                "Duplicate CHANGELOG",
            ),
            (
                f"## [1.2.3] - 2026-11-18\n{entry}\n## [1.2.1] - 2026-11-17\n",
                "without skipping",
            ),
            (
                f"## [1.2.3] - 2026-11-18\n{entry}\n## [1.2.4] - 2026-11-17\n",
                "without skipping",
            ),
            (
                f"## [1.2.3] - 2026-11-16\n{entry}\n## [1.2.2] - 2026-11-17\n",
                "dates are out of order",
            ),
            ("## [1.2.3] - 2026-11-18\n\n### Fixed\n", "contain actual changes"),
            (
                f"## [Unreleased]\n\n- Unarchived fix.\n\n## [1.2.3] - 2026-11-18\n{entry}",
                "must be archived",
            ),
            (
                f"## [Unreleased]\n\nPending change.\n\n## [1.2.3] - 2026-11-18\n{entry}",
                "must be archived",
            ),
            (
                f"## [Unreleased]\n## [Unreleased]\n## [1.2.3] - 2026-11-18\n{entry}",
                "Unreleased heading",
            ),
        ):
            with self.subTest(headings=headings), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                self.set_source_version(root, "1.2.3", f"# Changelog\n\n{headings}")
                output = Path(directory) / "output" / "invalid.zip"
                with self.assertRaisesRegex(ValueError, message):
                    build(root, output)
                self.assertFalse(output.exists())
                self.assertFalse(output.parent.exists())

    def test_changelog_allows_single_step_patch_minor_major_and_same_day_releases(self):
        for newer, older in (("1.2.3", "1.2.2"), ("1.3.0", "1.2.9"), ("2.0.0", "1.9.9")):
            with self.subTest(newer=newer, older=older), self.temporary_directory() as directory:
                root = self.copy_source(directory)
                self.set_source_version(
                    root,
                    newer,
                    "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n"
                    f"## [{newer}] - 2026-11-18\n\n- Current fix.\n\n"
                    f"## [{older}] - 2026-11-18\n",
                )
                self.assertEqual(validate_versions(self.source_bytes(root))["version"], newer)

    def test_changelog_version_validation_accepts_lf_and_crlf_without_changing_source_bytes(self):
        source = self.source_bytes(ROOT)
        text = source["CHANGELOG.md"].decode("utf-8").replace("\r\n", "\n")
        version = metadata(source["metadata.yaml"])["version"]
        for ending in ("\n", "\r\n"):
            with self.subTest(ending=repr(ending)):
                raw = text.replace("\n", ending).encode("utf-8")
                candidate = {**source, "CHANGELOG.md": raw}
                self.assertEqual(validate_versions(candidate)["version"], version)
                self.assertEqual(candidate["CHANGELOG.md"], raw)

    def test_check_built_derives_standard_zip_path_from_current_metadata(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            target = root / "dist" / f"{PLUGIN}-v1.2.3.zip"
            build(root, target)
            self.assertEqual(check_built(root)["path"], str(target.resolve()))
            with (
                mock.patch.object(build_release, "ROOT", root),
                redirect_stdout(io.StringIO()) as out,
            ):
                result = main(["--check-built"])
            self.assertEqual(json.loads(out.getvalue()), result)
            self.assertEqual(result["version"], "1.2.3")
            self.assertFalse(result["source_verified"])

    def test_old_zip_integrity_check_is_independent_of_current_version_declarations(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "0.1.0")
            old = Path(directory) / f"{PLUGIN}-v0.1.0.zip"
            build(root, old)
            self.set_source_version(root, "0.1.1")
            (root / "package.json").write_bytes(b"invalid current package JSON")
            self.assertEqual(validate(old, root=root)["version"], "0.1.0")
            with (
                mock.patch.object(build_release, "ROOT", root),
                redirect_stdout(io.StringIO()) as out,
            ):
                main(["--check", str(old)])
            self.assertEqual(json.loads(out.getvalue())["version"], "0.1.0")
            current = root / "dist" / f"{PLUGIN}-v0.1.1.zip"
            current.parent.mkdir()
            shutil.copyfile(old, current)
            with self.assertRaisesRegex(ValueError, "Built ZIP version"):
                check_built(root)

    def test_cli_rejects_conflicting_modes_and_ref_without_check_before_building(self):
        for arguments in (["--check", "anything.zip", "--check-built"], ["--ref", "v1.2.3"]):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(build_release, "build") as builder,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 2)
                builder.assert_not_called()

    def test_tag_verification_reads_all_raw_git_blobs_and_reports_resolved_commit(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            target = root / "dist" / f"{PLUGIN}-v1.2.3.zip"
            build(root, target)
            for commit in ("a" * 40, "b" * 64):
                with self.subTest(commit=commit), self.git_source(root, commit=commit) as reader:
                    result = check_built(root, ref="v1.2.3")
                self.assertEqual(
                    {key: result[key] for key in ("tag", "source_commit", "source_verified")},
                    {"tag": "v1.2.3", "source_commit": commit, "source_verified": True},
                )
                self.assertEqual(
                    {
                        call.args[3].split(":", 1)[1]
                        for call in reader.call_args_list
                        if call.args[1:3] == ("cat-file", "blob")
                    },
                    {*FILES, *VERSION_FILES},
                )
            with (
                self.git_source(root),
                mock.patch.object(build_release, "ROOT", root),
                redirect_stdout(io.StringIO()) as out,
            ):
                main(["--check", str(target), "--ref", "v1.2.3"])
            self.assertTrue(json.loads(out.getvalue())["source_verified"])
            with (
                self.git_source(root),
                mock.patch.object(build_release, "ROOT", root),
                redirect_stdout(io.StringIO()) as out,
            ):
                main(["--check-built", "--ref", "v1.2.3"])
            self.assertEqual(json.loads(out.getvalue())["tag"], "v1.2.3")
            with self.git_source(root), mock.patch.object(build_release, "MAX_SOURCE_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "Total Git release source"):
                    validate(target, root=root, ref="v1.2.3")

    def test_old_tag_verification_uses_tag_versions_not_current_worktree_versions(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "0.1.0")
            target = Path(directory) / f"{PLUGIN}-v0.1.0.zip"
            build(root, target)
            snapshot = self.source_bytes(root)
            self.set_source_version(root, "0.1.1")
            with self.git_source(root, source=snapshot):
                result = validate(target, root=root, ref="v0.1.0")
            self.assertEqual(result["version"], "0.1.0")
            self.assertTrue(result["source_verified"])

    def test_invalid_ref_mismatched_version_and_nonstandard_zip_name_are_rejected(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            standard = Path(directory) / f"{PLUGIN}-v1.2.3.zip"
            custom = Path(directory) / "custom.zip"
            build(root, standard)
            shutil.copyfile(standard, custom)
            for target, ref, message in (
                (standard, "main", "exact vX.Y.Z tag"),
                (standard, "a" * 40, "exact vX.Y.Z tag"),
                (standard, "v01.2.3", "exact vX.Y.Z tag"),
                (standard, "v1.2.3^{commit}", "exact vX.Y.Z tag"),
                (standard, "v1.2.3-preview", "exact vX.Y.Z tag"),
                (standard, "v1.2.4", "tag version"),
                (custom, "v1.2.3", "standard release ZIP filename"),
            ):
                with (
                    self.subTest(ref=ref, target=target),
                    mock.patch.object(build_release, "_git_bytes") as reader,
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        validate(target, root=root, ref=ref)
                    reader.assert_not_called()

    def test_invalid_git_commit_identity_and_wrong_repository_root_are_rejected(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            target = Path(directory) / f"{PLUGIN}-v1.2.3.zip"
            build(root, target)
            for commit in ("", "g" * 40, "a" * 41, "a" * 40 + "\nother"):
                with self.subTest(commit=commit), self.git_source(root, commit=commit):
                    with self.assertRaisesRegex(ValueError, "Git source commit identity"):
                        validate(target, root=root, ref="v1.2.3")
            with self.git_source(root, repository=root / "not-the-repository"):
                with self.assertRaisesRegex(ValueError, "repository root"):
                    validate(target, root=root, ref="v1.2.3")

    def test_tag_source_metadata_and_nonpackaged_versions_must_match_the_tag(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            target = Path(directory) / f"{PLUGIN}-v1.2.3.zip"
            build(root, target)
            for name, raw in (
                ("package.json", b'{"version":"1.2.4"}'),
                ("package-lock.json", b'{"version":"1.2.3","packages":{"":null}}'),
            ):
                snapshot = self.source_bytes(root)
                snapshot[name] = raw
                with self.subTest(name=name), self.git_source(root, source=snapshot):
                    with self.assertRaisesRegex(ValueError, name):
                        validate(target, root=root, ref="v1.2.3")
            self.set_source_version(root, "1.2.4")
            with self.git_source(root):
                with self.assertRaisesRegex(ValueError, "tag version.*Git source metadata"):
                    validate(target, root=root, ref="v1.2.3")

    def test_self_consistent_modified_zip_is_rejected_when_git_source_bytes_differ(self):
        with self.temporary_directory() as directory:
            root = self.copy_source(directory)
            self.set_source_version(root, "1.2.3")
            original = Path(directory) / "original.zip"
            target = Path(directory) / f"{PLUGIN}-v1.2.3.zip"
            build(root, original)
            with zipfile.ZipFile(original) as archive:
                payload = {name: archive.read(name) for name in archive.namelist()}
            payload["main.py"] += b"\n# Synthetic source tampering fixture.\n"
            manifest = json.loads(payload["RELEASE_MANIFEST.json"])
            for row in manifest["files"]:
                data = payload[row["path"]]
                row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            payload["RELEASE_MANIFEST.json"] = json.dumps(manifest).encode()
            with zipfile.ZipFile(target, "w") as archive:
                for name, data in payload.items():
                    archive.writestr(name, data)
            self.assertFalse(validate(target)["source_verified"])
            with self.git_source(root):
                with self.assertRaisesRegex(ValueError, "source bytes.*main.py"):
                    validate(target, root=root, ref="v1.2.3")

    def test_git_invocation_is_read_only_bounded_and_does_not_use_a_shell(self):
        response = subprocess.CompletedProcess([], 0, stdout=b"\x00raw\r\nbytes\xff", stderr=b"")
        with (
            mock.patch.dict(
                build_release.os.environ,
                {"GIT_DIR": "unrelated", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "redirect"},
            ),
            mock.patch.object(build_release.subprocess, "run", return_value=response) as run,
        ):
            result = build_release._git_bytes(ROOT, "cat-file", "blob", "a" * 40 + ":main.py")
        self.assertEqual(result, response.stdout)
        arguments, options = run.call_args
        self.assertEqual(
            arguments[0],
            [
                "git",
                "--no-replace-objects",
                "--no-lazy-fetch",
                "cat-file",
                "blob",
                "a" * 40 + ":main.py",
            ],
        )
        self.assertEqual(options["cwd"], ROOT)
        self.assertEqual(options["timeout"], build_release.GIT_TIMEOUT)
        self.assertFalse(options["shell"])
        self.assertTrue(options["check"])
        self.assertNotIn("GIT_DIR", options["env"])
        self.assertNotIn("GIT_CONFIG_KEY_0", options["env"])
        self.assertEqual(options["env"]["GIT_NO_LAZY_FETCH"], "1")

    def test_missing_tag_git_failures_and_timeouts_are_reported_as_clean_value_errors(self):
        for error in (
            OSError("Git unavailable"),
            subprocess.CalledProcessError(128, ["git", "rev-parse"]),
            subprocess.TimeoutExpired(["git", "rev-parse"], build_release.GIT_TIMEOUT),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(build_release.subprocess, "run", side_effect=error),
            ):
                with self.assertRaisesRegex(ValueError, "Cannot read local Git"):
                    build_release._git_bytes(
                        ROOT, "rev-parse", "--verify", "refs/tags/v1.2.3^{commit}"
                    )

    def test_git_blob_sizes_are_checked_before_reading_bytes(self):
        for size in (
            b"-1",
            b"not-a-size",
            b"9" * 11,
            str(build_release.MAX_ZIP_BYTES + 1).encode(),
        ):
            with (
                self.subTest(size=size),
                mock.patch.object(build_release, "_git_bytes", return_value=size) as reader,
            ):
                with self.assertRaisesRegex(ValueError, "Invalid or oversized Git release source"):
                    build_release._git_blob(ROOT, "a" * 40, "main.py")
                self.assertEqual(reader.call_count, 1)
        with mock.patch.object(build_release, "_git_bytes", side_effect=[b"3\n", b"four"]):
            with self.assertRaisesRegex(ValueError, "Git release source size mismatch"):
                build_release._git_blob(ROOT, "a" * 40, "main.py")

    def test_zip_digest_describes_the_same_bounded_snapshot_that_was_validated(self):
        with self.temporary_directory() as directory:
            target = Path(directory) / "candidate.zip"
            build(ROOT, target)
            raw = target.read_bytes()
            archive_type = zipfile.ZipFile

            def changed_after_read(source, *args, **kwargs):
                target.write_bytes(b"Changed after the validation snapshot was read.")
                return archive_type(source, *args, **kwargs)

            with mock.patch.object(
                build_release.zipfile, "ZipFile", side_effect=changed_after_read
            ):
                result = validate(target)
            self.assertEqual(result["bytes"], len(raw))
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertNotEqual(target.read_bytes(), raw)
