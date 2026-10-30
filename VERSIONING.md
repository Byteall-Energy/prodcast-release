# ProdCast release versioning and orchestration

Stable releases use exactly `vMAJOR.MINOR.PATCH`, for example `v0.6.1`.
The same immutable tag is created in `prodcast-release`, `prodcast-app`,
`prodcast-agent`, `prodcast-worker`, and `prodcast-ai` only after all artifacts
have been built and validated.

The tag is a historical snapshot marker: it identifies the exact commit used
in that ProdCast release. It is not a build input and must never be moved or
force-updated. Component release-candidate tags are obsolete and are not used
by the centralized workflow.

## Normal release

Run **Build and assemble ProdCast release** in `prodcast-release` and provide
only `version`. The workflow:

1. Resolves the current `main` HEAD of all five repositories once.
2. Rejects an invalid version, an existing release, or an existing tag in any
   repository, and rejects known pending/failing required CI on a selected HEAD.
3. Selects the newest earlier published stable Complete release that has the
   expected ZIP asset. `base_release` can override this selection.
4. Checks out every component by the frozen full SHA and builds App, Agent,
   Worker, AI, and Manager in parallel.
5. Replaces every dynamic component in the base Complete archive while retaining
   only its static/offline infrastructure.
6. Validates component provenance, allowlisted contents, sizes, checksums,
   required payloads, and the final Complete ZIP.
7. Creates a draft central release and uploads the verified assets.
8. Rechecks tag absence, creates the same tag at each frozen SHA, verifies every
   tag, and only then publishes the draft.

`build_only` stops after artifact validation. It does not create a draft, tags,
or a release.

For independent App, AI, Worker, Agent, or Manager artifacts without a Complete
archive or a base release, use **Build selected ProdCast components** instead.
See [COMPONENT-BUILDS.md](COMPONENT-BUILDS.md) for selection, source refs, and checks.

## Authentication

Prefer a GitHub App installed on all five repositories. Add these repository or
organization Actions secrets to `prodcast-release`:

- `PRODCAST_RELEASE_APP_ID`
- `PRODCAST_RELEASE_APP_PRIVATE_KEY`

The workflow creates a fresh installation token independently in each job. As a
fallback, omit both App secrets and create `PRODCAST_RELEASE_TOKEN` containing a
fine-grained personal access token. The App or PAT needs:

- Contents: read and write on all five repositories (checkout, release assets,
  and tag creation).
- Metadata: read (implicit for GitHub Apps and fine-grained tokens).
- Checks: read and Commit statuses: read when CI validation is enforced.

The workflow-level `GITHUB_TOKEN` remains read-only. The generated App token or
PAT is passed only to cross-repository checkouts and GitHub API/release
operations, is never persisted by checkout, and is never printed.

For additional protection, configure the `production-release` environment with
required reviewers and restrict the secret to that environment if the resolve
job is changed to use a separate read-only credential.

## Failure behavior

Build or validation failures create no tags and no release. A failure while
uploading the draft leaves no published release. A failure during cross-repo
tagging leaves the draft unpublished and never overwrites any tag; because tag
creation cannot be atomic across repositories, partially created tags require
operator investigation and must not be deleted or moved automatically. A
published release is never mutated by this workflow.
