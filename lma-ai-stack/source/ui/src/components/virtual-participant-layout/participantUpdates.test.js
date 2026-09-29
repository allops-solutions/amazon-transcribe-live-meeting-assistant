import { describe, it, expect } from 'vitest';
import mergeParticipantUpdate from './participantUpdates';

describe('calendar VP subscription updates', () => {
  const participant = { id: 'vp', meetingName: 'Old title', meetingTime: 1, status: 'SCHEDULED' };

  it('applies the updated title, link and time', () => {
    expect(
      mergeParticipantUpdate(participant, { meetingName: 'Moved', meetingTime: 2, meetingId: 'new-link' }),
    ).toEqual({ ...participant, meetingName: 'Moved', meetingTime: 2, meetingId: 'new-link' });
  });

  it('preserves fields omitted by status-only mutations', () => {
    expect(
      mergeParticipantUpdate(participant, { status: 'CANCELLED', meetingName: null, meetingTime: undefined }),
    ).toEqual({ ...participant, status: 'CANCELLED' });
  });

  it('keeps meaningful false and empty values', () => {
    expect(mergeParticipantUpdate(participant, { isScheduled: false, SharedWith: '' })).toEqual({
      ...participant,
      isScheduled: false,
      SharedWith: '',
    });
  });

  it('ignores older notifications', () => {
    const current = { ...participant, updatedAt: '2099-01-01T11:00:00Z' };
    expect(mergeParticipantUpdate(current, { status: 'SCHEDULED', updatedAt: '2099-01-01T10:00:00Z' })).toBe(current);
  });
});
