"""Build a reproducible installable ZIP; never publish or overwrite it."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.persistence import strict_json_loads  # noqa: E402

PLUGIN = "astrbot_plugin_youtube_music"
REPOSITORY = "https://github.com/pakhozako/astrbot_plugin_youtube_music"
DOCS_COMMIT = "2d3384f1d6cdb272c686065cb6c29fe122682f59"
MAX_ZIP_BYTES = 16_000_000
MANIFEST = "RELEASE_MANIFEST.json"
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
VERSION_FILES = ("package.json", "package-lock.json")
VERSION_DECLARATIONS = (
    "metadata.yaml",
    "README.md",
    "CHANGELOG.md",
    "core/dependency_manager.py",
    *VERSION_FILES,
)
MAX_SOURCE_BYTES = MAX_ZIP_BYTES * (1 + len(VERSION_FILES))
GIT_TIMEOUT = 10
FILES = (
    "__init__.py",
    "main.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "docs/index.md",
    "docs/INSTALLATION.md",
    "docs/USAGE.md",
    "docs/CONFIGURATION.md",
    "docs/DEPENDENCIES.md",
    "docs/TROUBLESHOOTING.md",
    "docs/images/desktop-search.png",
    "docs/images/desktop-library.png",
    "docs/images/tablet-maintenance.png",
    "docs/images/mobile-queue.png",
    "core/__init__.py",
    "core/ai_tools.py",
    "core/audio_cache.py",
    "core/dependency_manager.py",
    "core/errors.py",
    "core/library.py",
    "core/maintenance.py",
    "core/persistence.py",
    "core/process.py",
    "core/queue.py",
    "core/search_quality.py",
    "core/search_recall.py",
    "core/service.py",
    "core/settings.py",
    "core/state.py",
    "core/store_validation.py",
    "core/utils.py",
    "core/webui.py",
    "core/windows_job.py",
    "core/vendor/yt-dlp-public.key",
    "views/console/index.html",
    "views/console/style.css",
    "views/console/app.css",
    "views/console/app.css.LEGAL.txt",
    "views/console/app.js",
    "views/console/live-connection.mjs",
    ".astrbot-plugin/i18n/zh-CN.json",
    ".astrbot-plugin/i18n/en-US.json",
    "views/console/licenses/material-MIT.txt",
    "views/console/licenses/material-NOTICE.txt",
    "views/console/licenses/material-symbols-Apache-2.0.txt",
)


class _MetadataLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError:
                raise ValueError("Invalid plugin metadata YAML mapping key") from None
            if duplicate:
                raise ValueError("Duplicate plugin metadata YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise ValueError("Invalid release semantic version")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        raise ValueError("Invalid release semantic version") from None


def metadata(raw: bytes):
    try:
        data = yaml.load(raw, Loader=_MetadataLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise ValueError("Invalid plugin metadata YAML") from None
    if not isinstance(data, dict) or data.get("name") != PLUGIN:
        raise ValueError("Invalid plugin metadata")
    _version_parts(data.get("version"))
    for key in ("display_name", "desc", "author"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Missing metadata field: {key}")
    if data.get("support_platforms") != ["aiocqhttp"]:
        raise ValueError("Unexpected platform support declaration")
    if data.get("astrbot_version") != ">=4.29.4,<5":
        raise ValueError("Unexpected AstrBot version declaration")
    if data.get("repo") != REPOSITORY:
        raise ValueError("Unexpected plugin repository URL")
    return data


def _validate_changelog(raw: bytes, version: str):
    text = raw.decode("utf-8").replace("\r\n", "\n")
    headings = list(re.finditer(r"^## \[([^\]\r\n]+)\]([^\r\n]*)$", text, re.M))
    releases = []
    unreleased = False
    for index, heading in enumerate(headings):
        label, suffix = heading.groups()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end() : end]
        if label == "Unreleased":
            if unreleased or releases or suffix.strip():
                raise ValueError("Invalid CHANGELOG Unreleased heading")
            unreleased = True
            if any(
                line.strip()
                and not re.fullmatch(
                    r"### (?:Added|Changed|Deprecated|Removed|Fixed|Security)", line.strip()
                )
                for line in body.splitlines()
            ):
                raise ValueError("CHANGELOG Unreleased changes must be archived before release")
            continue
        try:
            parts = _version_parts(label)
        except ValueError:
            raise ValueError("Invalid CHANGELOG release version") from None
        match = re.fullmatch(r" - ([0-9]{4}-[0-9]{2}-[0-9]{2})", suffix)
        if match is None:
            raise ValueError("Missing or invalid CHANGELOG release date")
        try:
            released = date.fromisoformat(match[1])
        except ValueError:
            raise ValueError("Invalid CHANGELOG release date") from None
        releases.append((label, parts, released, body))
    if not releases or releases[0][0] != version:
        raise ValueError("Latest CHANGELOG release version does not match metadata")
    if not re.search(r"^[-*] \S", releases[0][3], re.M):
        raise ValueError("Latest CHANGELOG release must contain actual changes")
    if len({release[0] for release in releases}) != len(releases):
        raise ValueError("Duplicate CHANGELOG release version")
    for newer, older in zip(releases, releases[1:], strict=False):
        major, minor, patch = older[1]
        if newer[1] not in (
            (major, minor, patch + 1),
            (major, minor + 1, 0),
            (major + 1, 0, 0),
        ):
            raise ValueError("CHANGELOG release versions must increase without skipping versions")
        if newer[2] < older[2]:
            raise ValueError("CHANGELOG release dates are out of order")


def validate_versions(source):
    """Check version declarations without importing or executing release source."""
    try:
        meta = metadata(source["metadata.yaml"])
        version = meta["version"]
        for name in VERSION_FILES:
            try:
                package = strict_json_loads(source[name])
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError(f"Invalid {name} version declaration JSON") from None
            if not isinstance(package, dict) or package.get("version") != version:
                raise ValueError(f"{name} version does not match metadata")
            if name == "package-lock.json":
                packages = package.get("packages")
                primary = packages.get("") if isinstance(packages, dict) else None
                if not isinstance(primary, dict) or primary.get("version") != version:
                    raise ValueError(
                        "package-lock.json root package version does not match metadata"
                    )
        tree = ast.parse(
            source["core/dependency_manager.py"].decode("utf-8"),
            filename="core/dependency_manager.py",
        )
        values = []
        classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DependencyManager"
        ]
        if len(classes) != 1:
            raise ValueError("Missing or ambiguous DependencyManager class")
        for node in classes[0].body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(
                isinstance(target, ast.Name) and target.id == "USER_AGENT" for target in targets
            ):
                value = node.value
                values.append(value.value if isinstance(value, ast.Constant) else None)
        if values != [f"AstrBot-YouTube-Music-Dependency-Manager/{version}"]:
            raise ValueError("DependencyManager USER_AGENT version does not match metadata")
        badges = re.findall(
            r"!\[Version\]\(https://img\.shields\.io/badge/version-([^-()\s/]+)-[^()\s]+\)",
            source["README.md"].decode("utf-8"),
        )
        if badges != [version]:
            raise ValueError("README version badge does not match metadata")
        _validate_changelog(source["CHANGELOG.md"], version)
    except KeyError as error:
        raise ValueError(f"Missing release version declaration: {error.args[0]}") from None
    except UnicodeError:
        raise ValueError("Invalid UTF-8 release version declaration") from None
    return meta


def _read_source(root: Path, name: str):
    source = root / name
    source.resolve().relative_to(root)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Missing or unsafe runtime file: {name}")
    with source.open("rb") as stream:
        raw = stream.read(MAX_ZIP_BYTES + 1)
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError(f"Runtime file exceeds release size limit: {name}")
    return raw


def build(root: Path, target: Path):
    root = root.resolve()
    payload = {}
    total_bytes = 0
    for name in sorted(FILES):
        payload[name] = _read_source(root, name)
        total_bytes += len(payload[name])
        if total_bytes > MAX_ZIP_BYTES:
            raise ValueError("Uncompressed release exceeds size limit")
    meta = validate_versions(
        {**payload, **{name: _read_source(root, name) for name in VERSION_FILES}}
    )
    manifest = {
        "schema_version": 1,
        "name": PLUGIN,
        "version": meta["version"],
        "repository": meta["repo"],
        "target_astrbot": "4.29.4",
        "official_docs_commit": DOCS_COMMIT,
        "files": [
            {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in payload.items()
        ],
    }
    payload[MANIFEST] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if sum(len(data) for data in payload.values()) > MAX_ZIP_BYTES:
        raise ValueError("Uncompressed release exceeds size limit")
    _validate_payload(payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, data in sorted(payload.items()):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data, compresslevel=9)
    return validate(target)


def _validate_payload(payload):
    meta = metadata(payload["metadata.yaml"])
    try:
        manifest = strict_json_loads(payload[MANIFEST])
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid release manifest JSON") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema_version",
            "name",
            "version",
            "repository",
            "target_astrbot",
            "official_docs_commit",
            "files",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("repository") != meta["repo"]
        or manifest.get("version") != meta["version"]
        or manifest.get("name") != PLUGIN
        or manifest.get("target_astrbot") != "4.29.4"
        or manifest.get("official_docs_commit") != DOCS_COMMIT
    ):
        raise ValueError("Manifest identity/version does not match metadata")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(FILES):
        raise ValueError("Invalid manifest file count")
    for row in records:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "bytes", "sha256"}
            or not isinstance(row["path"], str)
            or type(row["bytes"]) is not int
            or row["bytes"] < 0
            or not isinstance(row["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
        ):
            raise ValueError("Invalid manifest file record")
    if {row["path"] for row in records} != set(FILES):
        raise ValueError("Manifest file set does not match ZIP")
    for row in records:
        data = payload[row["path"]]
        if row["sha256"] != hashlib.sha256(data).hexdigest() or row["bytes"] != len(data):
            raise ValueError(f"Manifest digest/size mismatch: {row['path']}")
    for name, data in payload.items():
        if name.endswith(".py"):
            ast.parse(data.decode("utf-8"), filename=name)
        if name.endswith(".json"):
            try:
                strict_json_loads(data)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError(f"Invalid runtime JSON: {name}") from None
    return meta


def _git_bytes(root: Path, *arguments: str):
    # Ignore redirected repositories and prohibit replacement objects or lazy network fetches.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "--no-lazy-fetch", *arguments],
            cwd=root,
            env=environment,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"Cannot read local Git tag/source objects: {arguments[0]}") from None
    return result.stdout


def _git_blob(root: Path, commit: str, name: str):
    object_name = f"{commit}:{name}"
    raw_size = _git_bytes(root, "cat-file", "-s", object_name).strip()
    if not re.fullmatch(rb"[0-9]{1,10}", raw_size) or int(raw_size) > MAX_ZIP_BYTES:
        raise ValueError(f"Invalid or oversized Git release source: {name}")
    data = _git_bytes(root, "cat-file", "blob", object_name)
    if len(data) != int(raw_size):
        raise ValueError(f"Git release source size mismatch: {name}")
    return data


def _verify_source(path: Path, payload, root: Path, ref: str, version: str):
    if not isinstance(ref, str) or not re.fullmatch(rf"v{VERSION}", ref):
        raise ValueError("Release ref must be an exact vX.Y.Z tag")
    if ref[1:] != version:
        raise ValueError("Release tag version does not match ZIP metadata")
    if path.name != f"{PLUGIN}-{ref}.zip":
        raise ValueError("Tag verification requires the standard release ZIP filename")
    root = root.resolve()
    try:
        repository = _git_bytes(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
        commit = (
            _git_bytes(root, "rev-parse", "--verify", f"refs/tags/{ref}^{{commit}}")
            .decode("ascii")
            .strip()
        )
    except UnicodeError:
        raise ValueError("Invalid Git repository or commit identity") from None
    if not repository or not Path(repository).is_absolute() or Path(repository).resolve() != root:
        raise ValueError("Git repository root does not match release source root")
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit):
        raise ValueError("Invalid Git source commit identity")
    source = {"metadata.yaml": _git_blob(root, commit, "metadata.yaml")}
    if metadata(source["metadata.yaml"])["version"] != ref[1:]:
        raise ValueError("Release tag version does not match Git source metadata")
    total_bytes = 0
    for name in (*FILES, *VERSION_FILES):
        data = source[name] if name in source else _git_blob(root, commit, name)
        total_bytes += len(data)
        if total_bytes > MAX_SOURCE_BYTES:
            raise ValueError("Total Git release source exceeds size limit")
        if name in payload and data != payload[name]:
            raise ValueError(f"ZIP source bytes do not match tag commit: {name}")
        if name in VERSION_DECLARATIONS:
            source[name] = data
    validate_versions(source)
    return {"tag": ref, "source_commit": commit, "source_verified": True}


def validate(path: Path, *, ref: str | None = None, root: Path = ROOT):
    with path.open("rb") as source:
        raw = source.read(MAX_ZIP_BYTES)
    if len(raw) >= MAX_ZIP_BYTES:
        raise ValueError("Release exceeds conservative 16 MB ZIP limit")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(name.casefold() for name in names)):
            raise ValueError("Duplicate ZIP entry")
        expected = {*FILES, MANIFEST}
        if set(names) != expected:
            raise ValueError("ZIP contents differ from the explicit runtime allowlist")
        if sum(item.file_size for item in archive.infolist()) > MAX_ZIP_BYTES:
            raise ValueError("Uncompressed release exceeds size limit")
        for item in archive.infolist():
            if stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG):
                raise ValueError("ZIP contains a non-regular runtime file")
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC verification failed")
        payload = {name: archive.read(name) for name in names}
    meta = _validate_payload(payload)
    result = {
        "path": str(path.resolve()),
        "version": meta["version"],
        "entries": len(names),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "repository": meta["repo"],
        "source_verified": False,
        "validation_scope": "Package integrity only; not a live QQ or production acceptance test",
    }
    if ref is not None:
        result.update(_verify_source(path, payload, root, ref, meta["version"]))
        result["validation_scope"] = (
            "Package integrity and exact local tag source bytes; "
            "not a tag signature, live QQ or production acceptance test"
        )
    return result


def check_built(root: Path = ROOT, *, ref: str | None = None):
    root = root.resolve()
    version = metadata(_read_source(root, "metadata.yaml"))["version"]
    target = root / "dist" / f"{PLUGIN}-v{version}.zip"
    result = validate(target, ref=ref, root=root)
    if result["version"] != version:
        raise ValueError("Built ZIP version does not match current metadata")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", type=Path, help="Validate an existing local ZIP independently")
    checks.add_argument(
        "--check-built", action="store_true", help="Validate dist ZIP named by current metadata"
    )
    parser.add_argument("--ref", help="Also prove ZIP source bytes match a local vX.Y.Z tag")
    args = parser.parse_args(argv)
    if args.ref is not None and args.check is None and not args.check_built:
        parser.error("--ref requires --check or --check-built")
    try:
        if args.check is not None:
            result = validate(args.check, ref=args.ref, root=ROOT)
        elif args.check_built:
            result = check_built(ROOT, ref=args.ref)
        else:
            version = metadata(_read_source(ROOT, "metadata.yaml"))["version"]
            result = build(ROOT, ROOT / "dist" / f"{PLUGIN}-v{version}.zip")
    except (ValueError, OSError, SyntaxError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
