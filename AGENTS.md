# Working on this project

Read [README.md](README.md) for the project overview,
[docs/deployment.md](docs/deployment.md) for the operator/agent checklist, and
[docs/recovery.md](docs/recovery.md) before repairing interrupted setup or sends.

The owner has confirmed that the connector has been tested. Synthetic fixtures
describe the automated test method and protect camera privacy; they do not mean
that the project lacks deployment testing. Keep new reports specific to the
checks you performed and preserve the documented delivery-evidence distinctions.

For deployment work, use the staged checklist and completion criteria in the
deployment guide. Identify the target host and the owner's saved configuration
before installing or changing service state. Development and documentation work
use synthetic systems; they do not activate surveillance.

Keep camera access and bot identity separate from development chat identities.
Tests must use synthetic media and mocked MQTT, HTTP and wn processes. Do not
view, export or send a user's camera recordings, change their surveillance
profile, or reboot/shut down their machine without their explicit instruction.

Preserve durable enqueue boundaries and unknown-effect reconciliation. A timeout
is not evidence that an upload or send failed. Never label enqueue or relay
acceptance as confirmed phone delivery. Keep secrets and machine state out of Git.

Run `python -m pytest` and inspect the full diff before publishing changes.
Pin MDK compatibility to actual command/JSON schemas, not guessed CLI flags.
