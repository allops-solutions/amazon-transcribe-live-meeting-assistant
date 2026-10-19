// A deterministic model can successfully regenerate exactly the same text.
// A DONE record from a newer request still counts as completion in that case.
const summaryGenerationCompleted = (item, baseline) =>
  (item.callSummaryText ?? '') !== baseline.text ||
  (item.summaryStatus === 'DONE' &&
    Boolean(item.summaryRequestedAt) &&
    item.summaryRequestedAt !== baseline.requestedAt);

export default summaryGenerationCompleted;

export const summaryGenerationFailed = (status) => status === 'FAILED' || status === 'FAILED_OUTPUT_LIMIT';

export const summaryFailureMessage = (status) =>
  status === 'FAILED_OUTPUT_LIMIT'
    ? 'The summary exceeded the model output/context limit. The previous summary was kept. ' +
      'Use a more concise profile or ask an administrator to review the output budget.'
    : 'Summary generation failed. The previous summary was kept. ' +
      'Choose a profile and try again after the issue is resolved.';
