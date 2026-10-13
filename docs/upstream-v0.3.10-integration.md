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

## Required acceptance before production

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
6. Inspect the production change set for unexpected stateful replacements, then
   deploy the accepted revision through production CI.

Mocked unit tests and local builds do not establish these live outcomes. Do not
delete the existing dev environment while these gates remain incomplete.

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
original dev and the working Quick connector were not modified. PR 21 remains draft.
