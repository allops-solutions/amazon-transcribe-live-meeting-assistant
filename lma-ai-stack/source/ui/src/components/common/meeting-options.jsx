/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import { FormField, RadioGroup, Select } from '@cloudscape-design/components';

const client = generateClient();
const logger = new ConsoleLogger('meeting-options');

const getLLMPromptTemplate = `
  query GetLLMPromptTemplate($LLMPromptTemplateId: ID!) {
    getLLMPromptTemplate(LLMPromptTemplateId: $LLMPromptTemplateId) {
      LLMPromptTemplateId
    }
  }
`;

export const DEFAULT_TRANSCRIBE_LANGUAGE_MODE = 'identify-multiple-languages';

export const TRANSCRIBE_LANGUAGE_MODE_ITEMS = [
  {
    value: 'en-US',
    label: 'English only',
    description: 'Best accuracy for English-speaking calls. Bosnian/Croatian speech will be transcribed incorrectly.',
  },
  {
    value: 'bs-BA',
    label: 'Bosnian only',
    description: 'Best accuracy for Bosnian-heavy calls, including occasional English terms.',
  },
  {
    value: 'hr-HR',
    label: 'Croatian only',
    description: 'Best accuracy for Croatian-heavy calls, including occasional English terms.',
  },
  {
    value: 'identify-language',
    label: 'Auto-detect (locks in early)',
    description:
      'Identifies the language once, a few seconds into the conversation, and sticks with it. ' +
      "Best when you don't know which language a call will be in, but expect it to be consistent.",
  },
  {
    value: DEFAULT_TRANSCRIBE_LANGUAGE_MODE,
    label: 'Auto-detect, mixed languages (default)',
    description:
      'Re-checks language throughout the call. May produce poor quality when a speaker switches ' +
      'languages mid-sentence — use English only / Bosnian only / Croatian only above for meetings like that.',
  },
];

export const SUMMARY_LANGUAGE_OPTIONS = [
  { value: 'English', label: 'English' },
  { value: 'Bosnian', label: 'Bosnian' },
];

// Missing/empty catalog just means no profiles exist yet — not an error,
// since most stacks won't have created any.
export const useSummaryProfileCatalog = () => {
  const [catalog, setCatalog] = useState([]);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = await client.graphql({
          query: getLLMPromptTemplate,
          variables: { LLMPromptTemplateId: 'SummaryProfileCatalog' },
        });
        const catalogItem = JSON.parse(result.data.getLLMPromptTemplate.LLMPromptTemplateId) || {};
        const raw = catalogItem['0#PROFILES'];
        if (!cancelled) setCatalog(raw ? JSON.parse(raw) : []);
      } catch (err) {
        logger.debug('No summary profile catalog found (none created yet):', err);
        if (!cancelled) setCatalog([]);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);
  return catalog;
};

export const TranscribeLanguageModeField = ({ value, onChange, disabled = false }) => (
  <FormField label="Meeting language" stretch>
    <RadioGroup
      value={value}
      onChange={({ detail }) => onChange(detail.value)}
      items={TRANSCRIBE_LANGUAGE_MODE_ITEMS}
      disabled={disabled}
    />
  </FormField>
);
TranscribeLanguageModeField.propTypes = {
  value: PropTypes.string.isRequired,
  onChange: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
};

export const SummaryOptionsFields = ({
  summaryProfile,
  onSummaryProfileChange,
  summaryLanguage,
  onSummaryLanguageChange,
  summaryProfileCatalog,
  disabled = false,
}) => (
  <>
    <FormField
      label="Summary profile (optional)"
      description={
        "Which set of summary sections to use. Leave blank for the stack's Default/Custom templates. " +
        'Manage profiles under Configuration → Transcript Summary.'
      }
      stretch
    >
      <Select
        selectedOption={
          summaryProfile
            ? {
                value: summaryProfile,
                label: summaryProfileCatalog.find((p) => p.id === summaryProfile)?.name || summaryProfile,
              }
            : null
        }
        onChange={({ detail }) => onSummaryProfileChange(detail.selectedOption.value)}
        options={summaryProfileCatalog.map((p) => ({ value: p.id, label: p.name }))}
        placeholder="Default / Custom (stack-wide)"
        empty="No summary profiles created yet"
        disabled={disabled}
      />
    </FormField>

    <FormField
      label="Summary language (optional)"
      description={
        "Output language for this meeting's summary, independent of the profile above. " +
        'Leave blank to use whatever language the chosen templates are written in.'
      }
      stretch
    >
      <Select
        selectedOption={summaryLanguage ? { value: summaryLanguage, label: summaryLanguage } : null}
        onChange={({ detail }) => onSummaryLanguageChange(detail.selectedOption.value)}
        options={SUMMARY_LANGUAGE_OPTIONS}
        placeholder="Whatever language the templates are written in"
        disabled={disabled}
      />
    </FormField>
  </>
);
SummaryOptionsFields.propTypes = {
  summaryProfile: PropTypes.string.isRequired,
  onSummaryProfileChange: PropTypes.func.isRequired,
  summaryLanguage: PropTypes.string.isRequired,
  onSummaryLanguageChange: PropTypes.func.isRequired,
  summaryProfileCatalog: PropTypes.arrayOf(PropTypes.shape({ id: PropTypes.string, name: PropTypes.string }))
    .isRequired,
  disabled: PropTypes.bool,
};
