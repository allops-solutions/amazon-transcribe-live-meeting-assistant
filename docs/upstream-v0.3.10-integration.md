---
title: "allOps upstream v0.3.10 integration"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. Licensed under the MIT License.

# Integration scope

This branch merges upstream v0.3.10 (`04fb5696`) into the maintained allOps fork,
not a replacement of the fork with stock upstream code. Production remains on
the previously deployed revision until the rollout gates below pass.

## Preserved allOps behavior

- Google Meet Virtual Participants, persistent profiles, and Calendar occurrence
  identity, schedule/update/cancel tools and duplicate prevention.
- Google Workspace SSO, administrator assignment, and company-wide meeting data
  visibility. Interactive VNC control remains owner/shared-user/admin restricted.
- On-demand summary generation, profile selection, model fallback, and preserving
  the last successful summary when generation fails.
- Per-meeting transcription language controls and quiet default announcements.
- CI deployment roles, application permissions boundaries, resource namespaces,
  private release artifacts and existing saved deployment configuration.

`TranscriptionEngine` remains a deprecated deployment-parameter alias so existing
CI configuration is accepted. The new `EnableMicrovmAsr` defaults to false; no
optional engine or voice integration is enabled merely by this merge.

## Upstream changes needing rollout attention

- The failed Kinesis-record discard queue needs the updated administrator-owned
  access policy in `deploy/ci/practical-access.yaml` **before** stack deployment.
  SQS grants are limited to this account/region's `LMA-*-DiscardedRecords` queues.
- ECS VNC now uses `createVncEdgeToken` and its new signing secret. The UI, token
  resolver and Lambda@Edge verifier must roll out together. Strict exact-path
  validation is preserved. The historical HMAC secret is retained, not deleted.
- MCP requests without a resolved caller now return 403, not synthetic admin
  access. Verify Quick's actual authentication path carries a user identity;
  the documented per-user API-key route supplies one. Do not restore the former
  identity-less administrator fallback to make an integration pass.
  The allOps REQUEST interceptor forwards the actual OAuth bearer token and the
  target verifies its signature, issuer, expiry, token type and MCP client ID.
  It never trusts user identity supplied as tool arguments. This preserves the
  existing Quick OAuth connector without restoring the insecure fallback.
- Read responses no longer expose MCP stored authentication configuration.
  Custom consumers must use `HasAuthConfig`, not the removed `AuthConfig` field.

## Release order and acceptance gates (updated 2026-10-19)

The isolated OAuth/Quick lifecycle checks below have passed. The owner explicitly
chose not to repeat isolated-dev live-meeting tests: remaining Google Meet,
recording, transcript and summary acceptance will use a disposable production
meeting after deployment. This is an accepted rollout risk, not a claim that those
paths have already passed on this release. The original dev environment stays up.

1. Complete release/configuration/rollback review and merge PR #21 into
   `allops-main` after its required checks pass. This branch is the approved
   production target for this fork (not upstream `develop`).
2. Before application deployment, apply and verify the scoped discard-queue
   permission from `deploy/ci/practical-access.yaml`. It is not installed merely
   by merging the PR; on 2026-10-19 the live provisioning role still lacked it.
3. Build privately through production CI/CD. Inspect the actual root/nested
   change set before execution; stop on unexpected replacements/deletions of
   stateful resources or changes to the active SSO pool. The current deployment
   script executes its change set immediately after printing it, so an explicit
   pre-execution inspection step must be arranged before launching deploy mode;
   do not mistake its printed summary for an approval gate.
4. Deploy the coordinated UI/API/edge/runtime revision. Verify Google SSO,
   video off/audio on, Kimi and on-demand summaries. Move Quick's connector and
   Flow action bindings from original dev to production, then authenticate each
   designated scheduler and verify their no-confirmation triggers.
5. Run one disposable production acceptance meeting: joining, transcript, audio
   recording, video default off, summary, owner email labels, company-wide
   transcript reads and owner/shared/Admin-only VP-browser control. Verify Quick
   schedule/repeat/update/explicit cancellation against actual backend state and
   an unattended trigger without review clicks. Also regenerate the existing long
   meeting only with permission to verify complete summary output, or an explicit
   output-limit failure with the previous summary preserved.
6. Finish on-demand dev create/update/feature-branch/confirmed-destroy support
   before retiring original dev. Historical AWS resource cleanup remains open;
   GLM evaluation and a signed-in Google Meet bot/logo are out of scope.

### Rollback baseline and constraints

- Current production root: `LMA`, account `009853297978`, region `us-east-1`;
  baseline revision `291d9c0c1ad0daac69f3f9df0c0cea8d835fa386`, CI run
  `38225965181`, attempt `1`. Healthy `CREATE_COMPLETE` and termination protection
  were verified on 2026-10-19.
- The previous private root template was verified present at
  `s3://allops-lma-ci-009853297978-production-us-east-1/releases/291d9c0c1ad0daac69f3f9df0c0cea8d835fa386/38225965181/1/lma-main.yaml`.
  Keep that complete release prefix and its referenced nested/code artifacts.
- Before execution, record the active stack/template references and configuration
  secret version ID without exporting secret values. Retain the current active
  SSO pool `us-east-1_eTo8Eu7eB`, legacy HMAC secret and all meeting/profile data.
  Source comparison shows existing stateful resource properties unchanged;
  deletion/retention-policy changes are not proof that the eventual change set
  contains no replacements. Inspect the packaged change set as described above.
- If CloudFormation update fails, let automatic rollback finish; do not start a
  competing update. Investigate `UPDATE_ROLLBACK_FAILED` before proceeding and
  never delete production to recover an update.
- If deployment succeeds but acceptance fails, restore the baseline packaged
  release through an explicitly reviewed CloudFormation UPDATE change set using
  the existing `LMA-CloudFormation` service role, baseline-supported parameters
  with `UsePreviousValue`, and the saved configuration version if needed. Verify
  referenced artifacts and the change set before executing. This is a coordinated
  UI/schema/edge/runtime rollback, not a live command performed by this document.
- Expect CloudFront propagation; require a browser refresh after rollback so
  cached new UI code does not query fields absent from the restored schema.
  Do not rotate/delete signing keys, overwrite named profiles or erase meeting
  data as part of rollback. A rollback plan is documented, not destructively
  rehearsed in production. New additive resources may remain retained.
- Production deployment is manually dispatched; merging does not deploy. The
  workflow only accepts production from `allops-main`, so do not dispatch it on
  an old feature branch expecting rollback. Use the reviewed baseline-template
  change set above, or a separately reviewed revert on `allops-main`.

## Historical isolated-dev validation procedure

The following describes the validation lane used during integration, not a
requirement to recreate the deleted isolated stack or repeat live tests.

1. Run branch CI checks and builds.
2. Deploy the integration revision through the dev CI lane. Production remains
   restricted to `allops-main`; dev accepts maintained feature branches allowed
   explicitly by the GitHub dev environment. `keep_dev=true` leaves the run-owned
   `LMA-CI-<last-six-run-id-digits>` stack up for acceptance, with data retention enabled. It never
   targets the existing `LMA-Dev` stack. Otherwise automatic cleanup remains on.
   The full run ID is recorded in ownership tags; a short-name collision fails
   closed rather than updating another run's stack.
   For a follow-up validation update, explicitly supply `dev_owner_run_id` with
   the original full run ID and keep `keep_dev=true`. CI verifies full ownership
   tags and healthy state, preserves the original owner tag, and never performs
   automatic cleanup for this operation. This cannot target original `LMA-Dev`
   or override a production target. An ordinary dev dispatch remains create-only.
3. Verify Google SSO and the real Quick connector user identity.
4. Verify Google Meet schedule, duplicate prevention, reschedule and explicit
   Calendar cancellation against the same VP.
5. Verify a real meeting produces transcript segments, a playable recording,
   and an on-demand summary; verify authorized live-desktop viewing and that
   another ordinary user cannot control an unshared VP.
6. Follow the release order above for production change-set review, deployment
   and the owner-approved production acceptance tests.

Mocked unit tests and local builds do not establish these live outcomes. Do not
delete the existing dev environment until production migration/acceptance and
the on-demand dev lifecycle gates above pass.

## Verified isolated OAuth acceptance — 2026-10-13

CI run `38385003766` deployed runtime revision `a85257ea` to `LMA-CI-254333`
(dev account `135755363077`), preserving owning run `38377254333`.
CloudFormation reached `UPDATE_COMPLETE`; CI and both branch validation runs passed.

The live HTTP AgentCore gateway probe used a real Cognito MCP-client access token
from a temporary user explicitly verified not to belong to the Admin group:

- Schedule, repeat schedule without duplication, update, cancel and repeat cancel passed.
- The same VP `7d0c0e80-b25f-4b9d-994f-d9bf09e87421` was tracked throughout.
- Consistent DynamoDB reads verified the actual user owner, updated time, preserved
  Bosnian language and final `CANCELLED` state. AppSync notifications succeeded.
- Requests with missing or invalid tokens returned HTTP 401.
- The temporary Cognito user was deleted; no invitation email was sent and no live
  join was submitted. Cancelled test records/identity claims remain for idempotency.

This establishes the deployed OAuth gateway compatibility fix, not a full Quick
UI Flow run, Google SSO redirect test or real meeting/audio acceptance. Production,
original dev and the working Quick connector were not modified by that test.

## Verified real Quick Flow acceptance — 2026-10-13

The private `LMA OAuth Acceptance Test` Flow
`66b995dc-284b-480d-bac6-1a0c71ae8a52` subsequently completed schedule, duplicate
schedule, update, cancellation and repeat cancellation using the isolated OAuth
connector and a non-admin test login. The same VP
`594e34fd-b180-4b7f-9e4f-6b2f59dfbf69` was verified in DynamoDB with the caller's
identity, preserved Bosnian language, updated time and final `CANCELLED` state.
No live meeting join was requested. The isolated stack was then deleted at the
owner's request. This is Quick OAuth lifecycle evidence, not evidence that the
production Calendar connector has been migrated; it still pointed at original
dev on 2026-10-19.

## Latest release fixes — 2026-10-19

- All three AWS credentials action pins use immutable Node-24-based v6.3.0.
- UI and VP launches inherit the deployment recording default; video remains an
  explicit per-VP opt-in, not a hard prohibition. Existing VP rows are not rewritten.
- Named summary profiles persist usable prompts before being listed. The editor
  shows only named profiles; older meetings keep their backend Default/Custom fallback.
- Display-only owner emails do not replace stored authorization identities.
  VNC checks recognize the same signed caller's username/email/sub aliases but
  still reject another ordinary user's unshared VP.
- Kimi has a 16,384-token per-section output budget; other providers keep 4,096.
  A short synthetic production-account request accepted the larger Kimi budget
  and ended normally. Actual long-meeting completion remains an acceptance test.
  `max_tokens` / `model_context_window_exceeded` results are rejected before
  export/publication, without another fallback-model call. Failed runs retain the
  previous summary and expose `FAILED_OUTPUT_LIMIT` with actionable UI guidance.
