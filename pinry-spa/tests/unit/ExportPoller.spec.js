/* eslint-env jest */
import flushPromises from 'flush-promises';
import { createExportPoller } from '@/components/export/exportPoller';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function active(percent = 40) {
  return { state: 'archiving', overall_percent: percent };
}

function terminal(state = 'complete') {
  return { state, overall_percent: 100 };
}

function fakeDocument() {
  const listeners = {};
  return {
    hidden: false,
    addEventListener: jest.fn((name, callback) => { listeners[name] = callback; }),
    removeEventListener: jest.fn((name, callback) => {
      if (listeners[name] === callback) delete listeners[name];
    }),
    dispatchVisibility() {
      if (listeners.visibilitychange) listeners.visibilitychange();
    },
  };
}

function setup(options = {}) {
  const requests = [];
  const fetchLatest = jest.fn(() => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  });
  const onData = jest.fn();
  const onError = jest.fn();
  const poller = createExportPoller({
    fetchLatest, onData, onError, ...options,
  });
  return {
    poller, fetchLatest, onData, onError, requests,
  };
}

async function rejectAndAdvance(requests, fetchLatest, delay) {
  const callsBeforeDelay = fetchLatest.mock.calls.length;
  requests[requests.length - 1].reject(new Error('network'));
  await flushPromises();
  jest.advanceTimersByTime(delay - 1);
  expect(fetchLatest).toHaveBeenCalledTimes(callsBeforeDelay);
  jest.advanceTimersByTime(1);
  expect(fetchLatest).toHaveBeenCalledTimes(callsBeforeDelay + 1);
}

async function expectTerminalRecovery(state, pollers) {
  const {
    poller, fetchLatest, onError, requests,
  } = setup();
  pollers.push(poller);
  poller.start();
  requests[0].resolve(terminal(state));
  await flushPromises();
  jest.advanceTimersByTime(30000);
  expect(fetchLatest).toHaveBeenCalledTimes(1);
  poller.refresh();
  requests[1].reject(new Error('manual failure'));
  await flushPromises();
  expect(onError).toHaveBeenCalledTimes(1);
  jest.advanceTimersByTime(30000);
  expect(fetchLatest).toHaveBeenCalledTimes(2);
  poller.refresh();
  requests[2].resolve(active());
  await flushPromises();
  jest.advanceTimersByTime(2000);
  expect(fetchLatest).toHaveBeenCalledTimes(4);
}

describe('export poller', () => {
  let pollers;

  beforeEach(() => {
    jest.useFakeTimers();
    pollers = [];
  });

  afterEach(() => {
    pollers.forEach(poller => poller.stop());
    jest.useRealTimers();
  });

  it('requests immediately and waits for an active result before one two-second timer', async () => {
    const { poller, fetchLatest, requests } = setup();
    pollers.push(poller);
    poller.start();
    expect(fetchLatest).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(10000);
    expect(fetchLatest).toHaveBeenCalledTimes(1);

    requests[0].resolve(active());
    await flushPromises();
    jest.advanceTimersByTime(1999);
    expect(fetchLatest).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(1);
    expect(fetchLatest).toHaveBeenCalledTimes(2);
  });

  it('backs off at 2, 4, 8, 16, and 30 seconds, then resets after success', async () => {
    const { poller, fetchLatest, requests } = setup();
    pollers.push(poller);
    poller.start();

    await rejectAndAdvance(requests, fetchLatest, 2000);
    await rejectAndAdvance(requests, fetchLatest, 4000);
    await rejectAndAdvance(requests, fetchLatest, 8000);
    await rejectAndAdvance(requests, fetchLatest, 16000);
    await rejectAndAdvance(requests, fetchLatest, 30000);
    requests[requests.length - 1].resolve(active());
    await flushPromises();
    jest.advanceTimersByTime(1999);
    expect(fetchLatest).toHaveBeenCalledTimes(6);
    jest.advanceTimersByTime(1);
    expect(fetchLatest).toHaveBeenCalledTimes(7);
  });

  it('ignores a late previous success after a newer refresh was accepted', async () => {
    const {
      poller, onData, onError, requests,
    } = setup();
    pollers.push(poller);
    poller.start();
    poller.refresh();
    const newer = active(40);
    requests[1].resolve(newer);
    await flushPromises();
    requests[0].resolve(active(80));
    await flushPromises();

    expect(onData).toHaveBeenCalledTimes(1);
    expect(onData).toHaveBeenCalledWith(newer);
    expect(onError).not.toHaveBeenCalled();
  });

  it('ignores a late previous rejection after a newer refresh was accepted', async () => {
    const {
      poller, onData, onError, fetchLatest, requests,
    } = setup();
    pollers.push(poller);
    poller.start();
    poller.refresh();
    requests[1].resolve(active(40));
    await flushPromises();
    requests[0].reject(new Error('late network'));
    await flushPromises();

    expect(onData).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
    jest.advanceTimersByTime(2000);
    expect(fetchLatest).toHaveBeenCalledTimes(3);
  });

  it('blocks stale success across stop then restart epochs', async () => {
    const {
      poller, onData, onError, requests,
    } = setup();
    pollers.push(poller);
    poller.start();
    const oldSuccess = requests[0];
    poller.stop();
    poller.start();
    oldSuccess.resolve(active(80));
    await flushPromises();
    expect(onData).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();

    requests[1].resolve(active(40));
    await flushPromises();
    expect(onData).toHaveBeenCalledWith(active(40));
    expect(onError).not.toHaveBeenCalled();
  });

  it('blocks stale rejection after the restarted lifecycle accepted newer data', async () => {
    const {
      poller, onData, onError, requests,
    } = setup();
    pollers.push(poller);
    poller.start();
    const oldRequest = requests[0];
    poller.stop();
    poller.start();
    requests[1].resolve(active(40));
    await flushPromises();
    oldRequest.reject(new Error('ignored'));
    await flushPromises();

    expect(onData).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it('starts a restarted lifecycle backoff from two seconds', async () => {
    const { poller, fetchLatest, requests } = setup();
    pollers.push(poller);
    poller.start();
    requests[0].reject(new Error('first'));
    await flushPromises();
    poller.stop();
    poller.start();
    requests[1].reject(new Error('new lifecycle'));
    await flushPromises();
    jest.advanceTimersByTime(1999);
    expect(fetchLatest).toHaveBeenCalledTimes(2);
    jest.advanceTimersByTime(1);
    expect(fetchLatest).toHaveBeenCalledTimes(3);
  });

  it('stops periodic polling after complete and restores it only after active manual data', async () => {
    await expectTerminalRecovery('complete', pollers);
  });

  it('stops periodic polling after failed and restores it only after active manual data', async () => {
    await expectTerminalRecovery('failed', pollers);
  });

  it('stops periodic polling after expired and restores it only after active manual data', async () => {
    await expectTerminalRecovery('expired', pollers);
  });

  it('continues terminal polling only when explicitly configured not to stop', async () => {
    const { poller, fetchLatest, requests } = setup({ stopOnTerminal: false });
    pollers.push(poller);
    poller.start();
    requests[0].resolve(terminal());
    await flushPromises();
    jest.advanceTimersByTime(2000);
    expect(fetchLatest).toHaveBeenCalledTimes(2);
  });

  it('uses visible refreshes, cleans up stop, and supports no document', async () => {
    const documentRef = fakeDocument();
    const { poller, fetchLatest, requests } = setup({ documentRef });
    pollers.push(poller);
    poller.start();
    requests[0].resolve(terminal());
    await flushPromises();
    documentRef.hidden = true;
    documentRef.dispatchVisibility();
    expect(fetchLatest).toHaveBeenCalledTimes(1);
    documentRef.hidden = false;
    documentRef.dispatchVisibility();
    expect(fetchLatest).toHaveBeenCalledTimes(2);
    poller.stop();
    jest.advanceTimersByTime(30000);
    documentRef.dispatchVisibility();
    expect(fetchLatest).toHaveBeenCalledTimes(2);
    expect(documentRef.removeEventListener).toHaveBeenCalledWith('visibilitychange', expect.any(Function));

    const ssr = setup();
    pollers.push(ssr.poller);
    ssr.poller.start();
    ssr.requests[0].resolve(active());
    await flushPromises();
    ssr.poller.refresh();
    ssr.poller.stop();
    expect(ssr.fetchLatest).toHaveBeenCalledTimes(2);
  });
});
