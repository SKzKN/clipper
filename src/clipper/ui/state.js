// Shared editor state, and the functions the editor shell lends to the panels.
export const S = {
  cfg: null,          // server capabilities and templates
  project: null,      // project payload
  words: null,        // [[text, start, end], ...] in source time
  fillers: new Set(),
  edl: null,          // current EDL payload
  kept: [],           // indices of words still in the edit
  sel: null,          // transcript selection {a, b}
  selectedItem: null, // {kind, id} of a selected zoom / overlay / b-roll / sound
  tab: 'AI',
  mode: 'live',
  styles: [],
  peaks: null,
  wordEls: [],
  nowWord: -1,
};

// Filled in by app.js: op, seek, now, selRange, pickFile, applyEdl, renderPreview, exportVideo, player
export const ctx = {};
