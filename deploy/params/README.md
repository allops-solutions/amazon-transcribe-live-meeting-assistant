# Stack parameter files

Each `<env>.json` is a snapshot of the CloudFormation parameters for that
stack, in the shape `aws cloudformation create-stack`/`update-stack --parameters
file://deploy/params/<env>.json` expects directly.

- `dev.json` — snapshot of the live `LMA-Dev` stack (`allops-genai-development`,
  135755363077), pulled 2026-09-16. Regenerate after any parameter change made
  through the console so this stays truthful:
  ```
  aws cloudformation describe-stacks --stack-name LMA-Dev \
    --query "Stacks[0].Parameters" --output json \
    | python3 -c "import json,sys; p=json.load(sys.stdin); json.dump(sorted(p,key=lambda x:x['ParameterKey']),sys.stdout,indent=2)" \
    > deploy/params/dev.json
  ```
  Then re-apply the `FILL_IN_FROM_SECRET_STORE` redaction below before committing.

- `prod.json` — **draft**, not yet applied to a real stack. Filled in from the
  locked decisions in `LMA-HANDOFF.md` where known; anything still open is
  marked `TODO`. Review it fully before the first prod `create-stack`.

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
