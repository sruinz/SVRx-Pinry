export const SORT_MODES = Object.freeze(['latest', 'oldest', 'random']);
export const MAX_RANDOM_SEED = 2147483646;
const STATE_FIELDS = ['mode', 'randomSeed', 'version'];

export function generateRandomSeed(cryptoObject = window.crypto) {
  const values = new Uint32Array(1);
  cryptoObject.getRandomValues(values);
  return values[0] % (MAX_RANDOM_SEED + 1);
}

export function pinSortStorageKey(filters = {}) {
  if (filters.idFilter) return null;
  if (filters.boardFilter) return `svrx.pinSort.v1:board:${Number(filters.boardFilter)}`;
  if (filters.userFilter) return `svrx.pinSort.v1:user:${encodeURIComponent(filters.userFilter)}`;
  if (filters.tagFilter) return `svrx.pinSort.v1:tag:${encodeURIComponent(filters.tagFilter)}`;
  return 'svrx.pinSort.v1:home';
}

function fallback(seedFactory) {
  return { version: 1, mode: 'latest', randomSeed: seedFactory() };
}

function valid(state) {
  if (!state || typeof state !== 'object' || Array.isArray(state)) return false;
  const fields = Object.keys(state).sort();
  return fields.length === STATE_FIELDS.length
    && fields.every((field, index) => field === STATE_FIELDS[index])
    && state.version === 1
    && SORT_MODES.includes(state.mode)
    && Number.isInteger(state.randomSeed)
    && state.randomSeed >= 0
    && state.randomSeed <= MAX_RANDOM_SEED;
}

export function writePinSortState(storage, key, state) {
  if (!key || !valid(state)) return false;
  try {
    storage.setItem(key, JSON.stringify(state));
    return true;
  } catch (_error) {
    return false;
  }
}

export function readPinSortState(storage, key, seedFactory = generateRandomSeed) {
  const defaultState = fallback(seedFactory);
  if (!key) return defaultState;
  try {
    const state = JSON.parse(storage.getItem(key));
    if (valid(state)) return state;
    storage.removeItem(key);
  } catch (_error) {
    try { storage.removeItem(key); } catch (_ignored) { /* 메모리 대체 상태를 사용한다. */ }
  }
  writePinSortState(storage, key, defaultState);
  return defaultState;
}

export function transitionPinSortState(state, mode, seedFactory = generateRandomSeed) {
  if (!SORT_MODES.includes(mode)) return { state, changed: false, reshuffled: false };
  if (state.mode === mode && mode !== 'random') {
    return { state, changed: false, reshuffled: false };
  }
  let { randomSeed } = state;
  const reshuffled = state.mode === 'random' && mode === 'random';
  if (reshuffled) {
    randomSeed = seedFactory();
    if (randomSeed === state.randomSeed) {
      randomSeed = (randomSeed + 1) % (MAX_RANDOM_SEED + 1);
    }
  }
  return {
    state: { version: 1, mode, randomSeed },
    changed: true,
    reshuffled,
  };
}
