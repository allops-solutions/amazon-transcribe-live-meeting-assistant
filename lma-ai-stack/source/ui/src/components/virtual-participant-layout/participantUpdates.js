/* Copyright (c) 2025 Amazon.com. Licensed under the MIT License. */

const mergeParticipantUpdate = (participant, update) => {
  if (Date.parse(update.updatedAt) < Date.parse(participant.updatedAt)) return participant;
  // Calendar notifications carry full rows; status-only mutations may deliver
  // omitted fields as null. Preserve those values rather than blanking the row.
  return {
    ...participant,
    ...Object.fromEntries(Object.entries(update).filter(([, value]) => value !== null && value !== undefined)),
  };
};

export default mergeParticipantUpdate;
