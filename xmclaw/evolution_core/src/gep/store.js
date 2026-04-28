'use strict';

/**
 * 资产持久化层 (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/gep/store.js
 * 
 * 管理 genes.json, capsules.json, events.jsonl 的读写。
 */

const fs = require('node:fs');
const path = require('node:path');

const DATA_DIR = process.env.WORKSPACE
  ? path.join(process.env.WORKSPACE, 'data')
  : path.join(__dirname, '..', '..', 'data');

function dataPath(filename) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  return path.join(DATA_DIR, filename);
}

function readJSON(filename) {
  const filepath = dataPath(filename);
  if (!fs.existsSync(filepath)) return filename.endsWith('.json') ? [] : {};
  const raw = fs.readFileSync(filepath, 'utf-8').trim();
  if (!raw) return filename.endsWith('.json') ? [] : {};
  try {
    return JSON.parse(raw);
  } catch {
    return filename.endsWith('.json') ? [] : {};
  }
}

function writeJSON(filename, data) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(dataPath(filename), JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

function appendJSONL(filename, record) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.appendFileSync(dataPath(filename), JSON.stringify(record) + '\n', 'utf-8');
}

function readJSONL(filename) {
  const filepath = dataPath(filename);
  if (!fs.existsSync(filepath)) return [];
  const raw = fs.readFileSync(filepath, 'utf-8').trim();
  if (!raw) return [];
  return raw.split('\n').filter(Boolean).map(line => {
    try { return JSON.parse(line); } catch { return null; }
  }).filter(Boolean);
}

// --- Gene CRUD ---

function loadGenes() { return readJSON('genes.json'); }
function saveGenes(genes) { writeJSON('genes.json', genes); }
function addGene(gene) {
  const genes = loadGenes();
  const existing = genes.findIndex(g => g.id === gene.id);
  if (existing >= 0) genes[existing] = gene;
  else genes.push(gene);
  saveGenes(genes);
}
function findGene(id) { return loadGenes().find(g => g.id === id) || null; }
function removeGene(id) {
  const genes = loadGenes();
  const filtered = genes.filter(g => g.id !== id);
  if (filtered.length === genes.length) return false;
  saveGenes(filtered);
  return true;
}

// --- Capsule CRUD ---

function loadCapsules() { return readJSON('capsules.json'); }
function saveCapsules(capsules) { writeJSON('capsules.json', capsules); }
function addCapsule(capsule) {
  const capsules = loadCapsules();
  capsules.push(capsule);
  saveCapsules(capsules);
}

// --- Event ---

function appendEvent(event) { appendJSONL('events.jsonl', event); }
function loadEvents() { return readJSONL('events.jsonl'); }

// --- Patterns (auto-evo specific) ---

function loadPatterns() { return readJSON('patterns.json'); }
function savePatterns(patterns) { writeJSON('patterns.json', patterns); }

module.exports = {
  loadGenes, saveGenes, addGene, findGene, removeGene,
  loadCapsules, saveCapsules, addCapsule,
  appendEvent, loadEvents,
  loadPatterns, savePatterns,
};
