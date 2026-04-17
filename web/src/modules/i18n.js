/** @module i18n - Simple internationalization */
const _translations = {
  zh: {
    placeholder: '发送消息...',
    thinking: '分析请求中...',
    toolCall: '正在使用 {tool}...',
    connected: '已连接',
    reconnecting: '重新连接中...',
    connectedError: '连接错误',
  },
  en: {
    placeholder: 'Send a message...',
    thinking: 'Analyzing request...',
    toolCall: 'Using {tool}...',
    connected: 'Connected',
    reconnecting: 'Reconnecting...',
    connectedError: 'Connection error',
  },
};

let _locale = 'zh';

export function t(key, params = {}) {
  const dict = _translations[_locale] || _translations['en'];
  let text = dict[key] || key;
  for (const [k, v] of Object.entries(params)) {
    text = text.replace(`{${k}}`, v);
  }
  return text;
}

export function setLocale(locale) {
  _locale = locale;
}

export function getLocale() {
  return _locale;
}
