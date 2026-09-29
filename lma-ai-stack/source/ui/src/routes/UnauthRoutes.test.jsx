import React from 'react';
import PropTypes from 'prop-types';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { signInWithRedirect, listen } = vi.hoisted(() => ({
  signInWithRedirect: vi.fn(),
  listen: vi.fn(() => () => {}),
}));
vi.mock('aws-amplify/auth', () => ({ signInWithRedirect }));
vi.mock('aws-amplify/utils', () => ({ Hub: { listen } }));
vi.mock('@aws-amplify/ui-react', () => {
  const Authenticator = ({ components }) => (
    <div data-testid="password-form">
      <components.Header />
    </div>
  );
  Authenticator.propTypes = { components: PropTypes.shape({ Header: PropTypes.elementType.isRequired }).isRequired };
  return { Authenticator };
});
vi.mock('@cloudscape-design/components/button', () => ({
  default: ({ children, onClick }) => (
    <button type="button" onClick={onClick}>
      {children}
    </button>
  ),
}));
vi.mock('@cloudscape-design/components/alert', () => ({
  default: ({ children }) => <div role="alert">{children}</div>,
}));
vi.mock('../components/mcp-servers/OAuthCallback', () => ({ default: () => null }));

async function loginPage(google, passwords) {
  vi.stubEnv('VITE_GOOGLE_SSO_ENABLED', google);
  vi.stubEnv('VITE_PASSWORD_SIGN_IN_ENABLED', passwords);
  const { default: UnauthRoutes } = await import('./UnauthRoutes');
  render(
    <MemoryRouter initialEntries={['/login']}>
      <UnauthRoutes location={{ pathname: '/login', search: '' }} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  sessionStorage.clear();
  window.location.hash = '';
  signInWithRedirect.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

describe('login modes', () => {
  it('keeps password login when Google is disabled', async () => {
    await loginPage('false', 'true');
    expect(screen.getByTestId('password-form')).toBeInTheDocument();
    expect(screen.queryByText('Continue with Google')).not.toBeInTheDocument();
  });
  it('offers both logins during a dev SSO test', async () => {
    await loginPage('true', 'true');
    expect(screen.getByTestId('password-form')).toBeInTheDocument();
    expect(screen.getByText('Continue with Google')).toBeInTheDocument();
  });
  it('shows Google without password fields in SSO-only mode', async () => {
    await loginPage('true', 'false');
    expect(screen.queryByTestId('password-form')).not.toBeInTheDocument();
    expect(screen.getByText('Continue with Google')).toBeInTheDocument();
    expect(screen.getByAltText('allOps')).toHaveAttribute('src', '/allops-logo-light.jpg');
  });
  it('uses the CloudFront-adapted Cognito provider and preserves the requested route', async () => {
    await loginPage('true', 'true');
    window.location.hash = '#/login?redirect=%2Fcalls%2Fmeeting';
    fireEvent.click(screen.getByText('Continue with Google'));
    await waitFor(() => expect(signInWithRedirect).toHaveBeenCalledWith({ provider: { custom: 'GoogleWorkspace' } }));
    expect(sessionStorage.getItem('lma-sso-return-to')).toBe('/calls/meeting');
  });
  it('reports sign-in startup failures without exposing the exception', async () => {
    signInWithRedirect.mockRejectedValue(new Error('sensitive details'));
    await loginPage('true', 'false');
    fireEvent.click(screen.getByText('Continue with Google'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Unable to start Google sign-in'));
    expect(screen.getByRole('alert')).not.toHaveTextContent('sensitive details');
  });
});
