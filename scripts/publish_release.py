"""Publish a verified tag as a draft first; never replace releases or assets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.persistence import strict_json_loads  # noqa: E402
from scripts.build_release import (  # noqa: E402
    MAX_ZIP_BYTES,
    REPOSITORY,
    _git_blob,
    _git_bytes,
    check_built,
    validate,
)
from scripts.check_history import check_history  # noqa: E402

SLUG = REPOSITORY.removeprefix("https://github.com/")
GH_TIMEOUT = 180


def _gh_environment():
    environment = dict(os.environ)
    environment.pop("GH_REPO", None)
    environment.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    return environment


def _gh(root: Path, *arguments: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            cwd=root,
            env=_gh_environment(),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GH_TIMEOUT,
            check=True,
            shell=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ValueError(f"GitHub operation failed: {arguments[0]}") from None
    return result.stdout


def _json_response(raw):
    try:
        return strict_json_loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Cannot read a valid GitHub API response") from None


def _api(
    root: Path,
    endpoint: str,
    *,
    paginated: bool = False,
    method: str = "GET",
    payload=None,
):
    arguments = ["api", "--hostname", "github.com", "--header", "Cache-Control: no-cache"]
    if paginated:
        arguments.extend(("--paginate", "--slurp"))
    if method != "GET":
        arguments.extend(("--method", method))
    arguments.append(f"repos/{SLUG}/{endpoint}")
    if payload is not None:
        arguments.extend(("--input", "-"))
    try:
        return _json_response(
            _gh(root, *arguments, input_text=json.dumps(payload) if payload is not None else None)
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Cannot read a valid GitHub API response") from None


def _verify_remote(root: Path, tag: str, commit: str, tag_object: str) -> None:
    for ref, expected, kind in (
        (f"tags/{tag}", tag_object, "tag"),
        ("heads/main", commit, "commit"),
    ):
        record = _api(root, f"git/ref/{ref}")
        remote = record.get("object") if isinstance(record, dict) else None
        if (
            not isinstance(remote, dict)
            or remote.get("type") != kind
            or remote.get("sha") != expected
        ):
            raise ValueError(f"Remote {ref} does not match the verified source")


def _inventory(root: Path):
    pages = _api(root, "releases?per_page=100", paginated=True)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Cannot verify the existing release inventory")
    records = [record for page in pages for record in page]
    if any(
        not isinstance(record, dict)
        or not isinstance(record.get("tag_name"), str)
        or not record["tag_name"].strip()
        for record in records
    ):
        raise ValueError("Cannot verify the existing release inventory")
    return records


def _verify_identity(record, tag: str, commit: str, *, draft: bool):
    if (
        not isinstance(record, dict)
        or record.get("tag_name") != tag
        or record.get("target_commitish") != commit
        or record.get("draft") is not draft
        or record.get("prerelease") is not False
        or type(record.get("id")) is not int
        or record["id"] <= 0
        or not isinstance(record.get("html_url"), str)
        or (
            not record["html_url"].startswith(f"{REPOSITORY}/releases/")
            if draft
            else record["html_url"] != f"{REPOSITORY}/releases/tag/{tag}"
        )
    ):
        raise ValueError("Release identity or publication state does not match the tag")
    return record["id"]


def _verify_asset(asset, package):
    if (
        not isinstance(asset, dict)
        or type(asset.get("id")) is not int
        or asset["id"] <= 0
        or asset.get("name") != Path(package["path"]).name
        or type(asset.get("size")) is not int
        or asset.get("size") != package["bytes"]
        or asset.get("state") != "uploaded"
        or asset.get("digest") != f"sha256:{package['sha256']}"
    ):
        raise ValueError("Release asset size or SHA-256 does not match the verified ZIP")
    return asset["id"]


def _verify_record(record, package, tag: str, commit: str, *, draft: bool):
    release_id = _verify_identity(record, tag, commit, draft=draft)
    assets = record.get("assets")
    if not isinstance(assets, list) or len(assets) != 1:
        raise ValueError("Release must contain exactly one verified installable ZIP")
    _verify_asset(assets[0], package)
    return release_id


def _upload_asset(root: Path, release_id: int, package):
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("Invalid GitHub release identity for asset upload")
    query = urlencode({"name": Path(package["path"]).name})
    endpoint = f"https://uploads.github.com/repos/{SLUG}/releases/{release_id}/assets?{query}"
    return _json_response(
        _gh(
            root,
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST",
            endpoint,
            "--header",
            "Content-Type: application/zip",
            "--input",
            package["path"],
        )
    )


async def _stream_asset(root: Path, asset_id: int, target: Path, expected_bytes: int):
    process = None
    try:
        with target.open("xb") as output:
            process = await asyncio.create_subprocess_exec(
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--header",
                "Accept: application/octet-stream",
                f"repos/{SLUG}/releases/assets/{asset_id}",
                cwd=root,
                env=_gh_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            received = 0
            while block := await process.stdout.read(min(65_536, expected_bytes - received + 1)):
                if received + len(block) > expected_bytes:
                    raise ValueError("Downloaded release asset has an unexpected size")
                output.write(block)
                received += len(block)
            if await process.wait() != 0:
                raise ValueError("GitHub release asset download failed")
            if received != expected_bytes:
                raise ValueError("Downloaded release asset has an unexpected size")
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.communicate(), timeout=5)


def _download_asset(root: Path, asset_id: int, target: Path, expected_bytes: int):
    if (
        type(asset_id) is not int
        or asset_id <= 0
        or type(expected_bytes) is not int
        or not 0 < expected_bytes < MAX_ZIP_BYTES
    ):
        raise ValueError("Invalid GitHub release asset download identity or size")

    async def run():
        await asyncio.wait_for(
            _stream_asset(root, asset_id, target, expected_bytes), timeout=GH_TIMEOUT
        )

    try:
        asyncio.run(run())
    except (OSError, TimeoutError, subprocess.SubprocessError):
        raise ValueError("GitHub release asset download failed") from None


def _notes(root: Path, package, tag: str, tag_object: str) -> str:
    text = _git_blob(root, package["source_commit"], "CHANGELOG.md").decode("utf-8")
    text = text.replace("\r\n", "\n")
    section = re.search(
        rf"^## \[{re.escape(package['version'])}\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\n"
        r"(.*?)(?=^## \[|^\[[^\]]+\]:|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if section is None:
        raise ValueError("Cannot extract the verified release CHANGELOG section")
    return (
        f"{section[1].strip()}\n\n"
        "## Source And Package\n\n"
        f"- Tag: `{tag}`\n"
        f"- Source commit: `{package['source_commit']}`\n"
        f"- Tag object: `{tag_object}`\n"
        f"- ZIP: `{Path(package['path']).name}`\n"
        f"- ZIP SHA-256: `{package['sha256']}`\n\n"
        "Package integrity and exact tag source bytes are verified. "
        "Offline tests do not prove live YouTube downloads, QQ delivery or production acceptance.\n"
    )


def _verification_directory(root: Path) -> Path:
    directory = root / ".verification"
    try:
        if (
            directory.is_symlink()
            or directory.is_junction()
            or directory.exists()
            and not directory.is_dir()
        ):
            raise ValueError
        resolved = directory.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        raise ValueError("Unsafe local release verification directory") from None
    return resolved


def publish(root: Path = ROOT, *, tag: str, dry_run: bool = False):
    root = root.resolve()
    history = check_history(root, tag=tag)
    package = check_built(root, ref=tag)
    commit, tag_object = package["source_commit"], history["tag_object"]
    if history["head"] != commit:
        raise ValueError("The release tag must point to the checked-out source")
    if _git_bytes(root, "status", "--porcelain").strip():
        raise ValueError("Release publication requires a clean source worktree")
    origin = _git_bytes(root, "remote", "get-url", "--push", "origin").decode().strip()
    if origin not in {
        f"{REPOSITORY}.git",
        REPOSITORY,
        f"git@github.com:{SLUG}.git",
        f"ssh://git@github.com/{SLUG}.git",
    }:
        raise ValueError("The origin repository does not match plugin metadata")
    _verification_directory(root)
    _verify_remote(root, tag, commit, tag_object)
    if any(record["tag_name"] == tag for record in _inventory(root)):
        raise ValueError("Release already exists; nothing overwritten")
    notes = _notes(root, package, tag, tag_object)
    if dry_run:
        return {**package, "tag_object": tag_object, "publication": "dry-run"}
    created = _api(
        root,
        "releases",
        method="POST",
        payload={
            "tag_name": tag,
            "target_commitish": commit,
            "name": tag,
            "body": notes,
            "draft": True,
            "prerelease": False,
            "make_latest": "false",
        },
    )
    # The creation response is authoritative even when draft listing is temporarily stale.
    draft_id = _verify_identity(created, tag, commit, draft=True)
    if created.get("assets") != []:
        raise ValueError("The newly created draft release must have no assets")
    endpoint = f"releases/{draft_id}"
    asset_id = _verify_asset(_upload_asset(root, draft_id, package), package)
    uploaded = _api(root, endpoint)
    if (
        _verify_record(uploaded, package, tag, commit, draft=True) != draft_id
        or uploaded["assets"][0]["id"] != asset_id
    ):
        raise ValueError("Uploaded asset does not belong to the verified draft")
    verification = _verification_directory(root)
    verification.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-download-", dir=verification) as directory:
        target = Path(directory) / Path(package["path"]).name
        _download_asset(
            root,
            asset_id,
            target,
            package["bytes"],
        )
        downloaded = validate(target, ref=tag, root=root)
        if downloaded["sha256"] != package["sha256"]:
            raise ValueError("Downloaded draft ZIP differs from the local verified ZIP")
    _verify_remote(root, tag, commit, tag_object)
    ready = _api(root, endpoint)
    if (
        _verify_record(ready, package, tag, commit, draft=True) != draft_id
        or ready["assets"][0]["id"] != asset_id
    ):
        raise ValueError("The verified draft release changed before publication")
    _gh(
        root,
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PATCH",
        f"repos/{SLUG}/{endpoint}",
        "--input",
        "-",
        input_text=json.dumps({"draft": False, "make_latest": "true"}),
    )
    release = _api(root, endpoint)
    if (
        _verify_record(release, package, tag, commit, draft=False) != draft_id
        or release["assets"][0]["id"] != asset_id
    ):
        raise ValueError("Published release is not the verified draft")
    return {
        **package,
        "tag_object": tag_object,
        "release_id": draft_id,
        "asset_id": asset_id,
        "release_url": release["html_url"],
        "publication": "published",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="Exact annotated vX.Y.Z release tag")
    parser.add_argument("--dry-run", action="store_true", help="Read-only publication checks")
    args = parser.parse_args(argv)
    try:
        result = publish(tag=args.tag, dry_run=args.dry_run)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Release publication failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
