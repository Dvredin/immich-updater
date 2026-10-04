# Retired Immich rehearsal and cold-checkpoint controllers

These modules preserve the pre-cleanup rehearsal/sample/resource and complete
filesystem recovery implementations. They are not a supported production entrypoint
and are never imported or copied by the active installer. The active implementation
is `single-stack-v1`, described in [the runbook](../docs/OPERATIONS.md).

The original bodies are retained, with only internal imports qualified as `archive.*`.
The old `risk_policy.json` is retained beside them. Retired controller fixtures also
remain in `tests/archive/` and `tools/archive/`; the old architecture documentation
is in `docs/archive/`. Those older fixtures are snapshots, not active CLI recipes.

`tests/legacy/` contains the 106 still-executable historical regression tests. Run
all current and historical regressions from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

The old synthetic migration runner is retained at
`tests/legacy/run_live_acceptance.py`; it is not invoked by default tests or by
installation. Never point a synthetic runner or recovery controller at an owner
installation. Existing failed-update journals remain a refusal in the active
updater; this archive is not permission to force automatic restore or downgrade.
