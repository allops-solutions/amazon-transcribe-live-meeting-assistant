import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SummaryOptionsFields } from './meeting-options';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
afterEach(cleanup);

it('starts with no selected profile and offers no summary-language selector', () => {
  render(
    <SummaryOptionsFields
      summaryProfile=""
      onSummaryProfileChange={vi.fn()}
      summaryProfileCatalog={[{ id: 'bosnian', name: 'Bosnian profile' }]}
    />,
  );
  expect(screen.getByText('Choose a summary profile')).toBeInTheDocument();
  expect(screen.getByText('Summary profile')).toBeInTheDocument();
  expect(screen.queryByText(/Summary language/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Summary profile \(optional\)/)).not.toBeInTheDocument();
});
