/**
 * Local preferences.
 *
 * The server supplies the defaults; anything the user changes in the settings
 * panel is remembered in this browser only.
 */

const KEY = 'micclaude.preferences.v1';

export function loadPreferences() {
  try {
    const stored = localStorage.getItem(KEY);
    return stored ? JSON.parse(stored) : {};
  } catch {
    return {};
  }
}

export function savePreferences(preferences) {
  try {
    localStorage.setItem(KEY, JSON.stringify(preferences));
  } catch {
    // Private windows and blocked storage: preferences simply do not persist.
  }
}

/** Merge stored preferences over the server defaults. */
export function mergeSettings(defaults, preferences) {
  return {
    audio: { ...defaults.audio, ...preferences.audio },
    trigger: { ...defaults.trigger, ...preferences.trigger },
    speech: { ...defaults.speech, ...preferences.speech },
    contextLines: preferences.contextLines ?? defaults.contextLines,
    deviceId: preferences.deviceId ?? null,
    // The spoken language belongs to the server; the page only follows it.
    language: defaults.language ?? 'en',
  };
}
