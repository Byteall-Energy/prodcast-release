# Creating a ProdCast release

The centralized workflow builds a release directly from the latest `main` HEAD
of every ProdCast repository. Component release-candidate tags and pre-published
component packages are not used.

## Before the run

Ensure approved changes and required CI are complete on `main` in:

- `Byteall-Energy/prodcast-app`
- `Byteall-Energy/prodcast-agent`
- `Byteall-Energy/prodcast-worker`
- `Byteall-Energy/prodcast-ai`
- `Byteall-Energy/prodcast-release`

Configure the release GitHub App secrets or the `PRODCAST_RELEASE_TOKEN` PAT as described in
[`VERSIONING.md`](../VERSIONING.md). Do not create the target release or tag in
advance.

## Run the workflow

Open the `prodcast-release` Actions page, choose **Build and assemble ProdCast
release**, and supply a stable `version`, for example `v0.7.0`.

The optional `base_release` input pins an earlier published stable Complete
release for static/offline dependencies. Leave it empty to select the newest
valid earlier release automatically. Select `build_only` to build and validate
without creating a draft, tags, or published release.

Release workflow artifacts are retained for one day, including build-only
outputs. Download those outputs before they expire. Standalone Manager/Agent
build artifacts and Agent CI SBOM reports are retained for seven days. Published
release assets are separate from Actions artifacts and do not expire under this
policy.

The workflow freezes all five full commit SHAs before starting parallel jobs.
Every checkout uses its frozen SHA even if `main` moves later. It then builds:

- App backend, frontend, and gateway Linux/amd64 images and deployment archive.
- Windows/amd64 Agent installer and metadata.
- Windows/amd64 Worker package and metadata.
- AI Linux/amd64 image and deployment archive.
- A one-file Windows Manager using `prodcast-data/logs` and
  `prodcast-data/sites` beside the executable.

The assembler replaces App, Worker, and AI in the selected base. Agent and
Manager remain standalone release assets. Static/offline infrastructure is
carried forward only from the validated base.

## Publication order

After payload and checksum validation, the workflow:

1. Creates a draft `prodcast-release` release and uploads all verified assets.
2. Rechecks that the target tag is absent in every repository.
3. Creates that tag at each frozen source SHA and verifies the result.
4. Publishes the draft only after all five tags exist at the expected commits.

The core release assets are:

- `ProdCast-vX.Y.Z-complete.zip`
- `ProdcastAgentSetup-vX.Y.Z.exe`
- `ProdCast-Manager-vX.Y.Z.exe`
- `release-manifest.json`
- `release-manifest.json.sha256`
- `SHA256SUMS`
- `RELEASE-NOTES.md`

Agent metadata and SBOM files produced by its build are also uploaded. Complete
must contain no top-level Agent or Manager executable.

## Failure handling

A build, assembly, or validation failure creates neither tags nor a release. An
upload failure leaves the release unpublished. If cross-repository tagging
fails after some tags were created, the draft remains unpublished and the
workflow never deletes or moves tags automatically. Investigate that partial
snapshot before choosing a new release version.

Published releases and all release tags are immutable.
