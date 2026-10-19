/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TranscriptSummaryPage from './TranscriptSummaryPage';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql }) }));

let profiles;
beforeEach(() => {
  profiles = [
    { id: 'summary-en', name: 'Summary (EN)' },
    { id: 'summary-bs', name: 'Summary (BS)' },
  ];
  graphql.mockReset();
  graphql.mockImplementation(async ({ variables }) => {
    if (variables.input) return { data: { updateLLMPromptTemplate: { Success: true } } };
    const id = variables.LLMPromptTemplateId;
    const config =
      id === 'SummaryProfileCatalog'
        ? { '0#PROFILES': JSON.stringify(profiles) }
        : { '1#Summary': `Prompt for ${id}: {transcript}` };
    return { data: { getLLMPromptTemplate: { LLMPromptTemplateId: JSON.stringify(config) } } };
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const expectNoFallbackAccess = () => {
  expect(
    graphql.mock.calls.some(([args]) => args.variables.LLMPromptTemplateId === 'CustomSummaryPromptTemplates'),
  ).toBe(false);
  expect(
    graphql.mock.calls.some(([args]) => args.variables.input?.LLMPromptTemplateId === 'CustomSummaryPromptTemplates'),
  ).toBe(false);
};

it('opens the first named profile and offers only named profiles', async () => {
  const { container } = render(<TranscriptSummaryPage />);
  await screen.findByText('Editing: Summary (EN)');
  await screen.findByDisplayValue('Prompt for Profile#summary-en: {transcript}');
  const select = createWrapper(container).findSelect();
  act(() => select.openDropdown());
  expect(screen.queryByText(/Default \/ Custom/)).not.toBeInTheDocument();
  act(() => select.selectOptionByValue('summary-bs'));
  await screen.findByDisplayValue('Prompt for Profile#summary-bs: {transcript}');
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
  await waitFor(() =>
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: {
          input: {
            LLMPromptTemplateId: 'Profile#summary-bs',
            TemplateConfig: JSON.stringify({ '1#Summary': 'Prompt for Profile#summary-bs: {transcript}' }),
          },
        },
      }),
    ),
  );
  expectNoFallbackAccess();
});

it('selects another named profile after deletion without opening the fallback', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  render(<TranscriptSummaryPage />);
  await screen.findByDisplayValue('Prompt for Profile#summary-en: {transcript}');
  fireEvent.click(screen.getByRole('button', { name: 'Delete profile' }));
  await screen.findByDisplayValue('Prompt for Profile#summary-bs: {transcript}');
  expectNoFallbackAccess();
});

it('requires creating a named profile when the catalog is empty', async () => {
  profiles = [];
  render(<TranscriptSummaryPage />);
  await screen.findByText('Create a profile to configure summary sections');
  expect(screen.getByRole('button', { name: 'Save Changes' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Reset to Defaults' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Add Template' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'New profile' })).toBeEnabled();
  expectNoFallbackAccess();
});
