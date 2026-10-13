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
- Read responses no longer expose MCP stored authentication configuration.
  Custom consumers must use `HasAuthConfig`, not the removed `AuthConfig` field.

## Required acceptance before production

1. Run branch CI checks and builds.
2. Deploy the integration revision through the dev CI lane. Production remains
   restricted to `allops-main`; dev accepts maintained feature branches allowed
   explicitly by the GitHub dev environment. `keep_dev=true` leaves the run-owned
   `LMA-CI-<run-id>` stack up for acceptance, with data retention enabled. It never
   targets the existing `LMA-Dev` stack. Otherwise automatic cleanup remains on.
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
