# Security reporting

Report a vulnerability privately to the repository owner before publishing
camera data or exploit details. Use GitHub's private vulnerability reporting if
available. Public issues must contain only synthetic reproduction data.

The threat model protects camera media in transit through Marmot and MDK
encrypted attachments. It does not protect secrets from another process running
as the same OS user, a compromised camera PC, a compromised recipient phone, or
traffic-analysis metadata visible to relays and blob stores. File-backed bot
keys are owner-only but not encrypted at rest by this connector.

No endpoint is exposed by this project. Keep Frigate and MQTT local/firewalled,
prefer authenticated TLS, patch the host and use a dedicated restricted bot
identity. Read-only broker ACLs should deny publication and unrelated topics.
Recognition-based suppression is optional and conservative; it is not an
authentication mechanism or a substitute for alarm-system certification.
