# Working on this project

Keep camera access and bot identity separate from development chat identities.
Tests must use synthetic media and mocked MQTT, HTTP and wn processes. Do not
view, export or send a user's camera recordings, change their surveillance
profile, or reboot/shut down their machine without their explicit instruction.

Preserve durable enqueue boundaries and unknown-effect reconciliation. A timeout
is not evidence that an upload or send failed. Never label enqueue or relay
acceptance as confirmed phone delivery. Keep secrets and machine state out of Git.

Run `python -m pytest` and inspect the full diff before publishing changes.
Pin MDK compatibility to actual command/JSON schemas, not guessed CLI flags.
