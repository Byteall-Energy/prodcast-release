# Portable Agent builds

Run **Build ProdCast portable Agent** from Actions and enter one internal version,
such as `v0.6.5`, matching the complete-release version format.

The workflow checks out `prodcast-agent` main, runs
`release/build_windows.ps1 -Portable`, and uploads `Prodcast-Agent-v0.6.5`
for 7 days. The artifact contains only `ProdCastAgent-v0.6.5.exe`.
No Inno Setup installation or setup EXE is used.

The complete-release workflow checks out its frozen Agent SHA and runs the same
portable build and packaged execution smoke test. Its `agent-payload` artifact
contains only the portable EXE. Checksums, source provenance, the SBOM, and
portable usage instructions travel in the one-day `agent-assembly-metadata`
artifact and are recombined for integrity validation. The final standalone Agent
asset is `ProdCastAgent-vX.Y.Z.exe`; older setup EXEs are rejected as new inputs
and removed when carrying forward a previous Complete release.

The packaged smoke test uses an isolated saved profile and an upstream fixture;
it exercises notebook and graph execution, authentication, CORS, and the bundled
PETEX integration without opening first-launch setup or registering autostart.

The Agent-only workflow creates no Git tags, drafts, or GitHub releases.
Rebuilding an internal version does not require or reserve a platform release
version. It uses the existing `PRODCAST_RELEASE_APP_ID` /
`PRODCAST_RELEASE_APP_PRIVATE_KEY` credentials, or the existing
`PRODCAST_RELEASE_TOKEN` fallback, to read the Agent repository.
