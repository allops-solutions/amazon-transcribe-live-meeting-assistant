/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Mirrors the web UI's components/common/meeting-options.jsx. Keep the values
// in sync: the websocket transcriber validates transcribeLanguageMode.

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
