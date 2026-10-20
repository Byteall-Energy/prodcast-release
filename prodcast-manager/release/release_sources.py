"""Resolve, validate, and tag the immutable source snapshot for a ProdCast release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


OWNER = "Byteall-Energy"
REPOSITORIES = {
    "app": f"{OWNER}/prodcast-app",
    "agent": f"{OWNER}/prodcast-agent",
    "worker": f"{OWNER}/prodcast-worker",
    "ai": f"{OWNER}/prodcast-ai",
    "release": f"{OWNER}/prodcast-release",
}
STABLE = re.compile(r"v(\d+)\.(\d+)\.(\d+)\Z")


@dataclass
class GitHub:
    token: str

    def request(self, method: str, path: str, body: object | None = None, *, missing_ok: bool = False) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            "https://api.github.com" + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "prodcast-release-orchestrator",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed ({error.code}): {detail}") from error
        return json.loads(payload) if payload else None


def validate_version(version: str) -> tuple[int, int, int]:
    match = STABLE.fullmatch(version)
    if not match:
        raise ValueError("Release version must match vMAJOR.MINOR.PATCH")
    return tuple(map(int, match.groups()))


def tag_target(api: GitHub, repository: str, version: str) -> str | None:
    value = api.request("GET", f"/repos/{repository}/git/ref/tags/{version}", missing_ok=True)
    if value is None:
        return None
    target = value["object"]
    if target["type"] == "tag":
        target = api.request("GET", f"/repos/{repository}/git/tags/{target['sha']}")["object"]
    if target["type"] != "commit":
        raise RuntimeError(f"{repository}/{version} does not resolve to a commit")
    return target["sha"]


def require_untagged(api: GitHub, version: str) -> None:
    conflicts = [repository for repository in REPOSITORIES.values() if tag_target(api, repository, version)]
    if conflicts:
        raise RuntimeError(f"Refusing to move existing tag {version}: {', '.join(conflicts)}")


def check_ci(api: GitHub, repository: str, sha: str) -> dict[str, object]:
    """Fail on any known pending/failing check; enforce protected-branch contexts when readable."""
    try:
        checks = api.request("GET", f"/repos/{repository}/commits/{sha}/check-runs?per_page=100")["check_runs"]
        statuses = api.request("GET", f"/repos/{repository}/commits/{sha}/status")
    except RuntimeError as error:
        print(f"warning: CI checks are not readable for {repository}@{sha}: {error}", file=sys.stderr)
        return {"state": "unavailable", "reason": "token cannot read checks/statuses"}
    bad_checks = [
        check["name"]
        for check in checks
        if check["status"] != "completed" or check.get("conclusion") not in {"success", "neutral", "skipped"}
    ]
    bad_statuses = [status["context"] for status in statuses.get("statuses", []) if status["state"] != "success"]
    try:
        protection = api.request(
            "GET", f"/repos/{repository}/branches/main/protection/required_status_checks", missing_ok=True
        )
    except RuntimeError:
        protection = None
    required = set()
    if protection:
        required.update(protection.get("contexts", []))
        required.update(check["context"] for check in protection.get("checks", []))
    successful = {
        check["name"] for check in checks if check["status"] == "completed" and check.get("conclusion") == "success"
    }
    successful.update(status["context"] for status in statuses.get("statuses", []) if status["state"] == "success")
    missing = sorted(required - successful)
    if bad_checks or bad_statuses or missing:
        raise RuntimeError(
            f"CI is not green for {repository}@{sha}: "
            f"checks={bad_checks}, statuses={bad_statuses}, missing_required={missing}"
        )
    return {"check_runs": len(checks), "required_contexts": sorted(required), "state": "success"}


def source_ci(api: GitHub, name: str, repository: str, sha: str, enabled: bool) -> dict[str, object]:
    """Validate component CI without making the release workflow depend on itself."""
    if not enabled:
        return {"state": "not-checked"}
    if name == "release":
        return {
            "state": "not-applicable",
            "reason": "the release workflow cannot gate on its own in-progress check",
        }
    return check_ci(api, repository, sha)


def find_base(api: GitHub, target: str, override: str | None) -> tuple[str, str]:
    target_key = validate_version(target)
    releases = api.request("GET", f"/repos/{REPOSITORIES['release']}/releases?per_page=100")
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for release in releases:
        tag = release.get("tag_name", "")
        match = STABLE.fullmatch(tag)
        if release.get("draft") or release.get("prerelease") or not match:
            continue
        key = tuple(map(int, match.groups()))
        if key >= target_key or (override and tag != override):
            continue
        expected = f"ProdCast-{tag}-complete.zip"
        if any(asset["name"] == expected for asset in release.get("assets", [])):
            candidates.append((key, tag, expected))
    if override and not candidates:
        raise RuntimeError(f"Base override {override} is not a published stable release with a valid Complete ZIP")
    if not candidates:
        raise RuntimeError("No earlier published stable release contains a Complete ZIP")
    _, tag, asset = max(candidates)
    return tag, asset


def write_outputs(values: dict[str, str]) -> None:
    output = os.getenv("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def resolve(args: argparse.Namespace) -> None:
    validate_version(args.version)
    api = GitHub(args.token)
    existing_release = api.request(
        "GET", f"/repos/{REPOSITORIES['release']}/releases/tags/{args.version}", missing_ok=True
    )
    if existing_release is not None:
        raise RuntimeError(f"Release {args.version} already exists (draft or published)")
    require_untagged(api, args.version)
    components: dict[str, dict[str, object]] = {}
    for name, repository in REPOSITORIES.items():
        commit = api.request("GET", f"/repos/{repository}/commits/main")
        sha = commit["sha"]
        components[name] = {
            "component": name,
            "repository": repository,
            "source_commit": sha,
            "short_commit": sha[:12],
            "subject": commit["commit"]["message"].splitlines()[0],
            "ci": source_ci(api, name, repository, sha, args.check_ci),
        }
    base_tag, base_asset = find_base(api, args.version, args.base_tag or None)
    snapshot = {
        "schema_version": 1,
        "release_version": args.version,
        "release_tag": args.version,
        "base_release": {"tag": base_tag, "asset": base_asset},
        "components": components,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    lines = [f"ProdCast release: {args.version}", ""]
    for name in ("app", "agent", "worker", "ai", "release"):
        item = components[name]
        lines.extend(
            [name.title() + ":", f"  commit: {item['source_commit']}", f"  short: {item['short_commit']}", f"  subject: {item['subject']}", ""]
        )
    lines.extend(["Base offline release:", f"  tag: {base_tag}", f"  asset: {base_asset}"])
    print("\n".join(lines))
    outputs = {f"{name}_sha": str(item["source_commit"]) for name, item in components.items()}
    outputs.update({"base_tag": base_tag, "base_asset": base_asset})
    write_outputs(outputs)


def create_tags(args: argparse.Namespace) -> None:
    api = GitHub(args.token)
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    version = snapshot["release_version"]
    validate_version(version)
    require_untagged(api, version)
    created: list[str] = []
    for name in ("app", "agent", "worker", "ai", "release"):
        item = snapshot["components"][name]
        repository = item["repository"]
        sha = item["source_commit"]
        current = api.request("GET", f"/repos/{repository}/commits/main")["sha"]
        print(f"Tagging frozen {repository}@{sha}; current main is {current}")
        api.request("POST", f"/repos/{repository}/git/refs", {"ref": f"refs/tags/{version}", "sha": sha})
        actual = tag_target(api, repository, version)
        if actual != sha:
            raise RuntimeError(f"Tag verification failed for {repository}/{version}: {actual} != {sha}")
        created.append(f"{repository}/{version}")
    print(json.dumps({"tags_created": created}, indent=2))


def preflight(args: argparse.Namespace) -> None:
    api = GitHub(args.token)
    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    version = snapshot["release_version"]
    validate_version(version)
    if api.request("GET", f"/repos/{REPOSITORIES['release']}/releases/tags/{version}", missing_ok=True) is not None:
        raise RuntimeError(f"Release {version} appeared after the build started")
    require_untagged(api, version)
    print(f"Publication preflight passed for frozen snapshot {version}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--version", required=True)
    resolve_parser.add_argument("--base-tag", default="")
    resolve_parser.add_argument("--output", required=True)
    resolve_parser.add_argument("--token", default=os.getenv("PRODCAST_RELEASE_TOKEN", ""))
    resolve_parser.add_argument("--check-ci", action=argparse.BooleanOptionalAction, default=True)
    resolve_parser.set_defaults(handler=resolve)
    tag_parser = subparsers.add_parser("create-tags")
    tag_parser.add_argument("--snapshot", required=True)
    tag_parser.add_argument("--token", default=os.getenv("PRODCAST_RELEASE_TOKEN", ""))
    tag_parser.set_defaults(handler=create_tags)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--snapshot", required=True)
    preflight_parser.add_argument("--token", default=os.getenv("PRODCAST_RELEASE_TOKEN", ""))
    preflight_parser.set_defaults(handler=preflight)
    args = parser.parse_args()
    if not args.token:
        parser.error("PRODCAST_RELEASE_TOKEN is required")
    try:
        args.handler(args)
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
