# ProdCast Manager 0.6

Start with **[GUIDE-EN.md](GUIDE-EN.md)** for profile handling, adding Workers, cloned VMs, Ollama compatibility and known cleanup warnings. Russian guide: **[GUIDE-RU.md](GUIDE-RU.md)**.

LDAP/AD settings are applied when installing or updating App v0.5.0-rc.2. Three file selectors: release, Ollama archive, and AI model. Leave the Ollama archive field empty when Ollama is included in the complete ZIP. Encrypted backup export/restore and two VM reset modes are included.

New in 0.6: add Workers to an installed site and reuse older Ollama ZIP containers with identical trusted inner payloads. See **CHANGES-0.6-EN.md**. Existing 0.5 features remain available.


### Per-Worker queues

Select a Worker under **Servers & workers**, then choose one or more **Processed
tasks**: Model-driven (`workflows`), Data-driven (`workflows_data`), or Forecast
(`scenarios`, scenario runs). Choices are stored in the draft and site profile
as `hosts.workerN.execution_queues`, and written to the Worker's `site.json`
during installation, update or selected Worker repair. Saving alone does not
change a running service. Queue-only selective updates affect that Worker.
Retries retain the original choices, and consumer resume restores only the
selected queues. Existing profiles without the field retain all three queues.
A restricted selection requires a Worker package declaring
`worker_queue_selection: {"schema": 1}` and Manager 0.6.7 or newer. Unsupported
packages are rejected before remote changes. Assign at least one Worker to each
queue used by your installation.
