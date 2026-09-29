import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

const SummaryGenerateButton = ({ hasSummary, disabled, loading, onGenerate }) => {
  const [confirming, setConfirming] = useState(false);
  const generate = () => {
    if (disabled || loading) return;
    setConfirming(false);
    onGenerate();
  };
  return (
    <>
      <Button
        iconName={hasSummary ? 'refresh' : 'gen-ai'}
        variant={hasSummary ? 'normal' : 'primary'}
        loading={loading}
        disabled={disabled}
        onClick={() => (hasSummary ? setConfirming(true) : generate())}
      >
        {hasSummary ? 'Regenerate summary' : 'Generate summary'}
      </Button>
      <Modal
        visible={confirming}
        onDismiss={() => setConfirming(false)}
        closeAriaLabel="Cancel regeneration"
        header="Regenerate summary?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button variant="primary" disabled={disabled || loading} onClick={generate}>
                Regenerate
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        The existing summary will be replaced and cannot be restored through LMA. If generation fails, the existing
        summary will be kept. Are you sure you want to continue?
      </Modal>
    </>
  );
};
SummaryGenerateButton.propTypes = {
  hasSummary: PropTypes.bool.isRequired,
  disabled: PropTypes.bool.isRequired,
  loading: PropTypes.bool.isRequired,
  onGenerate: PropTypes.func.isRequired,
};
export default SummaryGenerateButton;
