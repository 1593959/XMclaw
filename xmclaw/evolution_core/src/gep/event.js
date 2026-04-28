'use strict';

/**
 * EvolutionEvent 日志 (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/gep/event.js
 */

const crypto = require('node:crypto');

/**
 * 创建 EvolutionEvent
 */
function createEvent(params) {
  if (!params.event_type) throw new Error('Event must have an event_type');
  return {
    type: 'EvolutionEvent',
    id: `evt_${crypto.randomUUID().slice(0, 8)}`,
    event_type: params.event_type,
    payload: params.payload || {},
    gene_id: params.gene_id || null,
    cycle_id: params.cycle_id || null,
    timestamp: new Date().toISOString(),
  };
}

/**
 * 过滤事件
 */
function filterEvents(events, filter) {
  return events.filter(e => {
    if (filter.event_type && e.event_type !== filter.event_type) return false;
    if (filter.gene_id && e.gene_id !== filter.gene_id) return false;
    if (filter.since && e.timestamp < filter.since) return false;
    return true;
  });
}

/**
 * 汇总事件统计
 */
function summarizeEvents(events) {
  const summary = {};
  for (const e of events) {
    summary[e.event_type] = (summary[e.event_type] || 0) + 1;
  }
  return summary;
}

module.exports = { createEvent, filterEvents, summarizeEvents };
