// A deterministic model can successfully regenerate exactly the same text.
// A DONE record from a newer request still counts as completion in that case.
const summaryGenerationCompleted = (item, baseline) =>
  (item.callSummaryText ?? '') !== baseline.text ||
  (item.summaryStatus === 'DONE' &&
    Boolean(item.summaryRequestedAt) &&
    item.summaryRequestedAt !== baseline.requestedAt);

export default summaryGenerationCompleted;
