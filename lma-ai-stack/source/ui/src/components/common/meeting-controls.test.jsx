import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { deleteModal } from './meeting-controls';

const { state } = vi.hoisted(() => ({ state: { isAdmin: false } }));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => state }));
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const DeleteControls = () => deleteModal({ calls: [], selectedItems: [], loading: false });
afterEach(cleanup);

it('hides deletion entirely for ordinary users', () => {
  state.isAdmin = false;
  render(
    <MemoryRouter>
      <DeleteControls />
    </MemoryRouter>,
  );
  expect(screen.queryByRole('button', { name: 'Delete meetings' })).not.toBeInTheDocument();
});

it('offers the delete control to administrators', () => {
  state.isAdmin = true;
  render(
    <MemoryRouter>
      <DeleteControls />
    </MemoryRouter>,
  );
  expect(screen.getByRole('button', { name: 'Delete meetings' })).toBeInTheDocument();
});
