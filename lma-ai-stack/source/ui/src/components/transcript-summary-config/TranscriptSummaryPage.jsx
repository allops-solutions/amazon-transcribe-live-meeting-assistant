/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  FormField,
  Input,
  Select,
  Textarea,
  Button,
  Alert,
  Spinner,
  Box,
  ExpandableSection,
  Icon,
  Modal,
} from '@cloudscape-design/components';

const client = generateClient();
const getLLMPromptTemplateQuery = `
  query GetLLMPromptTemplate($LLMPromptTemplateId: ID!) {
    getLLMPromptTemplate(LLMPromptTemplateId: $LLMPromptTemplateId) {
      LLMPromptTemplateId
    }
  }
`;

const updateLLMPromptTemplateMutation = `
  mutation UpdateLLMPromptTemplate($input: UpdateLLMPromptTemplateInput!) {
    updateLLMPromptTemplate(input: $input) {
      LLMPromptTemplateId
      Success
    }
  }
`;

// The catalog of named summary profiles is itself stored as one LLMPromptTemplate
// item (id SummaryProfileCatalog), under a field matching the same N#LABEL shape
// every other template field uses, so it needs no special case in the write
// resolver's field allowlist. Its value is a JSON-encoded array of {id, name}.
const CATALOG_ID = 'SummaryProfileCatalog';
const CATALOG_FIELD = '0#PROFILES';
// Sentinel for "no profile selected" in the Select control — GraphQL/Cloudscape
// don't do well with an actual null/empty option value.
const DEFAULT_OPTION_VALUE = '__default__';

const slugify = (name) =>
  name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');

// Parse config object into sorted array of {number, label, prompt} entries
const parseTemplateConfig = (config) => {
  if (!config) return [];
  const entries = [];
  Object.keys(config).forEach((key) => {
    const match = key.match(/^(\d+)#(.+)$/);
    if (match) {
      entries.push({
        key,
        number: parseInt(match[1], 10),
        label: match[2],
        prompt: config[key],
      });
    }
  });
  return entries.sort((a, b) => a.number - b.number);
};

const TranscriptSummaryPage = () => {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [defaultConfig, setDefaultConfig] = useState({});
  const [templates, setTemplates] = useState([]);
  const [catalog, setCatalog] = useState([]); // [{ id, name }]
  const [selectedProfileId, setSelectedProfileId] = useState(null); // null = Default/Custom
  const [showNewProfileModal, setShowNewProfileModal] = useState(false);
  const [newProfileName, setNewProfileName] = useState('');
  const [newProfileError, setNewProfileError] = useState('');

  const fetchTemplateItem = async (templateId) => {
    const result = await client.graphql({
      query: getLLMPromptTemplateQuery,
      variables: { LLMPromptTemplateId: templateId },
    });
    return JSON.parse(result.data.getLLMPromptTemplate.LLMPromptTemplateId) || {};
  };

  const loadCatalog = useCallback(async () => {
    try {
      const catalogItem = await fetchTemplateItem(CATALOG_ID);
      const raw = catalogItem[CATALOG_FIELD];
      setCatalog(raw ? JSON.parse(raw) : []);
    } catch (err) {
      console.error('Error loading summary profile catalog:', err);
      setCatalog([]);
    }
  }, []);

  const saveCatalog = async (updatedCatalog) => {
    await client.graphql({
      query: updateLLMPromptTemplateMutation,
      variables: {
        input: {
          LLMPromptTemplateId: CATALOG_ID,
          TemplateConfig: JSON.stringify({ [CATALOG_FIELD]: JSON.stringify(updatedCatalog) }),
        },
      },
    });
    setCatalog(updatedCatalog);
  };

  // profileId === null loads the stack-wide Default+Custom pair (today's only
  // behavior); a string loads that named profile's own template set, seeded
  // from the Default templates when the profile has no saved content yet
  // (brand new, or created but never saved) — the same helpful starting point
  // Default already gives the Custom editor.
  const loadConfig = useCallback(async (profileId) => {
    setLoading(true);
    setError(null);
    try {
      const defaultData = await fetchTemplateItem('DefaultSummaryPromptTemplates');
      setDefaultConfig(defaultData);

      if (profileId) {
        const profileData = await fetchTemplateItem(`Profile#${profileId}`);
        const profileEntries = parseTemplateConfig(profileData);
        setTemplates(profileEntries.length > 0 ? profileEntries : parseTemplateConfig(defaultData));
      } else {
        const customData = await fetchTemplateItem('CustomSummaryPromptTemplates');
        const customEntries = parseTemplateConfig(customData);
        setTemplates(customEntries.length > 0 ? customEntries : parseTemplateConfig(defaultData));
      }
    } catch (err) {
      console.error('Error loading LLM prompt templates:', err);
      setError('Failed to load configuration. Please try again.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCatalog();
    loadConfig(null);
  }, [loadCatalog, loadConfig]);

  const handleProfileChange = (value) => {
    const profileId = value === DEFAULT_OPTION_VALUE ? null : value;
    setSelectedProfileId(profileId);
    setError(null);
    setSuccess(null);
    loadConfig(profileId);
  };

  const handleOpenNewProfileModal = () => {
    setNewProfileName('');
    setNewProfileError('');
    setShowNewProfileModal(true);
  };

  const handleCreateProfile = async () => {
    const name = newProfileName.trim();
    if (!name) {
      setNewProfileError('Enter a name for the profile.');
      return;
    }
    const id = slugify(name);
    if (!id) {
      setNewProfileError('That name has no usable characters — try including a letter or number.');
      return;
    }
    if (catalog.some((p) => p.id === id)) {
      setNewProfileError('A profile with this name already exists.');
      return;
    }
    setSaving(true);
    try {
      const updatedCatalog = [...catalog, { id, name }];
      await saveCatalog(updatedCatalog);
      setShowNewProfileModal(false);
      setSelectedProfileId(id);
      await loadConfig(id);
      setSuccess(`Profile "${name}" created. Edit its sections below, then Save Changes.`);
    } catch (err) {
      console.error('Error creating summary profile:', err);
      setNewProfileError('Failed to create profile. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteProfile = async () => {
    if (!selectedProfileId) return;
    const profile = catalog.find((p) => p.id === selectedProfileId);
    const profileId = selectedProfileId;
    // eslint-disable-next-line no-alert
    if (
      !window.confirm(
        `Permanently delete profile "${profile?.name || profileId}"? ` +
          'This cannot be undone. A meeting already using it falls back to the Default templates ' +
          'the next time its summary is generated.',
      )
    ) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // Unlist first, then hard-delete the profile's own item. If the delete
      // fails after unlisting, the orphaned item is harmless (unreachable
      // from the catalog) and can be cleaned up by trying again.
      await saveCatalog(catalog.filter((p) => p.id !== profileId));
      await client.graphql({
        query: updateLLMPromptTemplateMutation,
        variables: {
          input: {
            LLMPromptTemplateId: `Profile#${profileId}`,
            Delete: true,
          },
        },
      });
      setSelectedProfileId(null);
      await loadConfig(null);
      setSuccess('Profile deleted.');
    } catch (err) {
      console.error('Error deleting summary profile:', err);
      setError('Failed to delete profile. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleLabelChange = (index, newLabel) => {
    const updated = [...templates];
    updated[index] = { ...updated[index], label: newLabel };
    setTemplates(updated);
  };

  const handlePromptChange = (index, newPrompt) => {
    const updated = [...templates];
    updated[index] = { ...updated[index], prompt: newPrompt };
    setTemplates(updated);
  };

  const handleAddTemplate = () => {
    const maxNumber = templates.reduce((max, t) => Math.max(max, t.number), 0);
    setTemplates([
      ...templates,
      {
        key: `${maxNumber + 1}#NEW_TEMPLATE`,
        number: maxNumber + 1,
        label: 'NEW_TEMPLATE',
        prompt: 'Enter your prompt template here. Use {transcript} as placeholder for the meeting transcript.',
      },
    ]);
  };

  const handleDeleteTemplate = (index) => {
    const updated = templates.filter((_, i) => i !== index);
    setTemplates(updated);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const configData = {};
      templates.forEach((template, index) => {
        const key = `${index + 1}#${template.label}`;
        configData[key] = template.prompt;
      });

      await client.graphql({
        query: updateLLMPromptTemplateMutation,
        variables: {
          input: {
            LLMPromptTemplateId: selectedProfileId ? `Profile#${selectedProfileId}` : 'CustomSummaryPromptTemplates',
            TemplateConfig: JSON.stringify(configData),
          },
        },
      });

      setSuccess(
        selectedProfileId
          ? `Profile "${
              catalog.find((p) => p.id === selectedProfileId)?.name || selectedProfileId
            }" saved successfully.`
          : 'Summary prompt templates saved successfully.',
      );
      await loadConfig(selectedProfileId);
    } catch (err) {
      console.error('Error saving LLM prompt templates:', err);
      setError('Failed to save templates. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  const handleResetToDefaults = async () => {
    if (!selectedProfileId) {
      // Default/Custom: reset means clear the Custom override entirely.
      setSaving(true);
      setError(null);
      setSuccess(null);
      try {
        await client.graphql({
          query: updateLLMPromptTemplateMutation,
          variables: {
            input: {
              LLMPromptTemplateId: 'CustomSummaryPromptTemplates',
              TemplateConfig: JSON.stringify({}),
            },
          },
        });

        setTemplates(parseTemplateConfig(defaultConfig));
        setSuccess('Custom overrides cleared. Default templates will be used.');
        await loadConfig(null);
      } catch (err) {
        console.error('Error resetting templates:', err);
        setError('Failed to reset templates. Please try again.');
      } finally {
        setSaving(false);
      }
    } else {
      // A named profile: just reload the Default templates into the editor —
      // nothing is written until Save Changes.
      setTemplates(parseTemplateConfig(defaultConfig));
      setSuccess('Reloaded the Default templates into the editor. Save Changes to apply them to this profile.');
    }
  };

  const profileOptions = [
    { value: DEFAULT_OPTION_VALUE, label: 'Default / Custom (meetings without a profile)' },
    ...catalog.map((p) => ({ value: p.id, label: p.name })),
  ];

  if (loading && catalog.length === 0 && Object.keys(defaultConfig).length === 0) {
    return (
      <Container header={<Header variant="h1">Transcript Summary Prompts</Header>}>
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" /> Loading templates...
        </Box>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description={
              'Customize the summary prompt templates used for end-of-meeting transcript summarization. ' +
              'Create a named profile per meeting type (e.g. Technical Client Call, Startup Call, Team Weekly) — ' +
              'a meeting picks which profile (and which output language) to use when it is created. ' +
              'Set a prompt to "NONE" to disable a section.'
            }
          >
            Transcript Summary Prompts
          </Header>
        }
      >
        <SpaceBetween size="l">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}
          {success && (
            <Alert type="success" dismissible onDismiss={() => setSuccess(null)}>
              {success}
            </Alert>
          )}

          <FormField
            label="Profile"
            description="Which set of sections you're editing. Meetings choose a profile independently of language."
            stretch
          >
            <SpaceBetween direction="horizontal" size="xs">
              <div style={{ minWidth: '320px' }}>
                <Select
                  selectedOption={
                    profileOptions.find((o) => o.value === (selectedProfileId || DEFAULT_OPTION_VALUE)) || null
                  }
                  onChange={({ detail }) => handleProfileChange(detail.selectedOption.value)}
                  options={profileOptions}
                  disabled={saving}
                />
              </div>
              <Button onClick={handleOpenNewProfileModal} disabled={saving}>
                New profile
              </Button>
              {selectedProfileId && (
                <Button onClick={handleDeleteProfile} disabled={saving}>
                  Delete profile
                </Button>
              )}
            </SpaceBetween>
          </FormField>

          <Header
            variant="h3"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={handleResetToDefaults} loading={saving}>
                  Reset to Defaults
                </Button>
                <Button variant="primary" onClick={handleSave} loading={saving}>
                  Save Changes
                </Button>
              </SpaceBetween>
            }
          >
            {selectedProfileId
              ? `Editing: ${catalog.find((p) => p.id === selectedProfileId)?.name || selectedProfileId}`
              : 'Editing: Default / Custom'}
          </Header>

          {templates.map((template, index) => (
            <Container
              // Not the label: keying on it remounted the row on every keystroke,
              // dropping focus after each typed character.
              key={`template-${template.number}`}
              header={
                <Header
                  variant="h3"
                  actions={
                    <Button
                      iconName="remove"
                      variant="icon"
                      onClick={() => handleDeleteTemplate(index)}
                      ariaLabel={`Delete template ${index + 1}`}
                    />
                  }
                >
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>Template {index + 1}</span>
                    <Icon name="remove" />
                  </SpaceBetween>
                </Header>
              }
            >
              <SpaceBetween size="m">
                <FormField label="Label">
                  <Input
                    value={template.label}
                    onChange={({ detail }) => handleLabelChange(index, detail.value)}
                    placeholder="e.g., SUMMARY, DETAILS, ACTIONS"
                  />
                </FormField>
                <FormField
                  label='Prompt (set to "NONE" to disable this section)'
                  description={
                    'Use {transcript} for the meeting transcript. Use {language} to control exactly where the ' +
                    "output-language instruction goes — otherwise it's appended automatically when a meeting " +
                    'requests one.'
                  }
                >
                  <Textarea
                    value={template.prompt}
                    onChange={({ detail }) => handlePromptChange(index, detail.value)}
                    placeholder="Enter prompt template... Use {transcript} as placeholder."
                    rows={6}
                  />
                </FormField>
              </SpaceBetween>
            </Container>
          ))}

          <Button iconName="add-plus" onClick={handleAddTemplate}>
            Add Template
          </Button>
        </SpaceBetween>
      </Container>

      <ExpandableSection headerText="View Default Templates (read-only)" variant="container">
        <Box variant="code">
          <pre>{JSON.stringify(defaultConfig, null, 2)}</pre>
        </Box>
      </ExpandableSection>

      <Modal
        visible={showNewProfileModal}
        onDismiss={() => setShowNewProfileModal(false)}
        header="New summary profile"
        footer={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setShowNewProfileModal(false)}>Cancel</Button>
            <Button variant="primary" onClick={handleCreateProfile} loading={saving}>
              Create
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Profile name" errorText={newProfileError}>
            <Input
              value={newProfileName}
              onChange={({ detail }) => setNewProfileName(detail.value)}
              placeholder="e.g., Technical Client Call"
            />
          </FormField>
          <Box variant="small">Starts from the Default templates — edit and Save Changes once created.</Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default TranscriptSummaryPage;
