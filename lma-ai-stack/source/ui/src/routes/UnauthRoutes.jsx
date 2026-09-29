/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Authenticator } from '@aws-amplify/ui-react';
import { signInWithRedirect } from 'aws-amplify/auth';
import { Hub } from 'aws-amplify/utils';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';

import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';
import OAuthCallback from '../components/mcp-servers/OAuthCallback';

// Set at build time via the AllowedSignUpEmailDomain CloudFormation parameter.
const VITE_SHOULD_HIDE_SIGN_UP = import.meta.env.VITE_SHOULD_HIDE_SIGN_UP ?? 'true';
const GOOGLE_ENABLED = import.meta.env.VITE_GOOGLE_SSO_ENABLED === 'true';
const PASSWORD_ENABLED = import.meta.env.VITE_PASSWORD_SIGN_IN_ENABLED !== 'false';

const GoogleSignIn = () => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(
    () =>
      Hub.listen('auth', ({ payload }) => {
        if (payload.event === 'signInWithRedirect_failure') {
          setLoading(false);
          setError('Google sign-in failed. Please use your company Google account and try again.');
        }
      }),
    [],
  );
  const login = async () => {
    setLoading(true);
    setError('');
    const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
    const target = params.get(REDIRECT_URL_PARAM) || '/calls';
    sessionStorage.setItem('lma-sso-return-to', target.startsWith('/') && !target.startsWith('//') ? target : '/calls');
    try {
      await signInWithRedirect({ provider: { custom: 'GoogleWorkspace' } });
    } catch (err) {
      setLoading(false);
      setError('Unable to start Google sign-in. Please try again.');
    }
  };
  return (
    <div style={{ textAlign: 'center', margin: '1rem' }}>
      {error && <Alert type="error">{error}</Alert>}
      <Button variant="primary" loading={loading} onClick={login}>
        Continue with Google
      </Button>
    </div>
  );
};

// White-background variant: the transparent logo used in the top navigation
// has a white wordmark that vanishes on the light login card.
const AuthHeader = () => (
  <div style={{ textAlign: 'center', margin: '2rem 0' }}>
    <img
      src="/allops-logo-light.jpg"
      alt="allOps"
      style={{ height: '96px', maxWidth: '100%', marginBottom: '0.5rem' }}
    />
    <h1>Welcome to Live Meeting Assistant!</h1>
    {GOOGLE_ENABLED && <GoogleSignIn />}
  </div>
);

const AuthPanel = () =>
  PASSWORD_ENABLED ? (
    <Authenticator
      initialState="signIn"
      components={{ Header: AuthHeader }}
      services={{
        async validateCustomSignUp(formData) {
          if (formData.email) {
            return undefined;
          }
          return { email: 'Email is required' };
        },
      }}
      signUpAttributes={['email']}
      hideSignUp={GOOGLE_ENABLED || VITE_SHOULD_HIDE_SIGN_UP === 'true'}
    />
  ) : (
    <AuthHeader />
  );

const UnauthRoutes = ({ location }) => (
  <Routes>
    <Route path="/oauth/callback" element={<OAuthCallback />} />
    <Route path={LOGIN_PATH} element={<AuthPanel />} />
    <Route path={LOGOUT_PATH} element={<Navigate to={LOGIN_PATH} replace />} />
    <Route
      path="*"
      element={
        <Navigate
          to={{
            pathname: LOGIN_PATH,
            search: `?${REDIRECT_URL_PARAM}=${encodeURIComponent(location.pathname + location.search)}`,
          }}
          replace
        />
      }
    />
  </Routes>
);

UnauthRoutes.propTypes = {
  location: PropTypes.shape({
    pathname: PropTypes.string,
    search: PropTypes.string,
  }).isRequired,
};

export default UnauthRoutes;
