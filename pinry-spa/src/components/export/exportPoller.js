const ACTIVE_STATES = ['queued', 'snapshotting', 'archiving', 'verifying'];

const createExportPoller = ({
  fetchLatest,
  onData,
  onError,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  documentRef,
  stopOnTerminal = true,
}) => {
  let timer = null;
  let lifecycleEpoch = 0;
  let requestSequence = 0;
  let acceptedSequence = 0;
  let failureCount = 0;
  let periodicEnabled = true;
  let started = false;
  let visibilityListener = null;
  let refresh;

  function clearTimer() {
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  function isCurrent(epoch, sequence) {
    return started && epoch === lifecycleEpoch && sequence >= acceptedSequence;
  }

  function schedule(delay, epoch) {
    if (!started || epoch !== lifecycleEpoch || timer !== null) return;
    timer = setTimeoutFn(() => {
      timer = null;
      refresh();
    }, delay);
  }

  function stop() {
    lifecycleEpoch += 1;
    started = false;
    clearTimer();
    if (visibilityListener && documentRef && typeof documentRef.removeEventListener === 'function') {
      documentRef.removeEventListener('visibilitychange', visibilityListener);
    }
    visibilityListener = null;
  }

  function start() {
    if (started) stop();
    lifecycleEpoch += 1;
    requestSequence = 0;
    acceptedSequence = 0;
    failureCount = 0;
    periodicEnabled = true;
    started = true;
    if (documentRef && typeof documentRef.addEventListener === 'function') {
      visibilityListener = () => {
        if (!documentRef.hidden) refresh();
      };
      documentRef.addEventListener('visibilitychange', visibilityListener);
    }
    refresh();
  }

  refresh = function refreshLatest() {
    if (!started) return;
    clearTimer();
    const epoch = lifecycleEpoch;
    const sequence = requestSequence + 1;
    requestSequence = sequence;
    let request;
    try {
      request = fetchLatest();
    } catch (error) {
      request = Promise.reject(error);
    }
    Promise.resolve(request).then(
      (data) => {
        if (!isCurrent(epoch, sequence)) return;
        acceptedSequence = sequence;
        failureCount = 0;
        onData(data);
        if (ACTIVE_STATES.includes(data.state)) {
          periodicEnabled = true;
          schedule(2000, epoch);
        } else if (stopOnTerminal) {
          periodicEnabled = false;
        } else {
          periodicEnabled = true;
          schedule(2000, epoch);
        }
      },
      (error) => {
        if (!isCurrent(epoch, sequence)) return;
        acceptedSequence = sequence;
        onError(error);
        if (!periodicEnabled) return;
        failureCount += 1;
        schedule(Math.min(2000 * (2 ** (failureCount - 1)), 30000), epoch);
      },
    );
  };

  return { start, stop, refresh };
};

export { createExportPoller };
export default createExportPoller;
