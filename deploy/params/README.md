# Stack parameter files

Each `<env>.json` records the chosen configuration for that environment. These
files do not automatically configure a running LMA stack and are not consumed
by `lma publish`. They use the format accepted by an explicit AWS CLI
`--parameters file://deploy/params/<env>.json` deployment, at which point their
values do affect the stack. Console updates use the values on the console's
parameter page, not these local files.

- `dev.json` — intended normal dev settings, initially based on a live snapshot
  from 2026-09-16. SSO is deliberately disabled here; dev was temporarily enabled
  for testing through the console. Do not overwrite the normal configuration with
  temporary test flags merely to mirror the live stack. For any CLI update,
  deliberately choose and review the auth mode before passing this file.

- `prod.json` — **draft**, not yet applied to a real stack. Filled in from the
  locked decisions in `/Users/ahmed/Documents/projects/live-meeting-assistant/PROJECT-STATUS.md` where known; anything still open is
  marked `TODO`. Review it fully before the first prod `create-stack`.
  Meeting records, transcripts and audio/video recordings are configured for
  180 days; summaries use the Kimi K3 override. CloudWatch logs are also set to
  180 days. These are separate parameters, explicitly set here.

The old ignored `dev-update.local.json` CLI update helper has been removed.
Console updates and publishing never depended on it. Do not reuse an old CLI
command that explicitly names that now-absent file.

## Secrets — never commit real values

`AcsConnectionString`, `ElevenLabsApiKey`, `SimliApiKey`, `TavilyApiKey`, and
`ZoomMeetingSdkClientSecret` are `NoEcho` CloudFormation parameters (CFN itself
masks them as `****` on describe-stacks, which is why a straight snapshot
can't recover them). Both files carry the placeholder
`FILL_IN_FROM_SECRET_STORE` for these — fill them in locally from the team's
secret store immediately before a deploy, and never `git add` a version of
either file with a real secret in it. If you need a working copy with real
values, keep it as an untracked `*.local.json` (already covered by the
`deploy/params/*.local.json` gitignore entry) rather than editing the tracked
file in place.
