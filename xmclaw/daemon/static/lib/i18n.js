// XMclaw — i18n architecture (Phase F: hooks only, no full translation).
//
// Design: every UI string hardcoded in Chinese today. This module
// provides the *plumbing* so a future Phase can swap dictionaries
// without touching components. The t() call sites act as
// self-documenting keys.
//
// Usage in components:
//   import { t, useLocale } from "../lib/i18n.js";
//   const label = t("nav.settings"); // => "设置"
//
// Adding a language:
//   1. Add dictionary to DICTIONARIES below.
//   2. Add option to LanguageSwitcher.
//   3. Done — every t() call site picks it up automatically.

const STORAGE_KEY = "xmc_locale";
const DEFAULT_LOCALE = "zh_CN";

const DICTIONARIES = {
  zh_CN: {
    // Nav
    "nav.dashboard": "概览",
    "nav.chat": "对话",
    "nav.sessions": "会话",
    "nav.agents": "代理",
    "nav.channels": "通道",
    "nav.skills": "技能",
    "nav.marketplace": "技能商店",
    "nav.evolution": "进化",
    "nav.cognition": "认知",
    "nav.memory": "记忆",
    "nav.tools": "工具",
    "nav.security": "安全",
    "nav.settings": "设置",
    "nav.files": "文件",
    "nav.cron": "定时任务",
    "nav.workspace": "工作区",
    "nav.analytics": "分析",
    "nav.trace": "事件",
    "nav.logs": "日志",
    "nav.docs": "文档",
    "nav.config": "配置查看",
    // Common
    "common.loading": "加载中…",
    "common.error": "出错了",
    "common.empty": "暂无数据",
    "common.copy": "复制",
    "common.copied": "已复制",
    "common.refresh": "刷新",
    "common.close": "关闭",
    // Evoluion card (Dashboard)
    "evolution.title": "进化概览",
    "evolution.proposals": "今日提案",
    "evolution.promotes": "晋升",
    "evolution.rollbacks": "回滚",
    "evolution.gradeAvg": "评分均值",
    "evolution.viewDetails": "查看详情 →",
  },
  en: {
    // Nav
    "nav.dashboard": "Dashboard",
    "nav.chat": "Chat",
    "nav.sessions": "Sessions",
    "nav.agents": "Agents",
    "nav.channels": "Channels",
    "nav.skills": "Skills",
    "nav.marketplace": "Marketplace",
    "nav.evolution": "Evolution",
    "nav.cognition": "Cognition",
    "nav.memory": "Memory",
    "nav.tools": "Tools",
    "nav.security": "Security",
    "nav.settings": "Settings",
    "nav.files": "Files",
    "nav.cron": "Cron",
    "nav.workspace": "Workspace",
    "nav.analytics": "Analytics",
    "nav.trace": "Trace",
    "nav.logs": "Logs",
    "nav.docs": "Docs",
    "nav.config": "Config",
    // Common
    "common.loading": "Loading…",
    "common.error": "Error",
    "common.empty": "No data",
    "common.copy": "Copy",
    "common.copied": "Copied",
    "common.refresh": "Refresh",
    "common.close": "Close",
    // Evolution card (Dashboard)
    "evolution.title": "Evolution",
    "evolution.proposals": "Proposals",
    "evolution.promotes": "Promotes",
    "evolution.rollbacks": "Rollbacks",
    "evolution.gradeAvg": "Avg Grade",
    "evolution.viewDetails": "View details →",
  },
};

function _readLocale() {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_LOCALE;
  } catch {
    return DEFAULT_LOCALE;
  }
}

function _writeLocale(v) {
  try {
    localStorage.setItem(STORAGE_KEY, v);
  } catch {
    // ignore
  }
}

/** Translate a key into the current locale. Falls back to the key itself. */
export function t(key, fallback) {
  const locale = _readLocale();
  const dict = DICTIONARIES[locale] || DICTIONARIES[DEFAULT_LOCALE];
  const value = dict[key];
  if (value != null) return value;
  return fallback != null ? fallback : key;
}

/** Current locale string, e.g. "zh_CN" or "en". */
export function getLocale() {
  return _readLocale();
}

/** Switch locale persistently. */
export function setLocale(v) {
  if (DICTIONARIES[v]) {
    _writeLocale(v);
    // Notify subscribers
    _listeners.forEach((fn) => fn(v));
  }
}

/** List available locale codes. */
export function listLocales() {
  return Object.keys(DICTIONARIES);
}

const _listeners = new Set();

/** Subscribe to locale changes (returns unsubscribe function). */
export function onLocaleChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

/** Preact hook: re-renders when locale changes. */
export function useLocale() {
  const { useState, useEffect } = window.__xmc.preact_hooks;
  const [locale, setLocaleState] = useState(_readLocale);
  useEffect(() => {
    const unsub = onLocaleChange((v) => setLocaleState(v));
    return unsub;
  }, []);
  return { locale, setLocale: (v) => setLocale(v), listLocales: listLocales() };
}
