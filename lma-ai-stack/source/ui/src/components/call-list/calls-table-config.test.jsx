import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { COLUMN_DEFINITIONS_MAIN, DEFAULT_PREFERENCES, DEFAULT_SORT_COLUMN } from './calls-table-config';

afterEach(cleanup);

it('offers exactly the six requested columns in order', () => {
  expect(COLUMN_DEFINITIONS_MAIN.map((column) => column.header)).toEqual([
    'Meeting ID',
    'Status',
    'Owner Name',
    'Summary',
    'Duration',
    'Initiation Timestamp',
  ]);
  expect(DEFAULT_PREFERENCES.visibleContent).toEqual([
    'recordingStatus',
    'agentId',
    'summary',
    'conversationDuration',
    'initiationTimeStamp',
  ]);
});

it('formats DD.MM.YYYY while sorting by the original chronological timestamp', () => {
  const cell = DEFAULT_SORT_COLUMN.cell({ initiationTimeStamp: '2026-09-29T12:00:00Z' });
  render(<div>{cell}</div>);
  expect(screen.getByText('29.09.2026')).toBeInTheDocument();
  expect(DEFAULT_SORT_COLUMN.sortingField).toBe('initiationTimeStamp');
});
