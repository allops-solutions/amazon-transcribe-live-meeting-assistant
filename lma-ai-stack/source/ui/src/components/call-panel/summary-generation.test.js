import { expect, it } from 'vitest';
import summaryGenerationCompleted from './summary-generation';

const baseline = { text: 'old summary', requestedAt: 'old-request' };

it('recognizes a changed summary as complete', () => {
  expect(summaryGenerationCompleted({ callSummaryText: 'new summary' }, baseline)).toBe(true);
});

it('recognizes identical text from a completed new request', () => {
  expect(
    summaryGenerationCompleted(
      { callSummaryText: 'old summary', summaryStatus: 'DONE', summaryRequestedAt: 'new-request' },
      baseline,
    ),
  ).toBe(true);
});

it('does not confuse an old completion or an active request with a new completion', () => {
  [
    { summaryStatus: 'DONE', summaryRequestedAt: 'old-request' },
    { summaryStatus: 'IN_PROGRESS', summaryRequestedAt: 'new-request' },
    { summaryStatus: 'DONE' },
  ].forEach((item) => {
    expect(summaryGenerationCompleted({ callSummaryText: 'old summary', ...item }, baseline)).toBe(false);
  });
});
