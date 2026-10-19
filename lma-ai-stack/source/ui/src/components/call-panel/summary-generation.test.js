import { expect, it } from 'vitest';
import summaryGenerationCompleted, { summaryGenerationFailed, summaryFailureMessage } from './summary-generation';

const baseline = { text: 'old summary', requestedAt: 'old-request' };

it('recognizes output-limit failures and gives actionable guidance without claiming completion', () => {
  expect(summaryGenerationFailed('FAILED_OUTPUT_LIMIT')).toBe(true);
  expect(summaryGenerationFailed('FAILED')).toBe(true);
  expect(summaryGenerationFailed('IN_PROGRESS')).toBe(false);
  expect(summaryGenerationFailed('DONE')).toBe(false);
  expect(summaryGenerationFailed(undefined)).toBe(false);
  expect(summaryFailureMessage('FAILED_OUTPUT_LIMIT')).toContain('more concise profile');
  expect(summaryFailureMessage('FAILED_OUTPUT_LIMIT')).toContain('previous summary was kept');
  expect(
    summaryGenerationCompleted({ callSummaryText: baseline.text, summaryStatus: 'FAILED_OUTPUT_LIMIT' }, baseline),
  ).toBe(false);
});

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
