const listeners = new Map();
const eventBus = {
  on(event, handler) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(handler);
  },
  off(event, handler) {
    if (!handler) listeners.delete(event);
    else if (listeners.has(event)) listeners.get(event).delete(handler);
  },
  emit(event, ...args) {
    if (listeners.has(event)) [...listeners.get(event)].forEach(handler => handler(...args));
  },
};

export default {
  bus: eventBus,
  events: {
    refreshPin: 'refreshPin',
    refreshBoards: 'refreshBoards',
  },
};
