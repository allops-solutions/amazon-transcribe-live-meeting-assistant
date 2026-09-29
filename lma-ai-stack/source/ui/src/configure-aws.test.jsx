import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { renderHook, cleanup } from '@testing-library/react';
import React from 'react';

const { configure, appImport, listen, config } = vi.hoisted(() => ({
  configure: vi.fn(),
  appImport: vi.fn(),
  listen: vi.fn(() => () => {}),
  config: { test: 'configuration' },
}));
vi.mock('aws-amplify', () => ({ Amplify: { configure } }));
vi.mock('aws-amplify/auth/enable-oauth-listener', () => ({}));
vi.mock('aws-amplify/utils', () => ({ Hub: { listen } }));
vi.mock('./aws-exports', () => ({ default: config }));
vi.mock('./App', () => {
  // Imported components may construct Amplify clients before React mounts.
  appImport(configure.mock.calls.length);
  return { default: () => null };
});
vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: vi.fn() }) }));

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});
afterEach(cleanup);

it('configures Amplify before App dependencies are evaluated', async () => {
  await import('./index');
  expect(appImport).toHaveBeenCalledWith(1);
  expect(configure).toHaveBeenCalledExactlyOnceWith(config);
});

it('does not reconfigure during StrictMode effects or hook remounts', async () => {
  const { default: useAwsConfig } = await import('./hooks/use-aws-config');
  const first = renderHook(useAwsConfig, { wrapper: React.StrictMode });
  expect(first.result.current).toBe(config);
  first.unmount();
  const second = renderHook(useAwsConfig, { wrapper: React.StrictMode });
  expect(second.result.current).toBe(config);
  expect(configure).toHaveBeenCalledExactlyOnceWith(config);
});
