/** @module state - Centralized reactive state management */
const _state = {
  currentView: 'dashboard',
  currentAgentId: 'default',
  sessions: [],
  currentSessionId: null,
  todos: [],
  tasks: [],
  isStreaming: false,
  planMode: false,
  totalCost: 0,
  totalTokens: 0,
  geneCount: 0,
  skillCount: 0,
  toolHistory: [],
  selfModHistory: [],
  todosList: [],
  currentMessageEl: null,
  currentFilePath: null,
  workspaceFiles: [],
};

export function getState(key) {
  return key ? _state[key] : { ..._state };
}

export function setState(key, value) {
  _state[key] = value;
  notify(key);
}

export function subscribe(key, callback) {
  if (!_state._subscribers) _state._subscribers = {};
  if (!_state._subscribers[key]) _state._subscribers[key] = [];
  _state._subscribers[key].push(callback);
  return () => {
    _state._subscribers[key] = _state._subscribers[key].filter(cb => cb !== callback);
  };
}

export function notify(key) {
  if (!_state._subscribers || !_state._subscribers[key]) return;
  _state._subscribers[key].forEach(cb => cb(_state[key]));
}

export function increment(key, delta = 1) {
  _state[key] = (_state[key] || 0) + delta;
  notify(key);
}

// Expose globally for modules that use window-level access
window._state = _state;
window.getState = getState;
window.setState = setState;
window.subscribe = subscribe;
window.notify = notify;
window.increment = increment;
