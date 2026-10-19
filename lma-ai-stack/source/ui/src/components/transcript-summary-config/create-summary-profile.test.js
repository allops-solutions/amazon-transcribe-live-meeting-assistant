/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { expect, it, vi } from 'vitest';
import createSummaryProfile from './create-summary-profile';

it('saves usable sections before publishing the selectable profile', async () => {
  const order = [];
  const saveProfile = vi.fn(async () => order.push('profile'));
  const saveCatalog = vi.fn(async () => order.push('catalog'));
  await createSummaryProfile({
    id: 'general',
    name: 'General',
    catalog: [],
    defaults: {
      LLMPromptTemplateId: 'DefaultSummaryPromptTemplates',
      '1#Summary': 'Summarize {transcript}',
      '2#Off': 'NONE',
    },
    saveProfile,
    saveCatalog,
  });
  expect(order).toEqual(['profile', 'catalog']);
  expect(saveProfile).toHaveBeenCalledWith('general', { '1#Summary': 'Summarize {transcript}' });
  expect(saveCatalog).toHaveBeenCalledWith([{ id: 'general', name: 'General' }]);
});

it('does not publish an empty profile or a failed profile write', async () => {
  const saveCatalog = vi.fn();
  const saveProfile = vi.fn().mockRejectedValue(new Error('write failed'));
  const args = { id: 'general', name: 'General', catalog: [], saveCatalog, saveProfile };
  await expect(createSummaryProfile({ ...args, defaults: {} })).rejects.toThrow('empty');
  expect(saveProfile).not.toHaveBeenCalled();
  await expect(createSummaryProfile({ ...args, defaults: { '1#Summary': 'Summarize' } })).rejects.toThrow(
    'write failed',
  );
  expect(saveCatalog).not.toHaveBeenCalled();
});
