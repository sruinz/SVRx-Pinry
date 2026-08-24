export const BOARD_SORT_MODES = Object.freeze(['custom', 'latest', 'oldest', 'random']);
export const MAX_RANDOM_SEED = 2147483646;
const STATE_FIELDS = ['mode', 'randomSeed', 'version'];

export function generateRandomSeed(cryptoObject = window.crypto) {
  if (!cryptoObject || typeof cryptoObject.getRandomValues !== 'function') {
    return Math.floor(Math.random() * (MAX_RANDOM_SEED + 1));
  }
  const values = new Uint32Array(1);
  cryptoObject.getRandomValues(values);
  return values[0] % (MAX_RANDOM_SEED + 1);
}

export function boardSortStorageKey(username) {
  if (!username) return null;
  return `svrx.boardSort.v1:user:${encodeURIComponent(username)}`;
}

function fallback(seedFactory) {
  return { version: 1, mode: 'custom', randomSeed: seedFactory() };
}

function valid(state) {
  if (!state || typeof state !== 'object' || Array.isArray(state)) return false;
  const fields = Object.keys(state).sort();
  return fields.length === STATE_FIELDS.length
    && fields.every((field, index) => field === STATE_FIELDS[index])
    && state.version === 1
    && BOARD_SORT_MODES.includes(state.mode)
    && Number.isInteger(state.randomSeed)
    && state.randomSeed >= 0
    && state.randomSeed <= MAX_RANDOM_SEED;
}

export function writeBoardSortState(storage, key, state) {
  if (!key || !valid(state)) return false;
  try {
    storage.setItem(key, JSON.stringify(state));
    return true;
  } catch (_error) {
    return false;
  }
}

export function readBoardSortState(storage, key, seedFactory = generateRandomSeed) {
  const defaultState = fallback(seedFactory);
  if (!key) return defaultState;
  try {
    const state = JSON.parse(storage.getItem(key));
    if (valid(state)) return state;
    storage.removeItem(key);
  } catch (_error) {
    try { storage.removeItem(key); } catch (_ignored) { /* 메모리 대체 상태를 사용한다. */ }
  }
  writeBoardSortState(storage, key, defaultState);
  return defaultState;
}

export function transitionBoardSortState(state, mode, seedFactory = generateRandomSeed) {
  if (!BOARD_SORT_MODES.includes(mode)) return { state, changed: false, reshuffled: false };
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
