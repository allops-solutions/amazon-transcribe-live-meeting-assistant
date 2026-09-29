import React from 'react';
import PropTypes from 'prop-types';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CallAnalyticsTopNavigation from './CallAnalyticsTopNavigation';

const { context, fetchUserAttributes } = vi.hoisted(() => ({
  context: { value: {} },
  fetchUserAttributes: vi.fn(),
}));
vi.mock('../../contexts/app', () => ({ default: () => context.value }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => ({ isAdmin: true }) }));
vi.mock('aws-amplify/auth', () => ({ fetchUserAttributes, signOut: vi.fn() }));
vi.mock('aws-amplify/utils', () => ({
  ConsoleLogger: function ConsoleLogger() {
    this.error = vi.fn();
  },
}));
vi.mock('@cloudscape-design/components', () => {
  const TopNavigation = ({ utilities }) => <div>{utilities[0].text}</div>;
  TopNavigation.propTypes = {
    utilities: PropTypes.arrayOf(PropTypes.shape({ text: PropTypes.string })).isRequired,
  };
  return {
    TopNavigation,
    Modal: () => null,
    Box: () => null,
    Button: () => null,
    SpaceBetween: () => null,
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  context.value = {
    authState: 'authenticated',
    user: { username: 'googleworkspace_123', userId: 'account-one' },
  };
});
afterEach(cleanup);

it('displays the Google email from the ID token without an attribute request', () => {
  context.value.currentSession = { tokens: { idToken: { payload: { email: 'ahmed@allops.co' } } } };
  render(<CallAnalyticsTopNavigation />);
  expect(screen.getByText('ahmed@allops.co (admin)')).toBeInTheDocument();
  expect(screen.queryByText(/googleworkspace_/)).not.toBeInTheDocument();
  expect(fetchUserAttributes).not.toHaveBeenCalled();
});

it('uses fetched email when no session email is available', async () => {
  fetchUserAttributes.mockResolvedValue({ email: 'native@allops.co' });
  render(<CallAnalyticsTopNavigation />);
  await waitFor(() => expect(screen.getByText('native@allops.co (admin)')).toBeInTheDocument());
});

it('updates the display when the signed-in session changes', () => {
  context.value.currentSession = { tokens: { idToken: { payload: { email: 'first@allops.co' } } } };
  const view = render(<CallAnalyticsTopNavigation />);
  context.value = {
    ...context.value,
    user: { username: 'googleworkspace_456', userId: 'account-two' },
    currentSession: { tokens: { idToken: { payload: { email: 'second@allops.co' } } } },
  };
  view.rerender(<CallAnalyticsTopNavigation />);
  expect(screen.getByText('second@allops.co (admin)')).toBeInTheDocument();
  expect(screen.queryByText('first@allops.co (admin)')).not.toBeInTheDocument();
});
