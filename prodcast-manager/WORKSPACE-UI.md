# Manager workspace

The desktop application now uses a site workspace with seven sections. Existing site profiles, vaults, release-integrity checks and deployment engines are retained. This change does not introduce Worker domain service accounts or hostname support for VM endpoints.

New sites have empty server addresses, App URL and management-PC IPv4. Grey documentation-range examples are separate visual hints; they are never entry values and cannot be saved or used for connections. Existing profiles retain their configured addresses. The main window, custom dialogs, nested windows and future Tk dialogs use the shared ProdCast icon; Windows process identity is set to ProdCast Manager.

The default desktop renderer now uses local HTML/CSS inside bundled Qt WebEngine. It follows the preview's application shell, sidebar, grouped fields, conditional credential inputs and in-window dialogs. All styles, scripts and images are local; no web server, CDN or separately installed browser runtime is required. The Python deployment engines remain responsible for server mutations and validation. The previous Tk workspace remains available as source for migration/regression coverage; it is no longer the default GUI entry point.

The single-file EXE is larger because it includes Qt/Chromium. Browser navigation, remote asset requests, downloads, plugins and persistent browser storage are disabled. Only the fixed local interface can reach the allowlisted WebChannel API. File selection uses branded desktop dialogs; operation confirmations and settings forms use the HTML interface. Initial administrator credentials can be viewed while an operation runs, without saving/revalidating the profile.

Builds use `release/manager.spec`, which bundles the Qt wheel's C++ redistributables consistently at the onefile root and nested locations. This avoids loading Python's older `vcruntime140.dll` before Qt. The build also excludes unrelated ICU DLLs discovered on PATH, preserving Qt's use of the Windows ICU API. `build.ps1` runs the controller/engine tests, `release/check_html_ui.py` against the real renderer/bridge, and the packaged HTML smoke test with a timeout. Qt package versions are pinned in both runtime and build requirements. Existing workflow version stamping and one-EXE artifact naming continue to apply.

## Everyday use

- **Overview:** current local site, local deployment receipt, unresolved journal count and links to status checks. Remote health is never inferred from a completed local operation.
- **Servers & workers:** select a row; Password shows the SSH password directly, SSH key shows private key and optional passphrase. Linux adds explicit sudo choices; Windows does not. Fingerprint verification is separate, asynchronous and requires explicit trust. Test selected server checks sign-in and privileges without staging an agent or requiring other hosts to be configured.
- **Install / update:** Site → Package → AI → Review. The expected hash is the SHA256 of `release-manifest.json`, not of the ZIP. Local package verification does not bypass the engine's final compatibility and ownership checks. Update can intentionally omit AI packages using Keep existing AI; installation still follows the site's AI setting and release requirements.
- **AI:** separate runtime/model package selection and retry action. Core package selection is shared with Install / update.
- **App settings:** general/network settings, HTTPS import/deployment, domain login, activation. Domain-login connection, lookup and user tests are separate tabs. LDAP without TLS has its acknowledgement beside the transport choice. Successful tests do not mean settings are deployed or that container trust matches VM trust.
- **Backups & recovery:** server snapshots, encrypted portable export, data restore, Manager-profile recovery, component repair and full reset have distinct descriptions. Portable archive forms keep path, password and confirmation together. Existing engine checks still validate archive identity/compatibility before restore.
- **Operations:** a real history list includes core installation/update, Worker, AI, certificate, checks and maintenance runs. The current operation opens automatically. All targets is the default; target and summary filters are available. Full combined logs, selection copying, follow-output control and log-folder access remain available. App administrator access stays in the header and remains read-only during operations.

## Saving and recovery

`Save draft` stores incomplete non-secret inputs as `ui-draft.json`; it does not replace the validated `site.json` or deploy anything. Passwords must be saved with **Save server access**. Explicitly emptying a previously loaded secret clears it; changing authentication methods does not erase the alternate method's session draft. Before leaving a modified site, Save / Discard / Cancel is offered.

New local operation records are stored in `prodcast-data/logs/operations`. They are diagnostic records, separate from deployment ownership journals. Their `finished` status means the local action returned, not a live health verdict. A run left `running` after restarting Manager is displayed as interrupted/unknown. Earlier Manager logs remain available in the logs folder; they are not retroactively attributed to individual actions or sites.

Full log means the Manager event log plus the redacted remote diagnostic excerpts collected on action failures. It does not claim to stream every server transcript continuously. Captured App/DB/Worker/AI diagnostic excerpts appear under the relevant target filter; the original diagnostic files also remain in the logs folder.

All six existing journal families are inspected, including unreadable files. Recovery instructions identify the action that must be repeated. The existing stop action supports core/AI ownership release; it does not claim to cancel Worker, certificate or maintenance transactions. Those journals remain visible and require their supported resume path. Stopping is not rollback.

After successful core deployment, `ui-deployed.json` records the release, manifest and a keyed settings signature. It contains no credentials. It permits comparison of saved App settings with the last successful deployment. Existing profiles without a receipt show **unknown**. The receipt is not a substitute for remote health inspection.

An installed, topology-bound profile cannot silently become a new site by editing its core address or ID. Use New site. Uninstalled local profiles retain the existing separate-profile behavior when endpoints change. Existing worker IDs remain stable across removal and re-addition; initial worker count is for new sites, with explicit Add / Remove for installed sites.

## Validation

LDAP and LDAPS both allow the search account and password to be omitted together. App deployment keeps the existing user-bind contract and group restrictions. Without a search account, directory diagnostics require temporary test-user credentials; these are never saved and anonymous lookup is not attempted. Clearing the search account in the HTML interface also removes its encrypted saved password. LDAP without TLS still requires explicit acknowledgement; LDAPS certificate verification remains mandatory.

Regression coverage includes both authentication methods, saved password retention, trusted-key confirmation, Linux privilege selection, incomplete drafts without secrets, legacy profile separation, stable Worker IDs, explicit AI omission, persistent operation history, target filtering (including errors), concurrent log append, interrupted/corrupt records, all pending-journal families, deployment receipts, bilingual labels and directory test-result handling. Deployment-engine tests remain part of the full suite.
