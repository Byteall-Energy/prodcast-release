"""Assemble and validate a complete release from freshly built component payloads."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile

REPOSITORY = "Byteall-Energy/prodcast-release"
COMPONENT_REPOSITORIES = {
    "app": "Byteall-Energy/prodcast-app", "agent": "Byteall-Energy/prodcast-agent",
    "worker": "Byteall-Energy/prodcast-worker", "ai": "Byteall-Energy/prodcast-ai",
}
COMPONENT_PREFIXES = {
    "app": ("prodcast-app-", "prodcast-backend-", "prodcast-frontend-", "prodcast-gateway-"),
    "agent": ("prodcast-agent-", "ProdCastAgent-", "ProdcastAgentSetup-", "workflow-agent-"),
    "worker": ("prodcast-worker-",), "ai": ("prodcast-ai-",),
}
CONTROLS = {"release-manifest.json", "release-manifest.json.sha256", "SHA256SUMS"}


def sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"Unsafe artifact name: {value!r}")
    return value


def validate_version(value: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", value):
        raise ValueError("Release version must use vMAJOR.MINOR.PATCH")
    return value


def record(path: Path, **extra: object) -> dict[str, object]:
    path = Path(path)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path), **extra}


def write_json(path: Path, value: object) -> None:
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def verify(folder: Path, item: dict[str, object]) -> Path:
    path = Path(folder) / validate_name(str(item["name"]))
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
        raise ValueError(f"Artifact integrity failed: {path.name}")
    return path


def verify_checksum_file(folder: Path, filename: str = "SHA256SUMS") -> None:
    checksum_file = Path(folder) / filename
    if not checksum_file.is_file():
        raise ValueError(f"Missing {filename} in {folder}")
    seen: set[str] = set()
    for line in checksum_file.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not match:
            raise ValueError(f"Invalid checksum line in {checksum_file}: {line!r}")
        digest, name = match.groups()
        if name in seen or sha256(Path(folder) / validate_name(name)) != digest.lower():
            raise ValueError(f"Checksum validation failed for {name}")
        seen.add(name)


def unpack(archive: Path, destination: Path) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as source:
        names: set[str] = set()
        for item in source.infolist():
            name = validate_name(item.filename)
            mode = item.external_attr >> 16
            if name in names or item.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError(f"Unsafe ZIP member: {name}")
            names.add(name)
        for item in source.infolist():
            with source.open(item) as src, (destination / item.filename).open("wb") as dst:
                shutil.copyfileobj(src, dst)


def pack(folder: Path, output: Path, names: list[str]) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name in sorted(names):
            path = Path(folder) / validate_name(name)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Only regular files can be packaged")
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            with path.open("rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst)


def component_file(component: str, name: str) -> bool:
    return name.startswith(COMPONENT_PREFIXES[component]) or name == f"prodcast-{component}-source-manifest.json"


def manager_file(name: str) -> bool:
    return name.lower().startswith("prodcast-manager-")


def declared_external_payloads(manifest: dict) -> set[str]:
    offline = manifest.get("management", {}).get("offline", {})
    names: set[str] = set()
    if offline.get("external_ollama"):
        for key in ("ollama_runtime", "model_archive"):
            if isinstance(offline.get(key), str):
                names.add(offline[key])
    if isinstance(offline.get("external_model"), dict) and isinstance(offline["external_model"].get("name"), str):
        names.add(offline["external_model"]["name"])
    return names


def load_payload(folder: Path, component: str, snapshot: dict, version: str) -> tuple[Path, dict]:
    folder = Path(folder)
    manifest_path = folder / "release-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Missing {component} release-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = snapshot["components"][component]
    for key, value in {"repository": COMPONENT_REPOSITORIES[component], "source_commit": expected["source_commit"], "version": version}.items():
        if manifest.get(key) != value:
            raise ValueError(f"{component} {key} mismatch: {manifest.get(key)!r} != {value!r}")
    names = [validate_name(str(item["name"])) for item in manifest.get("artifacts", [])]
    if not names or len(names) != len(set(names)) or CONTROLS & set(names):
        raise ValueError(f"Invalid {component} artifact list")
    for item in manifest["artifacts"]:
        verify(folder, item)
    expected_names = set(names) | {"release-manifest.json", "SHA256SUMS"}
    actual_names = {path.name for path in folder.iterdir() if path.is_file()}
    if actual_names != expected_names or any(path.is_dir() or path.is_symlink() for path in folder.iterdir()):
        raise ValueError(f"Unexpected files in {component} payload: {sorted(actual_names ^ expected_names)}")
    verify_checksum_file(folder)
    return folder, manifest


def mapped_name(component: str, name: str, source_version: str, target: str) -> str:
    validate_name(name)
    if name == "release-manifest.json":
        return f"prodcast-{component}-source-manifest.json"
    if name in {"images.json", "packages.env", "INSTALL-RU.md", "INSTALL-EN.md"}:
        return f"prodcast-{component}-{name}"
    if not component_file(component, name):
        raise ValueError(f"Unexpected {component} artifact: {name}")
    return name.replace(source_version, target)


def build(version: str, base_archive: Path, payloads: dict[str, Path], manager_exe: Path, snapshot_path: Path, output: Path) -> dict[str, Path]:
    target = validate_version(version)
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    if snapshot.get("release_version") != target:
        raise ValueError("Source snapshot version does not match requested release")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    contents = output / "contents"
    unpack(Path(base_archive), contents)
    manifest_path = contents / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_version = manifest.get("version")
    if base_version != snapshot["base_release"]["tag"]:
        raise ValueError(f"Base archive version mismatch: {base_version!r}")
    declared_external = declared_external_payloads(manifest)
    for item in manifest["artifacts"]:
        if item["name"] not in declared_external and item.get("location", "complete") == "complete":
            verify(contents, item)
    verify_checksum_file(contents)

    components = {item["component"]: copy.deepcopy(item) for item in manifest["components"]}
    external = [copy.deepcopy(item) for item in manifest["artifacts"] if (item.get("location", "complete") != "complete" or item["name"] in declared_external) and not any(component_file(component, str(item["name"])) for component in COMPONENT_REPOSITORIES) and not manager_file(str(item["name"]))]
    for old in list(contents.iterdir()):
        if any(component_file(component, old.name) for component in ("app", "worker", "ai", "agent")) or manager_file(old.name):
            old.unlink()

    for component in ("app", "worker", "ai", "agent"):
        payload, build_manifest = load_payload(payloads[component], component, snapshot, target)
        if component == "worker":
            policy = build_manifest.get("worker_execution_policy")
            management = manifest.setdefault("management", {})
            management.pop("worker_execution_policy", None)
            if policy is not None:
                expected = {"schema": 1, "execution_mode": "windows-account", "execution_timeout_seconds": 0}
                if policy != expected or type(policy.get("schema")) is not int or type(policy.get("execution_timeout_seconds")) is not int:
                    raise ValueError("Unsupported Worker execution policy")
                management["worker_execution_policy"] = copy.deepcopy(policy)
                management["minimum_manager"] = "0.6.4"
        artifact_names = [item["name"] for item in build_manifest["artifacts"]]
        mapping: dict[str, str] = {}
        if component == "agent":
            installers = [name for name in artifact_names if name.startswith(("ProdcastAgentSetup-", "ProdCastAgent-Setup-")) and name.endswith(".exe")]
            if len(installers) != 1:
                raise ValueError("Agent payload must contain exactly one setup EXE")
            for name in artifact_names + ["release-manifest.json"]:
                destination_name = f"ProdcastAgentSetup-{target}.exe" if name == installers[0] else mapped_name(component, name, build_manifest["version"], target)
                if (output / destination_name).exists():
                    raise ValueError(f"Artifact collision: {destination_name}")
                shutil.copyfile(payload / name, output / destination_name)
                mapping[name] = destination_name
        else:
            for name in artifact_names + ["release-manifest.json"]:
                destination_name = mapped_name(component, name, build_manifest["version"], target)
                if (contents / destination_name).exists():
                    raise ValueError(f"Artifact collision: {destination_name}")
                shutil.copyfile(payload / name, contents / destination_name)
                mapping[name] = destination_name
        destination_root = output if component == "agent" else contents
        components[component] = {
            "component": component, "repository": COMPONENT_REPOSITORIES[component],
            "source_commit": snapshot["components"][component]["source_commit"], "release_tag": target,
            "binary_version": target, "platform": "windows" if component in {"agent", "worker"} else "linux",
            "architecture": "amd64",
            "images": json.loads((payload / "images.json").read_text(encoding="utf-8")) if (payload / "images.json").exists() else [],
            "artifacts": [record(destination_root / destination) for destination in mapping.values()],
            "artifact_mapping": mapping,
        }
        if component == "agent":
            external += [record(output / destination, location="release-asset") for destination in mapping.values()]

    manager_output = output / f"ProdCast-Manager-{target}.exe"
    if Path(manager_exe).suffix.lower() != ".exe" or not Path(manager_exe).is_file():
        raise ValueError("Manager build did not produce an EXE")
    shutil.copyfile(manager_exe, manager_output)
    if any(path.suffix.lower() == ".exe" for path in contents.iterdir()):
        raise ValueError("Complete ZIP may not contain top-level Agent or Manager executables")
    components["manager"] = {
        "component": "manager", "repository": REPOSITORY,
        "source_commit": snapshot["components"]["release"]["source_commit"], "release_tag": target,
        "binary_version": target, "platform": "windows", "architecture": "amd64",
        "artifacts": [record(manager_output, location="release-asset")],
    }

    manifest["schema_version"] = max(2, int(manifest.get("schema_version", 1)))
    manifest.update({"version": target, "release_tag": target, "status": "complete", "publication_status": "prepared", "production_accepted": False, "created_utc": datetime.now(timezone.utc).isoformat()})
    manifest["components"] = [components[name] for name in sorted(components)]
    manifest["assembly_source"] = {"base_version": base_version, "base_asset": snapshot["base_release"]["asset"], "changed_components": ["app", "agent", "worker", "ai", "manager"], "standalone_executables": [f"ProdcastAgentSetup-{target}.exe", manager_output.name]}
    management = manifest.setdefault("management", {})
    management.update({"recommended_manager": target.removeprefix("v"), "upgrade_from": sorted(set(management.get("upgrade_from", [])) | {base_version}), "distribution": "standalone-exe"})
    external += [record(manager_output, location="release-asset")]
    manifest["artifacts"] = external + [record(path, location="complete") for path in sorted(contents.iterdir()) if path.name not in CONTROLS]
    write_json(manifest_path, manifest)
    manifest_digest = sha256(manifest_path)
    (contents / "release-manifest.json.sha256").write_text(f"{manifest_digest}  release-manifest.json\n", encoding="utf-8")
    (contents / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in sorted(contents.iterdir()) if path.name != "SHA256SUMS"), encoding="utf-8")
    verify_checksum_file(contents)

    complete = output / f"ProdCast-{target}-complete.zip"
    pack(contents, complete, [path.name for path in contents.iterdir()])
    shutil.copyfile(manifest_path, output / "release-manifest.json")
    manifest_checksum = output / "release-manifest.json.sha256"
    manifest_checksum.write_text(f"{manifest_digest}  release-manifest.json\n", encoding="utf-8")
    notes = output / "RELEASE-NOTES.md"
    notes.write_text(f"# ProdCast {target}\n\nBuilt from the frozen source snapshot recorded in `release-manifest.json`. Static/offline dependencies were carried forward from {base_version}.\n\n- `ProdCast-{target}-complete.zip` — App, Worker, AI, deployment and offline payloads\n- `ProdcastAgentSetup-{target}.exe` — Windows Agent installer\n- `ProdCast-Manager-{target}.exe` — one-file Windows Manager; it creates `prodcast-data/logs` and `prodcast-data/sites` beside the executable\n", encoding="utf-8")
    checksums = output / "SHA256SUMS"
    checksum_targets = sorted(path for path in output.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    checksums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in checksum_targets), encoding="utf-8")
    verify_checksum_file(output)
    with zipfile.ZipFile(complete) as archive:
        names = archive.namelist()
        if any(name.lower().endswith(".exe") for name in names):
            raise ValueError("Complete ZIP contains a forbidden top-level executable")
        if not CONTROLS <= set(names):
            raise ValueError("Complete ZIP is missing release controls")
    shutil.rmtree(contents)
    return {"complete": complete, "agent": output / f"ProdcastAgentSetup-{target}.exe", "manager": manager_output, "manifest": output / "release-manifest.json", "manifest_checksum": manifest_checksum, "checksums": checksums, "notes": notes}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True); parser.add_argument("--base-archive", required=True)
    parser.add_argument("--snapshot", required=True); parser.add_argument("--app", required=True)
    parser.add_argument("--agent", required=True); parser.add_argument("--worker", required=True)
    parser.add_argument("--ai", required=True); parser.add_argument("--manager-exe", required=True)
    parser.add_argument("--output", default="dist/assembled")
    args = parser.parse_args()
    try:
        assets = build(args.version, Path(args.base_archive), {name: Path(getattr(args, name)) for name in COMPONENT_REPOSITORIES}, Path(args.manager_exe), Path(args.snapshot), Path(args.output))
    except (ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr); raise SystemExit(1) from error
    print(json.dumps({key: {"name": path.name, "sha256": sha256(path)} for key, path in assets.items()}, indent=2))


if __name__ == "__main__":
    main()
