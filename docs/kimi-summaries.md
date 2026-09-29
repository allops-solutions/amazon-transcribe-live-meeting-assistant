# Kimi K3 for summaries only

Dev's summary-only override is `SummaryBedrockModelId=global.moonshotai.kimi-k3`.
The existing `BedrockModelId` also feeds historical-meeting/knowledge-base searches,
so it stays on Sonnet. `MeetingAssistServiceBedrockModelID` stays on Sonnet too.
No knowledge-base resources or assistant tools are removed by this change.

The override defaults to empty for existing deployments and production; empty
retains the original `BedrockModelId` summary selection. Dev's committed parameter
file explicitly selects Kimi. On a console update, set **SummaryBedrockModelId**
to **global.moonshotai.kimi-k3** and preserve the other model parameters. Publishing
alone does not change deployed parameter values. Local CLI update parameter files
must also include this new value if used; they are not modified automatically.

The summary Lambda uses text-only, single-turn Converse requests without passing
Claude/OpenAI-specific reasoning settings. Existing token limit, timeouts, retries,
on-demand generation and configured Haiku fallback remain unchanged. A primary
failure may still produce a summary through the fallback; inspect logs to establish
which model actually generated it.

AWS documents Converse support and global cross-region availability from us-east-1:
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html

Offline tests verify model isolation, request format, text extraction and fallback.
Live account access, latency and summary quality require validation after deployment.
With ModelValidation enabled, the existing deployment validator checks the selected
summary model with a small inference request. No live inference or AWS mutation is
performed by the local test suite.
