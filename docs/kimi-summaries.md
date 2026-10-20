---
title: "Kimi K3 summary generation and failure handling"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. Licensed under the MIT License.

# Kimi K3 for summaries only

The dev and production summary-only override is `SummaryBedrockModelId=global.moonshotai.kimi-k3`.
The existing `BedrockModelId` also feeds historical-meeting/knowledge-base searches,
so it stays on Sonnet. `MeetingAssistServiceBedrockModelID` stays on Sonnet too.
No knowledge-base resources or assistant tools are removed by this change.

The template override defaults to empty; empty retains the original
`BedrockModelId` summary selection. Both committed environment parameter files
explicitly select Kimi. On a console update, set **SummaryBedrockModelId**
to **global.moonshotai.kimi-k3** and preserve the other model parameters. Publishing
alone does not change deployed parameter values. Local CLI update parameter files
must also include this new value if used; they are not modified automatically.

The summary Lambda uses text-only, single-turn **InvokeModel** requests for Kimi K3,
with an explicit `reasoning_effort: "medium"` in the native JSON request body.
`KIMI_SUMMARY_EFFORT` defaults to `medium` and is explicitly set in the stack;
it is independent of other providers' `SUMMARY_EFFORT`. Invalid effort settings
fail before inference rather than silently using the provider's maximum default.
AWS documents that omitted Kimi effort defaults to `max`, and recommends using
Responses, Chat Completions or InvokeModel to set effort rather than relying on
Converse validation. Native requests use the existing boto3 client, IAM role and
`bedrock:InvokeModel` permission: no new credentials, dependencies or permissions.
Other providers, including the Haiku fallback, continue using Converse.
Kimi retains its 16,384-token per-section output budget; other providers retain
4,096. Medium effort is not a guarantee of completion or a separate hard
reasoning-token cap. On-demand generation and the
configured Haiku fallback remain enabled. A primary
failure may still produce a summary through the fallback; inspect logs to establish
which model actually generated it.

## Bounded generation and retries

The primary model gets up to **300 seconds** to return, followed by at most one
fallback generation with up to **120 seconds**. All sections share a **540-second
inference budget**, starting before transcript retrieval. Remaining Lambda time
can shorten it further; finalization reserves 30 seconds, and each network request
reserves 10 seconds for connection/response overhead. Later sections may therefore
receive a shorter timeout or fail without starting a request. The worker's hard
Lambda timeout remains 600 seconds. These are client-side budgets, not a guarantee
that Bedrock stops work after a client disconnect.

Both Bedrock and the orchestrator's synchronous Lambda client use
`total_max_attempts=1`. Read timeouts, connection errors and ambiguous server
errors are **not resubmitted to the same model**. One retry is permitted only for
an explicit `ThrottlingException` or `TooManyRequestsException` rejection, within
the same model deadline. Fallback is bounded the same way. A timed-out primary
may still complete server-side and be billed even when fallback is used; never
assume timeout means cancellation or zero usage.

Output/context-limit termination now also tries the configured distinct fallback
**once per failed section**, if time remains. A partial primary response is never
used or exported. The fallback gets the same complete prompt and its own existing
4,096-token limit; it does not continue or repair the partial primary text.
If fallback fails or reaches its own limit, there is no further model switch or
generation retry (apart from the explicit throttling rejection rule above).
Other failures use the same bounded fallback path. If neither model succeeds,
the orchestrator marks only the matching request FAILED and keeps the previous
summary. It does not publish partial sections or automatically restart generation.

Async orchestration retries remain **zero**. Queued requests expire after 60
seconds rather than starting hours after the UI claim has expired. A queue
expiry or hard timeout/crash goes to
`SummaryFailureRecovery`, a separate status-only Lambda with no inference or
worker-invoke permission. Its conditional update checks the original request
timestamp and IN_PROGRESS status, so delayed/duplicate failure events cannot
alter a newer request, a completed summary, or its text. Recovery can retry its
status write twice; those retries never generate tokens. If DynamoDB or Lambda's
destination delivery is unavailable, recovery is not guaranteed: inspect failure
and destination-delivery metrics/logs. The existing 11-minute stale-claim rule
still permits a deliberate retry after an abandoned run, without auto-generating.

Deployment is required to activate code/configuration changes. Offline regression
tests simulate timeouts and failures without live model requests or token costs.

Usage logs contain only whitelisted model/API, stop reason, input/output/total,
reasoning and cache token counts when provided, plus visible-text character counts.
Missing provider counters stay `null`, never zero or inferred from text length.
Non-reasoning output tokens are derived only when the provider supplies valid total
output and reasoning counts. Prompt, transcript, generated summary and reasoning
text are not printed by the summary worker. Implicit caching can make uncached
input small: inspect cache counters too, not input tokens alone. These metrics are
diagnostics, not an authoritative AWS billing report.

AWS documents Converse support and global cross-region availability from us-east-1:
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html

Offline tests verify model isolation, request format, text extraction and fallback.
Live account access, latency and summary quality require validation after deployment.
With ModelValidation enabled, the existing deployment validator checks the selected
summary model with a small inference request. No live inference or AWS mutation is
performed by the local test suite.
