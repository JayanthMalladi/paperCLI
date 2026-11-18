# Migration note: removal of the legacy verification path (spec §17)

PaperCLI is greenfield (decision B). There is no legacy `agent-papercli`
0.1.0 code in this tree to migrate. The removed legacy verification code
corresponds to **exit code 3**, which is reserved and must never be
repurposed silently.

If old inputs or flags that reference the removed verification path are
encountered, the CLI returns actionable migration guidance (exit 2)
rather than silently reinterpreting them. Removing the legacy verification
is an intentional breaking change; the new package and schema versions are
documented in `README.md` and the published JSON Schemas.

Sci-Hub was removed completely as part of this greenfield rebuild: no
adapter, flag, environment variable, config key, or documentation example.
