/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/** Never advertise a selectable profile before its usable prompts are saved. */
const createSummaryProfile = async ({ id, name, catalog, defaults, saveProfile, saveCatalog }) => {
  const sections = Object.fromEntries(
    Object.entries(defaults || {}).filter(
      ([key, value]) => /^\d+#.+$/.test(key) && typeof value === 'string' && value.trim() && value !== 'NONE',
    ),
  );
  if (!Object.keys(sections).length) {
    throw new Error('Default summary templates are empty. Configure them before creating a profile.');
  }
  await saveProfile(id, sections);
  await saveCatalog([...catalog, { id, name }]);
};

export default createSummaryProfile;
