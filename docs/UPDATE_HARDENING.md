# Single-stack compatibility and safety repair

A read-only independent review of the first single-stack revision identified six
material issues. All were accepted and reproduced with native Compose parsing or
recording regressions before correction. The production failure's exact cause
remains unknown: only a generic RehearsalError was recorded after a successful
preflight. These defects are not presented as a proven diagnosis of that VM.

| Finding | Repair | Evidence |
|---|---|---|
| Private values and inherited env affected template interpolation | Parse official templates using neutral binds/DB placeholders and explicit version/environment | Native parser accepts dollar/braces/quotes/Unicode values and conflicting inherited version |
| Named volumes declared only after template parsing | Neutral bind placeholders during parsing; original mounts/volume declarations copied together afterward | Native declared named-volume fixture succeeds and preserves actual names |
| Errors lost stage, operation and exit code | Safe structured failure fields and application-bound receipt diagnostics; raw command/stderr stays hidden | Config/pull/dump/up failures distinguished; private canary never exposed |
| Public original Compose mode inherited by secret-filled resolved JSON | New runtime mode0600, private temporary creation, separate original metadata | Recording apply starts with source0644 and publishes0600 |
| Runtime/config mount drift not checked | Compare actual bind/named-volume identities, destination and read/write mode before mutation | Drift rejected before API/stop; matching bind+named mounts accepted |
| Absent site ports replaced by upstream default publication | Preserve absence and loopback mappings; apply refuses changed publication | Native no-ports/loopback fixtures and recording added-port rejection |

The same review exposed a broader literal-materialization gap: Compose's config
output escapes dollar signs, while reparsing raw JSON can interpolate them again.
The adapter now decodes escaped resolved output and explicitly encodes literals
only when writing an executable Compose document (including old-image recovery
configuration). Native config roundtrip preserves dollar/braces/Unicode settings.

## Verification boundaries

- Unit/recording cases never deploy or mutate an existing stack.
- Native parser regressions run Docker Compose config only; no daemon lifecycle.
- Developer live acceptance uses explicitly disposable synthetic data, including
  a PostgreSQL credential with dollar/braces/quotes/Unicode, no published app ports,
  a bind library/DB and named ML-cache volume. It exercises actual migration,
  logical-dump restoration, login, runtime-mount checks and post-migration fault
  retention. It is not a production rehearsal before every update.
- Hard SIGKILL/host-crash semantics are not certified by a KeyboardInterrupt test.
  Durable markers remain before writer stop and before target migration; failures
  retain state and never automatically downgrade or clear pending state.
- Separate photo/video disaster backup remains external.

[Synthetic proof for the repaired runtime](single-stack-hardening-acceptance.json)
is recorded separately from the historical single-stack acceptance. No reviewer clean verdict or VM deployment is inferred
from test counts or an independent model's text.
