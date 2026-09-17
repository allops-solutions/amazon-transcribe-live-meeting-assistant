/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Mirrors the web UI's components/common/meeting-options.jsx. Keep the values
// in sync: the websocket transcriber validates transcribeLanguageMode and the
// summary Lambda looks profiles up by id.

export const DEFAULT_TRANSCRIBE_LANGUAGE_MODE = 'identify-multiple-languages';

export const TRANSCRIBE_LANGUAGE_MODE_OPTIONS = [
  { value: 'en-US', label: 'English only', description: 'Best accuracy for English-speaking calls.' },
  { value: 'bs-BA', label: 'Bosnian only', description: 'Best accuracy for Bosnian-heavy calls.' },
  { value: 'hr-HR', label: 'Croatian only', description: 'Best accuracy for Croatian-heavy calls.' },
  {
    value: 'identify-language',
    label: 'Auto-detect (locks in early)',
    description: 'Identifies the language once, a few seconds in, and sticks with it.',
  },
  {
    value: DEFAULT_TRANSCRIBE_LANGUAGE_MODE,
    label: 'Auto-detect, mixed languages (default)',
    description: 'Re-checks language throughout the call.',
  },
];

export const SUMMARY_LANGUAGE_OPTIONS = [
  { value: 'English', label: 'English' },
  { value: 'Bosnian', label: 'Bosnian' },
];

export type SummaryProfile = { id: string; name: string };

const getLLMPromptTemplate = `
  query GetLLMPromptTemplate($LLMPromptTemplateId: ID!) {
    getLLMPromptTemplate(LLMPromptTemplateId: $LLMPromptTemplateId) {
      LLMPromptTemplateId
    }
  }
`;

// Reads the SummaryProfileCatalog row from AppSync with the user's Cognito
// token. A missing catalog (no profiles created yet) or any failure yields an
// empty list — the picker is optional, so it must never block starting a call.
export async function fetchSummaryProfileCatalog(
  graphqlEndpoint: string | undefined,
  idToken: string | undefined
): Promise<SummaryProfile[]> {
  if (!graphqlEndpoint || !idToken) {
    return [];
  }
  try {
    const response = await fetch(graphqlEndpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: idToken },
      body: JSON.stringify({
        query: getLLMPromptTemplate,
        variables: { LLMPromptTemplateId: 'SummaryProfileCatalog' },
      }),
    });
    if (!response.ok) {
      return [];
    }
    const body = await response.json();
    const catalogItem = JSON.parse(body?.data?.getLLMPromptTemplate?.LLMPromptTemplateId || '{}') || {};
    const raw = catalogItem['0#PROFILES'];
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((p) => p && typeof p.id === 'string' && typeof p.name === 'string')
      : [];
  } catch (err) {
    console.log('No summary profile catalog available:', err);
    return [];
  }
}
