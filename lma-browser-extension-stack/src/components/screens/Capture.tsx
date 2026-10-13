/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect } from 'react';
import logo from './logo.svg';
import './Capture.css'
import { Box, Button, Container, ContentLayout, CopyToClipboard, FormField, Grid, Header, Icon, Input, Link, Modal, Select, SpaceBetween } from '@cloudscape-design/components';
import UserMessage from '../views/UserMessage';
import OtherMessage from '../views/OtherMessage';
import { useNavigation } from '../../context/NavigationContext';
import AssistantMessage from '../views/AssistantMessage';
import ValueWithLabel from '../views/ValueWithLabel';
import { useUserContext } from '../../context/UserContext';
import { useIntegration } from '../../context/ProviderIntegrationContext';
import { useSettings } from '../../context/SettingsContext';
import { DEFAULT_TRANSCRIBE_LANGUAGE_MODE, TRANSCRIBE_LANGUAGE_MODE_OPTIONS } from '../../context/meetingOptions';
import { nameFromIdToken } from '../../lib/identity';
import { prefillField } from '../../lib/capture';

function Capture() {
  const { navigate } = useNavigation();
  const { user, logout } = useUserContext();
  const settings = useSettings();
  const { currentCall, muted, setMuted, paused, setPaused, activeSpeaker, metadata, fetchMetadata, isTranscribing, startTranscription, stopTranscription, platform } = useIntegration();

  const [topic, setTopic] = React.useState("");
  const [agentName, setAgentName] = React.useState("");
  const [nameErrorText, setNameErrorText] = React.useState("");
  const [meetingTopicErrorText, setMeetingTopicErrorText] = React.useState("");
  const [showDisclaimer, setShowDisclaimer] = React.useState(false);
  // What we last prefilled these two fields with. A field still holding our own
  // prefill is ours to refresh when a reader reports again or the user switches
  // to another meeting tab; a field holding anything else is theirs, and the
  // readers report often enough that overwriting it would eat what they typed.
  const prefilled = React.useRef({ topic: "", agentName: "" });

  // Per-meeting language mode, same as the web UI's Stream Audio page. The
  // summary profile/language are chosen later, on the meeting page in LMA.
  const [transcribeLanguageMode, setTranscribeLanguageMode] = React.useState(DEFAULT_TRANSCRIBE_LANGUAGE_MODE);

  // componentDidMount:
  useEffect(() => {
    // Your code here
    fetchMetadata();
  }, []);

  useEffect(() => {
    console.log("Metadata changed");
    if (!metadata) {
      return;
    }
    const nextTopic = prefillField(topic, prefilled.current.topic, metadata.meetingTopic);
    if (nextTopic !== topic) {
      prefilled.current.topic = nextTopic;
      setTopic(nextTopic);
    }
    const nextName = prefillField(agentName, prefilled.current.agentName, metadata.userName);
    if (nextName !== agentName) {
      prefilled.current.agentName = nextName;
      setAgentName(nextName);
    }
  }, [metadata, topic, agentName, setTopic, setAgentName]);

  // The name the meeting page gives us is better, but it is often missing: a
  // changed page, a lobby we have not been admitted from, or a platform with no
  // reader. Fall back to the signed-in user so the field is not left blank.
  // Only while the field is untouched and nothing has been put in it yet, so
  // that a name the user clears is not refilled under them on the next render.
  useEffect(() => {
    if (agentName || prefilled.current.agentName) {
      return;
    }
    const loginName = nameFromIdToken(user.id_token);
    if (loginName) {
      prefilled.current.agentName = loginName;
      setAgentName(loginName);
    }
  }, [user, agentName, setAgentName]);

  const validateForm = useCallback(() => {
    let isValid = true;
    if (agentName === undefined || agentName.trim().length === 0) {
      setNameErrorText("Name required.")
      isValid = false
    } else {
      setNameErrorText("");
    }
    if (topic === undefined || topic.trim().length === 0) {
      setMeetingTopicErrorText("Topic required.")
      isValid = false;
    } else {
      setMeetingTopicErrorText("");
    }
    return isValid;
  }, [topic, agentName, nameErrorText, setNameErrorText, meetingTopicErrorText, setMeetingTopicErrorText]);

  const startListening = useCallback(() => {
    // eslint-disable-next-line no-useless-escape
    const sanitized = topic.replace(/[\/?#%\+&]/g, '|');
    // Recorded as the prefilled value too, when that is what it came from:
    // sanitizing it would otherwise look like the user had edited the field,
    // and it would stop tracking the meeting page from here on.
    if (topic === prefilled.current.topic) {
      prefilled.current.topic = sanitized;
    }
    setTopic(sanitized);

    if (validateForm() === false) {
      return;
    }
    setShowDisclaimer(true);
  }, [settings, validateForm, showDisclaimer]);

  const disclaimerConfirmed = useCallback(() => {
    startTranscription(user, agentName, topic, { transcribeLanguageMode });
  }, [user, agentName, topic, transcribeLanguageMode, startTranscription])

  const stopListening = useCallback(() => {
    stopTranscription();
  }, [stopTranscription]);

  const openInLMA = useCallback(async () => {
    const url = `${settings.cloudfrontEndpoint}/#/calls/${currentCall.callId}`;
    window.open(url, '_blank', 'noreferrer');
  }, [currentCall, settings])

  const mute = useCallback(() => {
    setMuted(true);
  }, [muted, setMuted]);

  const unmute = useCallback(() => {
    setMuted(false);
  }, [muted, setMuted]);

  const [version, setVersion] = React.useState("");
  useEffect(() => {
    if (chrome && chrome.runtime) {
      const manifestData = chrome.runtime.getManifest();
      setVersion(manifestData.version)
    } else {
      setVersion("dev/web");
    }
  }, [version, setVersion]);

  return (
    <ContentLayout
      header={
        <SpaceBetween size={'xs'}>
          <Header
            variant="h1"
            description="allOps Live Meeting Assistant — powered by Amazon Transcribe and Amazon Bedrock"
          >
            Live Meeting Assistant
          </Header>
        </SpaceBetween>
      }
    >
      <Container
        fitHeight={true}
        header={
          <Header variant="h2" description="">
            Meeting Details
          </Header>
        }
      >
        <Modal
          onDismiss={() => setShowDisclaimer(false)}
          visible={showDisclaimer}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={async () => {
                  setShowDisclaimer(false);
                }}>Cancel</Button>
                <Button variant="primary" onClick={async () => {
                  setShowDisclaimer(false);
                  disclaimerConfirmed();
                }}>Agree</Button>
              </SpaceBetween>
            </Box>
          }
          header="Important:"
        >
          <Icon name="status-warning"></Icon>&nbsp;
          {settings.recordingDisclaimer}
        </Modal>
        <SpaceBetween size="l">
          <ValueWithLabel label="Platform Detected:">{platform}</ValueWithLabel>
          {(isTranscribing === true ?
            <>
              <Button fullWidth={true} onClick={async () => openInLMA()}>Open in LMA</Button>
              <ValueWithLabel label="Name:">{agentName}</ValueWithLabel>
              <ValueWithLabel label="Meeting Topic:">{topic}</ValueWithLabel>
              <ValueWithLabel label="Active Speaker:">{activeSpeaker}</ValueWithLabel>
              {
                paused === true ?
                  <>
                    <Button fullWidth={true} iconName="microphone-off" onClick={() => setPaused(false)}>Unmute All</Button>
                  </>
                  :
                  <>
                    <Button fullWidth={true} iconName="microphone" onClick={() => setPaused(true)}>Mute All</Button>
                  </>
              }
              <Button fullWidth={true} variant='primary' onClick={() => stopListening()}>Stop Listening</Button>
            </>
            :
            <>
              <FormField
                stretch={true}
                constraintText=""
                errorText={nameErrorText}
                label="Your name:"
              >
                <Input value={agentName} onChange={({ detail }) => setAgentName(detail.value)} placeholder='Your name' ></Input>
              </FormField>
              <FormField
                stretch={true}
                constraintText=""
                errorText={meetingTopicErrorText}
                label="Meeting Topic:"
              >
                <Input value={topic} onChange={({ detail }) => setTopic(detail.value)} placeholder='Meeting room topic' inputMode='text'></Input>
              </FormField>
              <FormField stretch={true} label="Meeting language:">
                <Select
                  selectedOption={TRANSCRIBE_LANGUAGE_MODE_OPTIONS.find((o) => o.value === transcribeLanguageMode) || null}
                  onChange={({ detail }) => setTranscribeLanguageMode(detail.selectedOption.value || DEFAULT_TRANSCRIBE_LANGUAGE_MODE)}
                  options={TRANSCRIBE_LANGUAGE_MODE_OPTIONS}
                />
              </FormField>
              <Button fullWidth={true} variant='primary' onClick={() => startListening()}>Start Listening</Button>
            </>
          )}
          <Grid gridDefinition={[{ colspan: 6 }, { colspan: 6 }]}>
            {muted === true ?
              <Button iconAlign="left" iconName="microphone-off" fullWidth={true} onClick={() => unmute()}>Unmute Me</Button>
              :
              <Button iconAlign="left" iconName="microphone" fullWidth={true} onClick={() => mute()}>Mute Me</Button>
            }
            <Button fullWidth={true} onClick={() => logout()}>Log out</Button>
          </Grid>

          <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
            <div className='version'>{version}</div>
          </Grid>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}

export default Capture;
