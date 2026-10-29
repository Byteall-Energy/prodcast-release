import json
from pathlib import Path
import sys
import subprocess
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))
from assemble import build, pack, record, sha256, write_json


VERSION = "v0.6.1"
COMMITS = {name: character * 40 for name, character in zip(("app", "agent", "worker", "ai", "release"), "abcde")}
REPOSITORIES = {name: f"Byteall-Energy/prodcast-{name}" for name in ("app", "agent", "worker", "ai")}


def checksum(folder):
    (folder / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(folder.iterdir()) if path.name != "SHA256SUMS"),
        encoding="utf-8",
    )


def payload(root, component, names):
    folder = root / component
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes((component + name).encode())
    write_json(
        folder / "release-manifest.json",
        {
            "repository": REPOSITORIES[component],
            "version": VERSION,
            "status": "stable",
            "source_commit": COMMITS[component],
            "artifacts": [record(path) for path in sorted(folder.iterdir())],
        },
    )
    checksum(folder)
    return folder


def base(root):
    folder = root / "base"
    folder.mkdir()
    for name in (
        "prodcast-backend-v0.6.0-linux-amd64.tar.gz",
        "prodcast-worker-v0.6.0-windows-amd64.zip",
        "prodcast-ai-v0.6.0-linux-amd64.tar.gz",
        "ProdCastAgent-Setup-v0.6.0.exe",
        "ProdCast-Manager-v0.6.0.exe",
        "offline-database-images.tar",
    ):
        (folder / name).write_bytes(name.encode())
    external = {"name": "offline-ollama.tar.xz", "bytes": 123, "sha256": "c" * 64, "location": "release-asset"}
    manifest = {
        "version": "v0.6.0",
        "management": {"schema": 3, "recommended_manager": "0.6.0", "upgrade_from": ["v0.5.0"]},
        "components": [{"component": name, "source_commit": "f" * 40, "binary_version": "v0.6.0"} for name in ("app", "agent", "worker", "ai", "license")],
        "artifacts": [record(path, location="complete") for path in sorted(folder.iterdir())] + [external],
    }
    write_json(folder / "release-manifest.json", manifest)
    checksum(folder)
    archive = root / "base.zip"
    pack(folder, archive, [path.name for path in folder.iterdir()])
    return archive


def snapshot(root):
    path = root / "source-snapshot.json"
    write_json(
        path,
        {
            "release_version": VERSION,
            "base_release": {"tag": "v0.6.0", "asset": "ProdCast-v0.6.0-complete.zip"},
            "components": {
                name: {"repository": "Byteall-Energy/prodcast-release" if name == "release" else REPOSITORIES[name], "source_commit": commit}
                for name, commit in COMMITS.items()
            },
        },
    )
    return path


def inputs(root, agent_prefix="ProdCastAgent-"):
    return {
        "app": payload(root, "app", [f"prodcast-backend-{VERSION}-linux-amd64.tar.gz", f"prodcast-app-{VERSION}-deployment.tar.gz"]),
        "agent": payload(root, "agent", [f"{agent_prefix}{VERSION}.exe", "workflow-agent-sbom.cdx.json"]),
        "worker": payload(root, "worker", [f"prodcast-worker-{VERSION}-windows-amd64.zip"]),
        "ai": payload(root, "ai", [f"prodcast-ai-{VERSION}-linux-amd64.tar.gz", f"prodcast-ai-{VERSION}-deployment.tar.gz"]),
    }


def test_complete_uses_fresh_payloads_and_excludes_standalone_executables(tmp_path):
    agent_prefix = "ProdCastAgent-"
    payloads = inputs(tmp_path, agent_prefix)
    manager = tmp_path / "ProdCast-Manager.exe"
    manager.write_bytes(b"manager")
    assets = build(VERSION, base(tmp_path), payloads, manager, snapshot(tmp_path), tmp_path / "out")
    with zipfile.ZipFile(assets["complete"]) as archive:
        names = set(archive.namelist())
        assert not any(name.lower().endswith(".exe") for name in names)
        assert f"prodcast-worker-{VERSION}-windows-amd64.zip" in names
        assert f"prodcast-ai-{VERSION}-linux-amd64.tar.gz" in names
        assert "offline-database-images.tar" in names
        manifest = json.loads(archive.read("release-manifest.json"))
    assert {item["component"] for item in manifest["components"]} >= {"app", "agent", "worker", "ai", "manager"}
    for component in ("app", "agent", "worker", "ai"):
        item = next(value for value in manifest["components"] if value["component"] == component)
        assert item["source_commit"] == COMMITS[component]
        assert item["release_tag"] == VERSION
        assert item["binary_version"] == VERSION
    assert assets["agent"].name == f"ProdCastAgent-{VERSION}.exe"
    assert assets["agent"].read_bytes() == ("agent" + agent_prefix + VERSION + ".exe").encode()
    assert assets["manager"].read_bytes() == b"manager"
    assert sha256(assets["manifest"]) in assets["manifest_checksum"].read_text()
    summary = tmp_path / "summary.md"
    subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1] / "release/write_summary.py"),
        str(tmp_path / "source-snapshot.json"), str(tmp_path / "out"), str(summary), "build-only",
    ], check=True)
    assert f"ProdCastAgent-{VERSION}.exe" in summary.read_text(encoding="utf-8")
    assert sha256(assets["agent"]) in summary.read_text(encoding="utf-8")
    assert manifest["assembly_source"]["standalone_executables"] == [assets["agent"].name, assets["manager"].name]



def test_provenance_mismatch_fails_closed(tmp_path):
    payloads = inputs(tmp_path)
    path = payloads["worker"] / "release-manifest.json"
    data = json.loads(path.read_text())
    data["source_commit"] = "0" * 40
    write_json(path, data)
    checksum(payloads["worker"])
    manager = tmp_path / "manager.exe"
    manager.write_bytes(b"manager")
    with pytest.raises(ValueError, match="worker source_commit mismatch"):
        build(VERSION, base(tmp_path), payloads, manager, snapshot(tmp_path), tmp_path / "out")


def test_unexpected_component_file_is_rejected(tmp_path):
    payloads = inputs(tmp_path)
    (payloads["ai"] / "secret.env").write_text("secret")
    manager = tmp_path / "manager.exe"
    manager.write_bytes(b"manager")
    with pytest.raises(ValueError, match="Unexpected files in ai payload"):
        build(VERSION, base(tmp_path), payloads, manager, snapshot(tmp_path), tmp_path / "out")


@pytest.mark.parametrize("policy", [None, {"schema": 1, "execution_mode": "windows-account", "execution_timeout_seconds": 0}])
def test_worker_policy_follows_payload(tmp_path, policy):
    payloads = inputs(tmp_path)
    path = payloads["worker"] / "release-manifest.json"
    data = json.loads(path.read_text())
    if policy is not None:
        data["worker_execution_policy"] = policy
        write_json(path, data)
        checksum(payloads["worker"])
    manager = tmp_path / "manager.exe"
    manager.write_bytes(b"manager")
    assets = build(VERSION, base(tmp_path), payloads, manager, snapshot(tmp_path), tmp_path / "out")
    doc = json.loads(assets["manifest"].read_text())
    assert doc["management"].get("worker_execution_policy") == policy
    if policy:
        assert doc["management"]["minimum_manager"] == "0.6.4"


@pytest.mark.parametrize("agent_prefix", ["ProdcastAgentSetup-", "ProdCastAgent-Setup-"])
def test_legacy_agent_installer_is_rejected(tmp_path, agent_prefix):
    payloads = inputs(tmp_path, agent_prefix)
    manager = tmp_path / "manager.exe"
    manager.write_bytes(b"manager")
    with pytest.raises(ValueError, match="exactly one versioned portable EXE"):
        build(VERSION, base(tmp_path), payloads, manager, snapshot(tmp_path), tmp_path / "out")


@pytest.mark.parametrize('capability',[{'schema':1},{'schema':True},{'schema':2}])
def test_worker_queue_capability_is_bound_to_built_payload(tmp_path,capability,monkeypatch):
    monkeypatch.setitem(globals(),'VERSION','v0.6.7')
    payloads=inputs(tmp_path);path=payloads['worker']/'release-manifest.json'
    data=json.loads(path.read_text());data['worker_queue_selection']=capability
    write_json(path,data);checksum(payloads['worker'])
    manager=tmp_path/'manager.exe';manager.write_bytes(b'manager')
    if type(capability['schema']) is not int or capability['schema']!=1:
        with pytest.raises(ValueError,match='queue selection'):build(VERSION,base(tmp_path),payloads,manager,snapshot(tmp_path),tmp_path/'out')
    else:
        assets=build(VERSION,base(tmp_path),payloads,manager,snapshot(tmp_path),tmp_path/'out')
        doc=json.loads(assets['manifest'].read_text())
        assert doc['management']['worker_queue_selection']==capability
        assert doc['management']['minimum_manager']=='0.6.7'


def test_queue_capability_cannot_ship_with_an_older_manager(tmp_path):
    payloads=inputs(tmp_path);path=payloads['worker']/'release-manifest.json'
    data=json.loads(path.read_text());data['worker_queue_selection']={'schema':1}
    write_json(path,data);checksum(payloads['worker'])
    manager=tmp_path/'manager.exe';manager.write_bytes(b'manager')
    with pytest.raises(ValueError,match='0.6.7'):
        build(VERSION,base(tmp_path),payloads,manager,snapshot(tmp_path),tmp_path/'out')
