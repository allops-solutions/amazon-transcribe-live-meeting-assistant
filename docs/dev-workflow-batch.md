# Dev workflow batch — 2026-09-29

Implemented locally; not committed, published or deployed by the agent. This batch
does not change existing storage/Cognito resource definitions or retention settings.
Review the eventual nested change set before executing the update.

## Changes

- Generate starts with no selected profile. Choose an existing, non-empty named
  profile on the meeting page; the backend also enforces this. A deleted profile
  cannot silently fall back to a different prompt. Profiles are managed under
  Configuration → Transcript Summary. If only legacy Default/Custom templates
  exist, create and save a named profile before using this workflow.
- Regenerate asks for confirmation before overwriting an existing summary. Cancel
  spends no tokens. Old text stays until a successful replacement; model failures
  and blank output do not replace it with an error message. Single-section profiles
  may return plain text/Markdown or JSON. Existing timeouts/fallback/retry budgets
  stay unchanged, including the 660-second stale in-progress guard.
  A completed run with identical output also clears the UI's generation state.
- The summary-language selector is removed. The selected prompt defines language;
  old saved language overrides are cleared on generation and ignored by the summary
  Lambda. Compatibility fields remain, so this is not a destructive schema migration.
- Kimi K3 is selected for dev summaries only. See [Kimi summaries](kimi-summaries.md).
  Assistant and knowledge-base searches remain on their original models.
- Only Admin users can delete meetings. UI controls, bulk-delete Lambda, GraphQL
  group authorization and direct-delete resolvers all enforce this. The internal
  cleanup role retains deletion permission; other IAM identities are denied by the
  direct-delete resolvers. Sharing remains unchanged.
- Meetings show Meeting ID → Status → Owner Name → Summary → Duration → Initiation
  Timestamp, with the date displayed as DD.MM.YYYY. Owner Email and Shared With are
  hidden columns, not deleted data. A new local preference key resets stale layouts.
- Regular VPs use allOps LMA, including migration of the previous default name.
  Explicit custom names and signed-in account naming are preserved. No automatic
  join/start chat announcements are sent, even with legacy message parameters.
  Explicit pause/leave handling remains.
- Both MCP tool-description copies explicitly advertise Google Meet support.
  Read-only inspection showed the live gateway already included Google Meet in its
  platform metadata, so Quick's false claim cannot be attributed simply to a missing
  platform entry. Cached context or an incorrect model answer remain possibilities.
- The account menu shows the signed-in email and role, not googleworkspace_….

## One deployment and acceptance checks

1. Stage new files before publishing: the build bundles tracked/staged files only.
   Publish from the repository root, then update the existing LMA-Dev stack.
2. On the console parameter page set SummaryBedrockModelId to
   global.moonshotai.kimi-k3. Preserve other model, auth, storage and retention values.
   Publish alone does not select the new model parameter.
3. Verify no profile is preselected, first Generate requires a choice, Regenerate
   opens the warning, and Cancel leaves the old summary unchanged. Test a one-section
   profile and a language-specific prompt. Check logs for the actual model used;
   live Kimi access/quality/latency were not tested by the agent.
4. Check the six table columns/date and the email/role account label.
5. Test Admin deletion with a disposable meeting; confirm a regular user has no
   button and cannot call deletion directly. Do not use important transcripts.
6. Join a test meeting: regular bot name allOps LMA, no intro/start chat messages.
7. Refresh/reconnect the Quick MCP integration and start a new chat if needed.
   Ask it to schedule a Google Meet meeting, verify the tool/platform selected and
   host admission. The agent has not live-tested Quick's response after deployment.

59 backend (including Workspace-domain enforcement), 63 UI and 144 VP tests pass,
along with local production builds;
AppSync's read-only template evaluator also checked both direct-delete resolvers
for Admin, regular user, trusted cleanup IAM and other IAM identities. No meeting
was deleted by those checks. CloudFormation lint has no errors; existing warnings
remain. End-to-end browser/meeting validation is still required after deployment.
