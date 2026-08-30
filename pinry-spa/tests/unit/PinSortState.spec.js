/* eslint-env jest */
import {
  pinSortStorageKey,
  readPinSortState,
  transitionPinSortState,
  writePinSortState,
} from '@/components/sorting/pinSortState';

describe('browser-local Pin sorting state', () => {
  beforeEach(() => localStorage.clear());

  it('isolates home, user, board, and tag keys', () => {
    expect(pinSortStorageKey({})).toBe('svrx.pinSort.v1:home');
    expect(pinSortStorageKey({ userFilter: 'a b' }))
      .toBe('svrx.pinSort.v1:user:a%20b');
    expect(pinSortStorageKey({ boardFilter: 7 }))
      .toBe('svrx.pinSort.v1:board:7');
    expect(pinSortStorageKey({ tagFilter: '애니' }))
      .toBe('svrx.pinSort.v1:tag:%EC%95%A0%EB%8B%88');
    expect(pinSortStorageKey({ idFilter: 3 })).toBeNull();
  });

  it('uses an order-independent key for a multiple-tag search', () => {
    const first = pinSortStorageKey({
      tagFilter: ['travel', 'night view', 'travel'],
    });
    const second = pinSortStorageKey({
      tagFilter: ['night view', 'travel'],
    });

    expect(first).toBe('svrx.pinSort.v1:tags:night%20view|travel');
    expect(second).toBe(first);
    expect(pinSortStorageKey({ tagFilter: [] }))
      .toBe('svrx.pinSort.v1:home');
  });

  it('restores valid state and replaces malformed state with latest', () => {
    const key = pinSortStorageKey({});
    localStorage.setItem(key, JSON.stringify({ version: 1, mode: 'random', randomSeed: 9 }));
    expect(readPinSortState(localStorage, key, () => 4))
      .toEqual({ version: 1, mode: 'random', randomSeed: 9 });
    localStorage.setItem(key, '{broken');
    expect(readPinSortState(localStorage, key, () => 4))
      .toEqual({ version: 1, mode: 'latest', randomSeed: 4 });
  });

  it('changes seed only when active random is clicked again', () => {
    const latest = { version: 1, mode: 'latest', randomSeed: 7 };
    const first = transitionPinSortState(latest, 'random', () => 8);
    expect(first).toEqual({
      state: { version: 1, mode: 'random', randomSeed: 7 },
      changed: true,
      reshuffled: false,
    });
    const second = transitionPinSortState(first.state, 'random', () => 8);
    expect(second.state.randomSeed).toBe(8);
    expect(second.reshuffled).toBe(true);
  });

  it('rejects unexpected fields and invalid random seeds before persisting', () => {
    const key = pinSortStorageKey({});
    const invalidStates = [
      {
        version: 1, mode: 'latest', randomSeed: 1, extra: true,
      },
      { version: 1, mode: 'latest', randomSeed: 1.5 },
      { version: 1, mode: 'latest', randomSeed: -1 },
      { version: 1, mode: 'latest', randomSeed: 2147483647 },
    ];

    invalidStates.forEach((state) => {
      expect(writePinSortState(localStorage, key, state)).toBe(false);
    });
    expect(localStorage.getItem(key)).toBeNull();
  });

  it('falls back when storage reads or cleanup fail', () => {
    const storage = {
      getItem: () => { throw new Error('read failed'); },
      removeItem: () => { throw new Error('cleanup failed'); },
      setItem: () => { throw new Error('write failed'); },
    };

    expect(readPinSortState(storage, 'svrx.pinSort.v1:home', () => 5))
      .toEqual({ version: 1, mode: 'latest', randomSeed: 5 });
  });

  it('reports failed writes without throwing', () => {
    const storage = {
      setItem: () => { throw new Error('write failed'); },
    };

    expect(writePinSortState(storage, 'svrx.pinSort.v1:home', {
      version: 1, mode: 'oldest', randomSeed: 5,
    })).toBe(false);
  });
});
