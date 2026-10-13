# Deployment guide for operators and coding agents

This guide connects an existing [Frigate](https://docs.frigate.video/) recorder
to a dedicated Marmot bot and a White Noise recipient. Use it alongside the
[installation instructions](../README.md#install) and [recovery guide](recovery.md).

## Required inputs

Collect these from the owner or their existing private configuration:

| Input | Where it is configured |
|---|---|
| Frigate API URL and any bearer-token file or CA certificate | `[frigate]` in `config.toml` |
| Authenticated MQTT host, port, username, password file and CA | `[mqtt]` |
| Camera names and the existing private Armed/Disarmed profile file | `[health]` |
| Recipient's public `npub`, or an existing group containing the bot | `configure --recipient` or `configure --group` |
| Explicitly registered recognition names, if suppression is wanted | `[alerts]` |
| An optional HTTPS encrypted-attachment store | `marmot.media_server` |

Use public recipient identifiers. Create a dedicated bot identity; the owner's
personal private key is unnecessary. Keep passwords and tokens in owner-only
files, outside Git and command-line arguments.

## Preserve existing state

- Confirm which Linux host and OS account own this installation.
- Preserve the owner's configuration, credentials and saved surveillance profile.
  Read the profile without changing it. The profile file must reflect the actual
  Frigate profile; the notifier reads it as health-check context.
- Keep bot identity and Marmot state separate from coding-agent chat accounts.
- Inspect an existing `setup.json` or `destination.json` before repeating setup.
  A missing command response is an uncertain outcome, not proof of failure.
- Use synthetic media and mocked MQTT/API/CLI events for development and tests.
  Actual camera viewing, export, analysis or sending requires explicit owner
  authorization for that operation. Never arm surveillance automatically.
- A host reboot or shutdown requires explicit owner permission.

## Deployment stages

### 1. Inspect prerequisites

Check Python 3.11+, FFmpeg, systemd user-service support and the pinned MDK build
requirements. Confirm that Frigate and authenticated Mosquitto already exist.
Read metadata and service status rather than camera media or credential values.

Frigate must provide person alerts, snapshots and continuous recording covering
the full 60-second pre-buffer and 120-second post-buffer. Ask the owner to resolve
missing recording coverage; the connector does not change Frigate settings.

### 2. Build and install files

Follow the README's [build and install commands](../README.md#install).
`install.py` creates the private environment, pinned binaries, config template and
user units. It preserves existing configuration and starts no services.

If an earlier build or install was interrupted, inspect the existing directory
and provenance manifest before repeating commands. The MDK build helper expects
a new directory; its source commit and binary hashes must match installation.

### 3. Configure the private files

Edit `~/.config/frigate-marmot/config.toml`. Provide the actual inputs listed
above, keep private files mode 0600 and bot/state directories mode 0700, and
preserve existing settings. Select snapshot timing and recognition suppression
with the owner; suppression is off by default.

The generated units use the default installation paths. If the owner chooses
another config or state directory, the service commands, bot home and socket must
all point to that same installation.

### 4. Start the dedicated daemon and configure a destination

```sh
systemctl --user daemon-reload
systemctl --user start frigate-marmot-daemon.service
~/.local/share/frigate-marmot/venv/bin/frigate-marmot configure --recipient NPUB
```

For an existing group, follow [manual identity setup](recovery.md#manual-identity-setup)
and use `configure --group HEX_GROUP_ID`. Choose one destination mode.

The configuration command returns public bot/group identifiers and writes its
private destination marker. The recipient accepts a new invitation in White
Noise. Interrupted identity or group creation uses the recorded recovery path;
never create another account or group solely because a response was lost.

### 5. Check configuration, then enable user services

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot check-config
systemctl --user enable --now frigate-marmot-daemon.service frigate-marmot-notifier.service
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
systemctl --user is-active frigate-marmot-daemon.service frigate-marmot-notifier.service
~/.local/share/frigate-marmot/venv/bin/frigate-marmot status
```

`check-config` must exit zero. It verifies the configured destination and current
config-byte binding. The notifier's service startup uses the same check.
Lingering keeps user services available after logout and at boot; it may require
local administrator authentication. Service startup leaves the surveillance
profile unchanged.

## Completion checklist

- The Frigate and MQTT inputs match the intended existing deployment.
- Configuration succeeds for the dedicated bot and intended group.
- The recipient has accepted the invitation where needed.
- Both user services report active, lingering is enabled, and the owner's saved
  profile and credentials are preserved.
- Status JSON is inspected for queued or uncertain jobs and invalid MQTT payloads.
- Any owner-authorized delivery check matches the intended alert and attachments
  on the phone, including background notification behavior.

Record each outcome separately: configured, services active, message durably
enqueued, relay publication observed, and phone receipt observed. A CI pass or
`enqueued` job cannot establish receipt of a particular message on a phone.
Deployment testing already confirmed for the project does not replace checking
the destination and permissions of a new installation.

## Read-only diagnostics and recovery

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot status
~/.local/share/frigate-marmot/venv/bin/frigate-marmot inspect-jobs
systemctl --user is-active frigate-marmot-daemon.service frigate-marmot-notifier.service
```

`status` returns JSON job counts, the invalid-payload count and a
`phone_delivery` field describing the connector's lack of per-message phone
acknowledgements. `inspect-jobs` returns uncertain job IDs, kinds and error codes.
Neither command exports message text or media.

| Job state | Meaning and next step |
|---|---|
| `scheduled` / `preparing` | Waiting for timing, a safe retry or local media preparation. |
| `uploaded` | An accepted attachment reference is retained for its unsent message. |
| `enqueued` | MDK accepted the message; let MDK retry publication. |
| `published` | MDK reported relay acceptance; check the intended phone separately. |
| `uncertain` | An upload/send response was lost; inspect and reconcile its existing effect. |
| `suppressed` | A completed visit met the configured registered-person rule. |

Use the [recovery guide](recovery.md) for uncertain setup, uploads, sends and
upgrades. An explicit `retry-uncertain --acknowledge-possible-duplicate` trades
duplicate risk for an owner-directed retry; obtain that decision from the owner.
Keep raw logs, databases, bot state and credentials private. Report metadata and
the checks performed rather than uploading camera media or secret files.

## Source map for maintainers

- `install.py`, `config.example.toml`, `configure.py`: install and configuration entry points.
- `frigate_marmot/cli.py`: command parsing, JSON status and owner recovery commands.
- `frigate_marmot/config.py`, `frigate_marmot/configure.py`: private files, destination binding and bot setup.
- `frigate_marmot/runner.py`: MQTT ingestion, startup reconciliation and supervision.
- `frigate_marmot/planner.py`, `store.py`: alert windows, recognition rules and durable jobs.
- `frigate_marmot/worker.py`, `frigate.py`: separate workers, media preparation and read-only API access.
- `frigate_marmot/marmot.py`: official MDK CLI calls and enqueue/reconciliation boundaries.
- `frigate_marmot/health.py`: persisted fault and recovery notifications.
- `tests/`: synthetic adapter, restart, worker, installer and supervisor coverage.

Run the [automated suite](../README.md#development) for source changes. Compare
documented commands to `frigate-marmot --help` and the actual CLI parser before
adding examples. Update these instructions when command behavior changes.
