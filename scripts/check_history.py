"""Verify Conventional Commits, linear history and exact SemVer release tags."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_release  # noqa: E402

OBJECT_ID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
TITLE = re.compile(
    r"(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?: (?P<description>\S.*)"
)
BREAKING_FOOTER = re.compile(r"^BREAKING(?: CHANGE|-CHANGE):(?:[ \t]+|$)", re.M)


def _object_id(raw: bytes):
    try:
        value = raw.decode("ascii")
    except UnicodeError:
        raise ValueError("Invalid Git history object identity") from None
    if not OBJECT_ID.fullmatch(value):
        raise ValueError("Invalid Git history object identity")
    return value


def _parse_history(raw: bytes, head: str):
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields or len(fields) % 3:
        raise ValueError("Invalid or empty Git commit history")
    commits = []
    for offset in range(0, len(fields), 3):
        commit = _object_id(fields[offset])
        parents = [_object_id(parent) for parent in fields[offset + 1].split()]
        if len(parents) > 1:
            raise ValueError(f"Merge commits are not allowed: {commit}")
        try:
            message = fields[offset + 2].decode("utf-8").replace("\r\n", "\n")
        except UnicodeError:
            raise ValueError(f"Invalid UTF-8 commit message: {commit}") from None
        title, _, body = message.partition("\n")
        match = TITLE.fullmatch(title)
        if (
            match is None
            or any(ord(character) < 32 or ord(character) == 127 for character in title)
            or match["description"].strip() != match["description"]
            or match["scope"] is not None
            and match["scope"].strip() != match["scope"]
        ):
            raise ValueError(f"Commit title must follow Conventional Commits: {commit}")
        commits.append(
            {
                "commit": commit,
                "parents": parents,
                "type": match["type"],
                "breaking": bool(match["breaking"] or BREAKING_FOOTER.search(body)),
            }
        )
    previous = None
    for commit in commits:
        if commit["parents"] != ([] if previous is None else [previous]):
            raise ValueError("Git commit history must be complete and linear")
        previous = commit["commit"]
    if previous != head:
        raise ValueError("Git commit history does not end at HEAD")
    return commits


def _next_version(previous: str, commits):
    if not commits:
        raise ValueError("Release tags must contain new commits")
    major, minor, patch = build_release._version_parts(previous)
    if any(commit["breaking"] for commit in commits):
        return f"{major + 1}.0.0", "major"
    if any(commit["type"] == "feat" for commit in commits):
        return f"{major}.{minor + 1}.0", "minor"
    return f"{major}.{minor}.{patch + 1}", "patch"


def _historical_version(raw: bytes):
    """Historical identity is stable; runtime support belongs to the current build gate."""
    try:
        meta = yaml.load(raw, Loader=build_release._MetadataLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise ValueError("Invalid historical plugin metadata YAML") from None
    if (
        not isinstance(meta, dict)
        or meta.get("name") != build_release.PLUGIN
        or meta.get("repo") != build_release.REPOSITORY
    ):
        raise ValueError("Invalid historical plugin metadata identity")
    version = meta.get("version")
    build_release._version_parts(version)
    return version


def _reachable_releases(root: Path, commits):
    positions = {commit["commit"]: index for index, commit in enumerate(commits)}
    raw = build_release._git_bytes(
        root,
        "for-each-ref",
        "--merged=HEAD",
        "--format=%(refname)%00%(objecttype)%00%(objectname)"
        "%00%(*objecttype)%00%(*objectname)%00%(tag)",
        "refs/tags",
    )
    releases = []
    for line in raw.splitlines():
        fields = line.split(b"\0")
        if len(fields) != 6:
            raise ValueError("Invalid local Git release tag records")
        try:
            name = fields[0].decode("utf-8").removeprefix("refs/tags/")
        except UnicodeError:
            raise ValueError("Invalid local Git release tag name") from None
        if not re.fullmatch(rf"v{build_release.VERSION}", name):
            continue
        if fields[1] != b"tag" or fields[3] != b"commit":
            raise ValueError(f"Release tag must be annotated and target a commit: {name}")
        tag_object = _object_id(fields[2])
        commit = _object_id(fields[4])
        if fields[5] != name.encode("utf-8"):
            raise ValueError(f"Annotated tag name does not match its release ref: {name}")
        if commit not in positions:
            raise ValueError(f"Release tag is outside the complete HEAD history: {name}")
        releases.append(
            {
                "tag": name,
                "version": name[1:],
                "tag_object": tag_object,
                "commit": commit,
                "position": positions[commit],
            }
        )
    return sorted(releases, key=lambda release: release["position"])


def check_history(root: Path = ROOT, *, tag: str | None = None):
    """Read only the requested repository; validate tag source, not the dirty worktree."""
    if tag is not None and (
        not isinstance(tag, str) or not re.fullmatch(rf"v{build_release.VERSION}", tag)
    ):
        raise ValueError("Release tag must be an exact vX.Y.Z semantic version")
    root = Path(root).resolve()
    try:
        repository = (
            build_release._git_bytes(root, "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .rstrip("\r\n")
        )
    except UnicodeError:
        raise ValueError("Invalid local Git repository root") from None
    if not Path(repository).is_absolute() or Path(repository).resolve() != root:
        raise ValueError("Git repository root does not match history source root")
    if build_release._git_bytes(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("History verification requires a complete, non-shallow repository")
    head = _object_id(
        build_release._git_bytes(root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    )
    commits = _parse_history(
        build_release._git_bytes(
            root,
            "log",
            "--reverse",
            "--no-color",
            "--no-decorate",
            "--no-notes",
            "--no-show-signature",
            "--encoding=UTF-8",
            "--format=%H%x00%P%x00%B",
            "-z",
            "HEAD",
            "--",
        ),
        head,
    )
    result = {
        "head": head,
        "source_commit": head,
        "commit_count": len(commits),
        "linear_history_verified": True,
        "conventional_commits_verified": True,
    }
    if tag is None:
        return result
    releases = _reachable_releases(root, commits)
    selected = next((release for release in releases if release["tag"] == tag), None)
    if selected is None:
        raise ValueError("Release tag must exist in the reachable HEAD history")
    if selected["commit"] != head:
        raise ValueError("Release tag must point to HEAD")
    if not releases or releases[0]["tag"] != "v0.1.0":
        raise ValueError("The first release in the linear history must be v0.1.0")
    previous = None
    bump = "initial"
    for release in releases:
        if previous is not None:
            interval = commits[previous["position"] + 1 : release["position"] + 1]
            expected, bump = _next_version(previous["version"], interval)
            if release["version"] != expected:
                raise ValueError(
                    f"Release {release['tag']} must be v{expected} "
                    f"for its {bump.upper()} Conventional Commit changes"
                )
        version = _historical_version(
            build_release._git_blob(root, release["commit"], "metadata.yaml")
        )
        if version != release["version"]:
            raise ValueError(
                f"Release tag version does not match committed metadata: {release['tag']}"
            )
        previous = release
    prior = releases[-2] if len(releases) > 1 else None
    return {
        **result,
        "tag": tag,
        "version": selected["version"],
        "tag_object": selected["tag_object"],
        "previous_tag": prior["tag"] if prior else None,
        "release_commit_count": (
            selected["position"] - prior["position"] if prior else selected["position"] + 1
        ),
        "release_count": len(releases),
        "bump": bump,
        "semver_verified": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Also verify the annotated vX.Y.Z release tag at HEAD")
    args = parser.parse_args(argv)
    try:
        result = check_history(ROOT, tag=args.tag)
    except (ValueError, OSError) as error:
        parser.exit(1, f"History validation failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
