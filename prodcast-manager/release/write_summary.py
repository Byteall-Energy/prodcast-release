"""Write the final human-readable GitHub Actions release summary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


snapshot_path = Path(sys.argv[1])
assets_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])
state = sys.argv[4]
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
assets = Path(assets_path)
version = snapshot["release_version"]
lines = [f"# ProdCast {version}", "", "## Source snapshot", ""]
for name in ("app", "agent", "worker", "ai", "release"):
    item = snapshot["components"][name]
    lines.extend([f"### {name.title()}", "", f"commit: `{item['source_commit']}`", ""])
lines.extend(["## Base offline release", "", snapshot["base_release"]["tag"], "", "## Artifacts", ""])
for name in (
    f"ProdCast-{version}-complete.zip",
    f"ProdCastAgent-{version}.exe",
    f"ProdCast-Manager-{version}.exe",
    "release-manifest.json",
):
    path = assets / name
    lines.extend([name, f"SHA256: `{digest(path)}`", ""])
if state == "published":
    lines.extend(["## Tags created", ""])
    for item in snapshot["components"].values():
        lines.append(f"- `{item['repository']}/{version}`")
    lines.extend(["", "## Release", "", "published"])
else:
    lines.extend(["## Release", "", "build-only; no draft, tags, or release created"])
Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
