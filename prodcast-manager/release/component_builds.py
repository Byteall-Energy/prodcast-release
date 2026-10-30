"""Read-only source resolution and artifact checks for selective builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from release_sources import GitHub


COMPONENTS = {
    "app": ("prodcast-app", "ubuntu-24.04", "3.14.7", "dist/release"),
    "ai": ("prodcast-ai", "ubuntu-24.04", "3.14.7", "dist/release"),
    "worker": ("prodcast-worker", "windows-2022", "3.14.7", "dist/release"),
    "agent": ("prodcast-agent", "windows-2022", "3.14.7", "dist/package"),
    "manager": ("prodcast-release", "windows-2025", "3.11.9", "prodcast-manager/dist"),
}


def selection(inputs: dict) -> dict:
    version = inputs.get("version", "")
    if not re.fullmatch(r"v[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", version):
        raise ValueError("Version must be vMAJOR.MINOR.PATCH")
    parts = [int(part) for part in version[1:].split(".")]
    if max(parts) > 65535:
        raise ValueError("Windows version components must not exceed 65535")
    selected = [name for name in COMPONENTS if inputs.get(name) in (True, "true")]
    if not selected:
        raise ValueError("Select at least one component")
    refs = json.loads(inputs.get("source_refs") or "{}")
    if not isinstance(refs, dict) or set(refs) - set(selected):
        raise ValueError("source_refs must be a JSON object containing only selected components")
    for ref in refs.values():
        if not isinstance(ref, str) or not ref or len(ref) > 255 or any(ord(c) < 33 for c in ref):
            raise ValueError("Each source ref must be a nonempty branch, tag, or commit without whitespace")
    return {
        "version": "v" + ".".join(map(str, parts)),
        "refs": {name: refs.get(name, "main") for name in selected},
    }


def resolve(api: GitHub, selected: dict, *, workflow_sha: str, run_url: str) -> dict:
    rows = []
    for name, ref in selected["refs"].items():
        repo, runner, python, payload = COMPONENTS[name]
        repository = f"Byteall-Energy/{repo}"
        commit = api.request("GET", f"/repos/{repository}/commits/{quote(ref, safe='')}")
        sha = commit["sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"Invalid source SHA returned for {repository}")
        rows.append({
            "component": name, "repo": repo, "repository": repository,
            "ref": ref, "sha": sha, "runner": runner, "python": python, "payload": payload,
        })
    return {
        "schema_version": 1, "build_kind": "selective", "production_accepted": False,
        "version": selected["version"], "workflow_sha": workflow_sha, "run_url": run_url,
        "components": rows,
    }


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage(snapshot: dict, component: str, source: Path, destination: Path) -> None:
    row = next(row for row in snapshot["components"] if row["component"] == component)
    payload = source / row["payload"]
    if component == "manager":
        files = [payload / f"ProdCast-Manager-{snapshot['version']}.exe"]
    else:
        manifest = json.loads((payload / "release-manifest.json").read_text(encoding="utf-8"))
        expected = {"version": snapshot["version"], "repository": row["repository"], "source_commit": row["sha"]}
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"{component}: manifest {key} differs from frozen source snapshot")
        artifacts = manifest.get("artifacts", [])
        if not artifacts:
            raise ValueError(f"{component}: empty artifact manifest")
        names = set()
        for item in artifacts:
            name = item["name"]
            if not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in '/\\\r\n') or name in names:
                raise ValueError("Invalid or duplicate artifact name")
            names.add(name)
            path = payload / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                raise ValueError(f"{component}: artifact checksum/size mismatch: {name}")
        files = sorted(payload.iterdir())
        if {p.name for p in files} != names | {"release-manifest.json", "SHA256SUMS"}:
            raise ValueError(f"{component}: payload contains unlisted files")
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing, empty, or invalid payload: {path.name}")
    destination.mkdir(parents=True, exist_ok=False)
    for path in files:
        shutil.copy2(path, destination / path.name)
    metadata = {
        **{key: value for key, value in snapshot.items() if key != "components"},
        "source": row,
    }
    write_json(destination / "component-build.json", metadata)
    files = sorted(destination.iterdir())
    (destination / "BUILD-SHA256SUMS").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in files), encoding="utf-8"
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def output(name: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["select", "resolve", "stage"])
    parser.add_argument("--selection", type=Path, default=Path("selection.json"))
    parser.add_argument("--snapshot", type=Path, default=Path("source-snapshot.json"))
    parser.add_argument("--component", choices=COMPONENTS)
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--destination", type=Path, default=Path("artifact"))
    args = parser.parse_args()
    if args.command == "select":
        inputs = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["inputs"]
        selected = selection(inputs)
        write_json(args.selection, selected)
        output("repositories", ",".join(COMPONENTS[name][0] for name in selected["refs"]))
        output("version", selected["version"])
    elif args.command == "resolve":
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise ValueError("Configure PRODCAST_RELEASE_APP_ID/private key or PRODCAST_RELEASE_TOKEN")
        snapshot = resolve(
            GitHub(token), json.loads(args.selection.read_text()), workflow_sha=os.environ["GITHUB_SHA"],
            run_url=f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}",
        )
        write_json(args.snapshot, snapshot)
        output("matrix", json.dumps({"include": snapshot["components"]}))
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"## Selective build {snapshot['version']}\n\n")
            for row in snapshot["components"]:
                stream.write(f"- {row['component']}: `{row['repository']}@{row['sha']}`\n")
            stream.write("\nSeparate artifacts only; no Complete archive, tags, release, or deployment.\n")
    else:
        if not args.component:
            parser.error("stage requires --component")
        stage(json.loads(args.snapshot.read_text()), args.component, args.source, args.destination)


if __name__ == "__main__":
    main()
