# Frigate → Marmot alerts

Send private Frigate person alerts to a Marmot group in White Noise. Recording
and recognition stay on your Linux computer. A dedicated bot sends an encrypted
snapshot, followed by buffered video when the visit ends. Home Assistant is not
required.

**Status: initial implementation, validated with synthetic events and media.**
An actual Frigate installation, relay publication and phone delivery still need
the owner's verification. This is a community integration, not an official
Frigate or MDK component.

MIT license. Dependencies retain their own licenses. MDK is used through its
official CLI rather than copied protocol or encryption code.

## What it does

- Subscribes to `frigate/reviews` with MQTT QoS 1 and a persistent client ID.
  Jobs and deduplication survive restarts in SQLite with WAL and full durability.
- Queues person-alert snapshots with a 15-second recognition allowance. Separate
  snapshot and video workers keep video preparation away from the snapshot queue.
- Queues video covering 60 seconds before the incident and 120 seconds after it,
  waiting another 20 seconds for recording segments to settle. Long incidents
  become contiguous parts of at most five minutes, within video-frame timing.
- Reconciles the previous 24 hours of reviews on startup and every five minutes,
  plus older unfinished incidents already known locally. Subscriptions begin
  before the scan; idempotent ingestion handles overlap.
- Optionally suppresses completed visits containing only explicitly registered
  people whose final recognition aggregate is at least 0.95. Unknown, uncertain,
  unresolved or missing recognition data keeps the alert.
- Checks Frigate, authenticated MQTT, Armed camera processing, disk space and
  the bot daemon every 30 seconds. A fault lasting three minutes queues one
  warning and one recovery when resolved. Warning state survives restarts.

Frigate controls are read-only. The connector never changes the owner's saved
Armed/Disarmed profile. Disarmed inactivity is expected. Phone alerts require an
outbound Internet connection to the relays and attachment store; camera capture
does not require exposing your Frigate instance to the Internet.

## Prerequisites

- Linux, Python 3.11+, FFmpeg with `libx264` and AAC, systemd user services,
  and an existing Frigate installation. API compatibility was inspected against
  Frigate 0.16.3; validate newer versions before relying on unattended operation.
- An existing Mosquitto broker with a dedicated authenticated read-only account.
  Prefer TLS, especially across machines. Its ACL needs subscribe/read access to
  `frigate/reviews`, `frigate/tracked_object_update` and `frigate/available`.
- Person alerts, snapshots and recording enabled in Frigate by the owner. Keep
  enough **continuous recording** to cover the full pre/post window. Motion-only
  retention can discard needed segments; the connector refuses incomplete video
  rather than claiming it sent the full incident.
- A White Noise recipient with a public `npub` and a published Marmot KeyPackage,
  or an existing Marmot group containing the dedicated bot.

A small 8 GB Linux computer can run this notifier, but available CPU, Frigate
detection load and video encoding determine throughput. Only one video worker
encodes at a time. Rust compilation of MDK has separate toolchain, memory and
disk requirements. No particular mini-PC performance has been measured here.

## Install

Clone this repository on the camera PC. Install Python's venv support, FFmpeg,
Git and the Rust toolchain needed by the pinned MDK source. Use a new directory
for the owner-run MDK build:

```sh
git clone https://github.com/Datawav/frigate-marmot.git
cd frigate-marmot
bash tools/build-mdk.sh "$PWD/../frigate-marmot-mdk-build"
python3 install.py --mdk-bin-dir "$PWD/../frigate-marmot-mdk-build/bin"
```

The MDK reference is commit
[`1016a7626341729d2c47188e63b81fa14a3d48af`](https://github.com/marmot-protocol/mdk/tree/1016a7626341729d2c47188e63b81fa14a3d48af),
the requested 0.13.0 reference. The build helper checks the source revision and
records binary SHA-256 hashes. Installation and configuration verify those
hashes and the revision manifest. A manifest is local build provenance, not an
independent publisher signature.

Installation creates a private virtual environment, copies the pinned binaries,
writes user units and verifies their syntax. It starts no services and overwrites
no existing configuration, credentials or profile. It needs no root access.

Edit `~/.config/frigate-marmot/config.toml` and keep it mode 0600. Set the actual
Frigate URL, broker host, CA certificate, camera names and private password file.
If using Frigate's authenticated endpoint, provide its bearer JWT through
`frigate.token_file`; renew it before expiry. A trusted same-host read-only HTTP
API is allowed only with explicit `allow_http = true`. Plaintext MQTT similarly
requires explicit `allow_plaintext = true`; TLS is the default.

Create the broker password file without putting the password in command-line
arguments or shell history:

```sh
umask 077
read -r -s -p 'MQTT password: ' mqtt_password
printf '\n'
printf '%s' "$mqtt_password" > ~/.config/frigate-marmot/mqtt-password
unset mqtt_password
```

Point `health.profile_file` at the owner's existing private JSON state with
`{"profile":"Armed"}` or `{"profile":"Disarmed"}`. The connector reads but never
creates or edits that file. If there is no saved profile yet, the owner can create
one explicitly as **Disarmed**. This file must reflect the actual Frigate profile;
it is not an arming switch. Leave an existing saved profile unchanged.

### Bot and recipient configuration

The generated services use a dedicated home at
`~/.local/state/frigate-marmot/bot`, its own Unix socket, and the two discovery
and default account relays:

- `wss://relay.eu.whitenoise.chat`
- `wss://relay.us.whitenoise.chat`

Start only the bot daemon, then configure the recipient using their public key:

```sh
systemctl --user daemon-reload
systemctl --user start frigate-marmot-daemon.service
~/.local/share/frigate-marmot/venv/bin/frigate-marmot configure --recipient NPUB
# equivalent from a checkout with the project installed:
python3 configure.py --recipient NPUB
```

Configuration creates a new bot identity, fetches the recipient's published
KeyPackage, creates a `Camera alerts` group and verifies local membership. It
never asks for the owner's personal private key. Accept the new invitation in
White Noise before expecting alerts. If KeyPackage lookup fails, publish or
refresh one in your client and retry configuration.

For an existing group, first add the bot using its public account identifier,
accept that invitation in the dedicated CLI home if needed, then run:

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot configure --group HEX_GROUP_ID
```

The group must contain the bot with active local membership. To create the bot
before inviting it into an existing group, follow [manual identity setup](docs/recovery.md#manual-identity-setup).
Configuration pins the destination to this state directory. Use a separate state
directory for another recipient; pending jobs must never silently change destination.

Attachments use the group's MDK encrypted-media settings. An optional
`marmot.media_server` overrides the upload endpoint with an explicitly selected
HTTPS Blossom server. The server receives encrypted blobs, not plaintext camera
media, but it still sees connection timing, ciphertext sizes and the bot's upload
identity. Choose a store you trust, optionally self-hosted.

### Enable boot startup

After recipient configuration succeeds, the owner enables services and user
lingering. Depending on the distribution, lingering may require local admin
authentication:

```sh
systemctl --user enable --now frigate-marmot-daemon.service frigate-marmot-notifier.service
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
systemctl --user status frigate-marmot-daemon.service frigate-marmot-notifier.service
```

The notifier's `ExecCondition` requires a successful configuration bound to the
current config bytes. After editing settings, stop the notifier, rerun
configuration for the same destination, and start it again. Neither installation
nor setup reboots or shuts down the host.

## Recognition and timing choices

Default `snapshot_mode = "early"` sends the snapshot after 15 seconds. A visit
cannot be known to contain only registered people until it ends, so an early
snapshot is never retroactively suppressed. With `suppress_known = true`, that
mode suppresses only completed known-only video.

Choose `snapshot_mode = "after_visit"` together with `suppress_known = true` to
suppress both snapshot and video for known-only visits. This delays the snapshot
until at least 15 seconds after the visit ends. `registered_people` is an explicit
allow-list of Frigate recognition names. Suppression is off by default.

Recognition comes from final event `sub_label`/`data.sub_label_score` or matched
`tracked_object_update` face evidence. Detector `top_score`, face-box confidence
and a historical maximum are not identity confidence. A 0.95 threshold is an
application rule, not a guarantee that recognition is correct.

Video is downloaded using complete recording-boundary segments, then locally
decoded and trimmed to the planned interval. This avoids server integer-seek
truncation and keyframe preroll. Missing coverage or files over the configured
attachment size remain retryable failures. Recording retention must exceed the
maximum outage you want to recover from; reconciling metadata cannot resurrect
deleted footage.

## Reliability and delivery evidence

`frigate-marmot status` reports job counts without exporting message content:

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot status
```

Three outcomes mean different things:

1. **Durable enqueue:** MDK returned persisted message IDs, or local history
   reconciled an interrupted send. The connector will not upload/send it again.
2. **Relay publication:** a positive MDK `published` result proves relay
   acceptance. `wnd` owns retries for queued messages. Reconciled history alone
   does not prove publication, and this connector does not invent a delivery ACK.
3. **Confirmed phone delivery:** the owner saw that exact alert and attachment
   on the phone. Only that observation establishes phone delivery here.

Network failures before an external effect retry with backoff. Upload and send
intents are committed before calling MDK. A timeout or lost response after an
external effect becomes `uncertain`; sends reconcile using a unique message
marker and the correct sender/group. MDK 0.13.0 does not expose a connector
upload-idempotency key, so an ambiguous upload is held for explicit recovery.
This favors avoiding duplicate private-media uploads over claiming exactly-once
delivery. See [recovery](docs/recovery.md).

Every attempt uses owner-only temporary staging and removes it afterward.
Startup removes abandoned staging from interrupted attempts. MDK's own private
state/cache and encrypted blobs on the attachment store have their own retention;
deleting staging does not erase those copies. Protect and back up the bot state
as carefully as an account key: this headless setup uses MDK's **file secret
backend**, not encryption at rest. Use a dedicated OS account and full-disk
encryption where appropriate. Other processes running as that OS user can read it.

QoS 1 applies to the subscriber. It cannot upgrade Frigate publications originally
sent at QoS 0. API reconciliation is part of recovery, not optional extra assurance.

## Verify phone delivery yourself

Keep surveillance Disarmed during setup. Run synthetic tests first. When you
choose to test actual alerts, explicitly authorize/enable the intended Frigate
profile yourself, accept the group invitation and check phone notification
permissions. Perform one known incident, then verify the snapshot and every video
part in that group, both with the app open and with it in the background. Match
the incident and the short `[fm:…]` marker to avoid mistaking an old alert for
a new one. Recheck after restarting the notifier and after a network outage.

Developers must not use actual camera media to implement or test this project.
No real deployment, invitation acceptance or phone-delivery result is implied
by the CI badge or synthetic suite.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests use synthetic MQTT/API fixtures, a fake CLI and generated black video.
They cover deduplication, restart boundaries, unknown effects, conservative
recognition, buffered split timing, health warning persistence, installer
idempotence and user-unit contents. No real camera endpoint or bot account is used.

Further background: [Frigate MQTT](https://docs.frigate.video/integrations/mqtt/),
[Frigate recording](https://docs.frigate.video/configuration/record/), and
[the pinned official CLI guide](https://github.com/marmot-protocol/mdk/blob/1016a7626341729d2c47188e63b81fa14a3d48af/crates/cli/README.md).
