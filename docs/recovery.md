# Recovery and safe operation

Use the dedicated bot home for every `wn` command. Never substitute a remote-chat
home or import the owner's private key. Run commands as the OS account that owns
this installation. Examples use the default paths.

## Inspect without exporting media

```sh
systemctl --user status frigate-marmot-daemon.service frigate-marmot-notifier.service
~/.local/share/frigate-marmot/venv/bin/frigate-marmot status
~/.local/share/frigate-marmot/bin/wn --home ~/.local/state/frigate-marmot/bot \
  --socket ~/.local/state/frigate-marmot/bot/wnd.sock --secret-store file --json daemon status
```

Notifier logs deliberately omit payloads, response text and command output.
Treat daemon logs and state as private. Do not upload them blindly for support.
Never attach the SQLite database, configuration, bot home or credentials to a
public issue.

## Manual identity setup

The recommended recipient flow creates the identity itself. For an existing
group, an owner can instead create one identity in a new dedicated home:

```sh
systemctl --user start frigate-marmot-daemon.service
~/.local/share/frigate-marmot/bin/wn --home ~/.local/state/frigate-marmot/bot \
  --socket ~/.local/state/frigate-marmot/bot/wnd.sock --secret-store file --json create-identity
```

Record the public `account_id` locally, invite that bot from White Noise, and
use `wn --home … --socket … --secret-store file --account ACCOUNT --json groups invites`
and `groups accept GROUP` for that bot. Configuration normally insists on an
empty or previously provisioned home. To adopt this explicitly created identity,
use the `adopt-bot` command described below; it verifies exactly one local-signing
account and never selects an account from a shared home.

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot adopt-bot --group HEX_GROUP_ID
~/.local/share/frigate-marmot/venv/bin/frigate-marmot configure --group HEX_GROUP_ID
```

## Interrupted setup

`setup.json` persists identity/group creation intents. If a CLI response was lost,
read back `accounts list` or `groups list` from the same dedicated home. Do not
run another `create-identity` or `groups create` based on a timeout alone.

An interrupted identity creation automatically reconciles exactly one account
from its original private home. If no account can be proved, setup remains held.
For an interrupted group creation, use the owner recovery command to select the
already created group after verifying its bot and recipient membership:

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot recover-setup --group HEX_GROUP_ID
~/.local/share/frigate-marmot/venv/bin/frigate-marmot configure --recipient NPUB
```

## Interrupted upload or send

SQLite preparation leases can be recovered. Upload/send intents are external
effects and are never automatically replayed just because their lease expired.
An accepted upload's group-epoch reference survives a retry of its unsent message.
A stale epoch is retried with fresh encryption only when MDK definitively rejects
the **unsent** reference; already enqueued messages remain MDK's responsibility.

An uncertain send is searched in local bot history by its unique `[fm:JOB_ID]`
marker. A matching sender/group/message proves enqueue, not relay publication or
phone receipt. Unknown uploads cannot be looked up reliably through this CLI.
Use `inspect-jobs` to show identifiers and error codes without media or message text.
If the owner decides a retry is necessary despite possible duplicate delivery,
the explicit recovery command makes that tradeoff visible:

```sh
~/.local/share/frigate-marmot/venv/bin/frigate-marmot inspect-jobs
~/.local/share/frigate-marmot/venv/bin/frigate-marmot retry-uncertain \
  --job JOB_ID --acknowledge-possible-duplicate
```

Stop the notifier before this command and restart it afterward. It only resets
an uncertain job, preserves an accepted attachment reference, and never arms
surveillance. Do not reset an enqueued or published job. First wait for MDK's
publication retry and inspect the phone.

## Backups and upgrades

Stop both user services before taking a private backup of the entire state
directory, including MDK state and SQLite WAL. Keep configuration and broker
credentials separately protected. Restore a coherent complete snapshot to one
machine only; concurrently running copies of one bot state are unsupported.
An old backup can lose deduplication history or protocol state.

Stop the notifier and daemon before rerunning the installer for an upgrade.
Keep the old binaries, manifest, environment and a coherent state backup until
the new version is verified. The installer preserves settings; changed config
bytes require rerunning configuration. Unsupported MDK revisions fail closed.
No reboot or shutdown is needed, and none is authorized by these instructions.

Faults during an alert transport outage cannot warn a phone through that same
failed transport. Warnings persist locally for later delivery; inspect service
status during an outage. The system does not guarantee timely security response.
