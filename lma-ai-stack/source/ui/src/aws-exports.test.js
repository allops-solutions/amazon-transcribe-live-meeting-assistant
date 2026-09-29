import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => vi.unstubAllEnvs());

describe('OAuth configuration', () => {
  it('leaves native authentication unchanged when Google is disabled', async () => {
    vi.resetModules();
    vi.stubEnv('VITE_GOOGLE_SSO_ENABLED', 'false');
    const { default: config } = await import('./aws-exports');
    expect(config.oauth).toEqual({});
  });
  it('uses Cognito code flow with CloudFront callback/logout and no frontend secret', async () => {
    vi.resetModules();
    vi.stubEnv('VITE_GOOGLE_SSO_ENABLED', 'true');
    vi.stubEnv('VITE_COGNITO_DOMAIN', 'pool.auth.us-east-1.amazoncognito.com');
    vi.stubEnv('VITE_CLOUDFRONT_DOMAIN', 'https://app.cloudfront.net/');
    const { default: config } = await import('./aws-exports');
    expect(config.oauth).toEqual({
      domain: 'pool.auth.us-east-1.amazoncognito.com',
      scope: ['openid', 'email', 'profile'],
      redirectSignIn: 'https://app.cloudfront.net/',
      redirectSignOut: 'https://app.cloudfront.net/',
      responseType: 'code',
    });
    expect(config.oauth).not.toHaveProperty('clientSecret');
  });
});
