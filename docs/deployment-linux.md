# RSS-Zen Linux Deployment

## Supported operating model

RSS-Zen is deployed as **one active process on one Linux host**. The SQLite database and
its `-wal`/`-shm` sidecar files must live on a local persistent filesystem under
`/var/lib/rss-zen`; do not run multiple `serve` instances or place the database on a shared
network filesystem. The application also holds a database-adjacent advisory lock, so a second
service process exits safely.

## Guided deployment script

When the target Linux host already contains this source checkout, the quickest supported setup is:

```bash
cd /path/to/RSS-Zen
sudo bash scripts/deploy-linux.sh
```

The guide detects Debian/Ubuntu and RHEL/Rocky/AlmaLinux/Fedora, confirms before modifying
the machine, installs OS prerequisites plus `uv` and managed Python 3.13 when absent, then
creates an immutable application release under `/opt/rss-zen/releases` and installs the managed
Python runtime in `/opt/rss-zen/python`, so the non-root service does not depend on root's home
directory. It preserves existing
`/etc/rss-zen/rss-zen.toml`, `/etc/rss-zen/rss-zen.env`, and `/var/lib/rss-zen` data on reruns.

On its first run, it creates protected templates rather than prompting for or recording secrets:

- `/etc/rss-zen/rss-zen.toml` ? provider and feed settings;
- `/etc/rss-zen/rss-zen.env` ? API keys, mode `0640`, readable only by root and the `rss-zen` group;
- `/var/lib/rss-zen/` ? SQLite, exports, backups, and timer locks.

If either template still contains `replace-me` values, the script deliberately leaves services
stopped. Edit both files and rerun the same command; it then validates the units, initializes
the database, and enables the service plus export/backup timers. Use `--yes` only for a known,
reviewed deployment when the initial confirmation must be skipped:

```bash
sudo bash scripts/deploy-linux.sh --yes
```

The script intentionally supports one local-disk, systemd-managed host only. It refuses
unsupported distributions, systems not booted with systemd, and systemd versions below 247 before
changing the host. Version 247 is the minimum for the `LoadCredential=` mechanism used to keep the
source secret file root-only.

## Least-privilege model

The guided installer uses two deployment stages:

```bash
# One-time host bootstrap: root only.
sudo bash scripts/deploy-linux.sh bootstrap

# Normal releases: deployment account only.
sudo -u rss-zen-deploy bash /opt/rss-zen/source/scripts/deploy-linux.sh release
```

- `rss-zen` is a non-login service account. It owns only `/var/lib/rss-zen` and runs sync,
  translation, export, and backup.
- `rss-zen-deploy` owns only `/opt/rss-zen/source` and `/opt/rss-zen/releases`. It cannot directly
  modify `/etc`, install packages, or open the SQLite, backup, or root-owned secret files.
- `/etc/rss-zen/rss-zen.env` is `root:root`, mode `0600`. systemd reads it with `LoadCredential=`
  and exposes a per-service, read-only runtime copy instead; the application can use the injected
  environment values but cannot open the root-owned source secret file.
- `/etc/sudoers.d/rss-zen-deploy` grants the deployment account only one root-owned control
  wrapper, `/usr/local/sbin/rss-zen-deploy-control`. The wrapper accepts only fixed
  `activate`, `restart`, `status`, and `is-active` actions for RSS-Zen units; it accepts no
  user-controlled command, unit name, or argument and grants no shell, editor, unit-file,
  package-manager, or arbitrary-command elevation.

The units also drop Linux capabilities and use systemd sandboxing to limit filesystem, device,
namespace, process, and privilege access.

> **Trust boundary:** a user allowed to publish arbitrary application code must still be trusted with
> the service identity. Although `rss-zen-deploy` cannot directly read the secret file, it can release
> code that systemd later runs as `rss-zen`, and that code receives provider credentials through its
> environment. If code publishers must not have that level of access, do not grant them `release`;
> instead use signed, root-approved release artifacts or a CI-controlled promotion process.

## Prerequisites

- Linux host booted with **systemd 247+**; the guided script installs Python 3.13 itself.
- The source checkout containing `scripts/deploy-linux.sh` already exists on the host.
- Outbound HTTPS access to configured feeds and translation providers.
- A dedicated system account:

```bash
# The guided bootstrap creates both accounts and directories automatically.
# Service account: rss-zen (no login shell, owns only /var/lib/rss-zen data)
# Deployment account: rss-zen-deploy (owns only /opt/rss-zen source/releases)
```

Copy the production config to `/etc/rss-zen/rss-zen.toml`. Use absolute paths for the
database and exports, for example `/var/lib/rss-zen/rss-zen.sqlite3` and
`/var/lib/rss-zen/exports/daily.md`.

```bash
sudo install -o root -g rss-zen -m 0640 rss-zen.toml /etc/rss-zen/rss-zen.toml
sudo install -o root -g root -m 0600 deploy/systemd/rss-zen.env.example /etc/rss-zen/rss-zen.env
sudoedit /etc/rss-zen/rss-zen.env
```

All feeds and provider endpoints must use HTTPS. Private/loopback addresses and URLs with
embedded credentials are rejected. Feed `headers` accepts only non-secret, valid HTTP headers;
use `header_env` for `Authorization` or `Cookie` so values remain in the root-owned credential
file rather than TOML. The optional `curl` fetcher validates every redirect, permits HTTPS only,
and applies the configured response-size limit.

## Install units

```bash
sudo install -m 0644 deploy/systemd/rss-zen.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen-export@.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen-export-daily.timer /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen-backup.timer /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen.conf /usr/lib/tmpfiles.d/rss-zen.conf
sudo systemd-tmpfiles --create rss-zen.conf
sudo systemctl daemon-reload
sudo systemctl enable --now rss-zen.service rss-zen-export-daily.timer rss-zen-backup.timer
```

Validate unit syntax before enabling it on a new host:

```bash
sudo systemd-analyze verify /etc/systemd/system/rss-zen*.service /etc/systemd/system/rss-zen*.timer
```

## Operations

```bash
sudo systemctl status rss-zen.service
sudo journalctl -u rss-zen.service -f
sudo -u rss-zen /usr/local/bin/rss-zen status --config /etc/rss-zen/rss-zen.toml
sudo systemctl list-timers 'rss-zen-*'
```

### Optional local health-check timer

The health-check timer is intentionally **not enabled by the installer**. It runs only the local
`rss-zen doctor --json` contract and does not contact feeds/providers or restart the service. Enable
it when systemd/journald failure state is already monitored by your alerting system:

```bash
sudo install -m 0644 deploy/systemd/rss-zen-healthcheck.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/rss-zen-healthcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rss-zen-healthcheck.timer
sudo journalctl -u rss-zen-healthcheck.service -f
```

A warning-only doctor result exits 0. Any doctor error exits 1, causing the oneshot unit to fail
for observability; it does not restart `rss-zen.service`.

### Optional Feishu delivery worker

The installer places `rss-zen-delivery.service` and `rss-zen-delivery.timer` on the host but never
enables them. Before staging activation, keep `[feishu].enabled = false`, inspect local work without
claiming or sending it, and verify doctor output:

```bash
sudo -u rss-zen /opt/rss-zen/current/.venv/bin/rss-zen delivery-run --dry-run --json \
  --config /etc/rss-zen/rss-zen.toml
sudo -u rss-zen /opt/rss-zen/current/.venv/bin/rss-zen doctor --json \
  --config /etc/rss-zen/rss-zen.toml
```

Only after the custom-app permissions, root-owned credential values, and `chat:` target have been
approved should an operator set `[feishu].enabled = true`, run one explicit staging
`delivery-run --json`, confirm the message and persisted delivery state, and then enable the timer:

```bash
sudo systemctl enable --now rss-zen-delivery.timer
```

The timer wakes one bounded worker batch per minute and is persistent across downtime. It does not
build editions or invoke Pi; it sends only already-rendered, hash-verified outbox artifacts.

### Optional topic deadline coordinator

The installer also places `rss-zen-deadline.service` and `rss-zen-deadline.timer` without enabling
them. Configure and review one or more versioned `[[topics]]`, then preview an injected UTC time or
the current clock without changing the database or filesystem:

```bash
sudo -u rss-zen /opt/rss-zen/current/.venv/bin/rss-zen deadline-run \
  --dry-run --json --config /etc/rss-zen/rss-zen.toml
```

The coordinator timer uses an explicit `Asia/Shanghai` calendar and wakes every minute. It creates
an edition only after each topic's preparation window opens, is idempotent across repeated wakes,
and catches up for the current local date after downtime. It performs no feed/provider/Feishu
network calls; it only renders and enqueues local editions. Enable it only after topic dry runs,
Feishu staging delivery, and fallback content have been approved:

```bash
sudo systemctl enable --now rss-zen-deadline.timer
```

Enable the deadline and delivery timers separately so operators can stop generation or sending
without coupling their failure domains.

`SIGTERM` and `SIGINT` stop scheduling new work and allow active bounded requests to finish;
`TimeoutStopSec=120s` bounds shutdown. Feed synchronization retries transient fetches, while
translation retries are persisted in SQLite with bounded exponential backoff. AnySearch
extraction remains manual and is never scheduled by `serve`.

## Backup and restore

The backup timer runs daily at 02:30 and writes verified snapshots to the `[backup].directory`
configured in `/etc/rss-zen/rss-zen.toml` (by default `/var/lib/rss-zen/backups`). A backup is
generated through the SQLite backup API and checked with `PRAGMA integrity_check` before
publication. Retention applies both `[backup].retention_days` and `[backup].retention_count`;
the newest successful backup is always retained.

To restore a backup:

```bash
sudo systemctl stop rss-zen.service
sudo sqlite3 /var/lib/rss-zen/backups/rss-zen-YYYYMMDDTHHMMSSZ.sqlite3 'PRAGMA integrity_check;'
sudo install -o rss-zen -g rss-zen -m 0640 /var/lib/rss-zen/backups/rss-zen-YYYYMMDDTHHMMSSZ.sqlite3 /var/lib/rss-zen/rss-zen.sqlite3
sudo systemctl start rss-zen.service
```

Keep the previous database file until the service starts cleanly. Local backups do not protect
against loss of the host or its disk; configure an external backup target separately if that
risk matters.

## Upgrade and rollback

1. Back up the database and config.
2. Stop the service.
3. Install the new locked application build.
4. Start the service; startup applies forward-only migrations and creates a pre-migration copy.
5. Check `journalctl` and `rss-zen status`.

Rollback application code only when the previous version understands the current schema.
Because schema migrations are forward-only, restore the matching pre-upgrade database copy if
a code rollback requires an older schema.
