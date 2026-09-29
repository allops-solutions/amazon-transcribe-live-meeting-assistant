import React from 'react';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SummaryGenerateButton from './SummaryGenerateButton';

afterEach(cleanup);

it('generates a first summary without confirmation', () => {
  const generate = vi.fn();
  render(<SummaryGenerateButton hasSummary={false} disabled={false} loading={false} onGenerate={generate} />);
  fireEvent.click(screen.getByRole('button', { name: 'Generate summary' }));
  expect(generate).toHaveBeenCalledTimes(1);
});

it('does not generate until replacement is explicitly confirmed', () => {
  const generate = vi.fn();
  render(<SummaryGenerateButton hasSummary disabled={false} loading={false} onGenerate={generate} />);
  fireEvent.click(screen.getByRole('button', { name: 'Regenerate summary' }));
  expect(generate).not.toHaveBeenCalled();
  expect(screen.getByText(/cannot be restored through LMA/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Regenerate', exact: true }));
  expect(generate).toHaveBeenCalledTimes(1);
});

it('cancel keeps the current summary untouched', () => {
  const generate = vi.fn();
  render(<SummaryGenerateButton hasSummary disabled={false} loading={false} onGenerate={generate} />);
  fireEvent.click(screen.getByRole('button', { name: 'Regenerate summary' }));
  fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }));
  expect(generate).not.toHaveBeenCalled();
});

it('cannot start a run without a valid profile or during another run', () => {
  const generate = vi.fn();
  render(<SummaryGenerateButton hasSummary={false} disabled loading={false} onGenerate={generate} />);
  expect(screen.getByRole('button', { name: 'Generate summary' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Generate summary' }));
  expect(generate).not.toHaveBeenCalled();
});
