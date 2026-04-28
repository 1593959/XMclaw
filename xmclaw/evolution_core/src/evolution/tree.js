'use strict';

/**
 * 能力树 (xm-auto-evo 版)
 *
 * 移植自 xm-evo/src/tree/capability_tree.js
 * 整合了 tree/node.js 的节点操作
 *
 * 管理层级化的能力图谱，支持节点的增删改查、合并、修剪和信号路径查找。
 */

const fs = require('node:fs');
const path = require('node:path');
const { createNode, validateNode, touchNode } = require('../tree/node');

const _TREE_DATA_DIR = process.env.WORKSPACE
  ? path.join(process.env.WORKSPACE, 'data')
  : path.join(__dirname, '..', '..', 'data');
const TREE_FILE = path.join(_TREE_DATA_DIR, 'capability_tree.json');

function loadTreeData() {
  try {
    if (fs.existsSync(TREE_FILE)) {
      const raw = fs.readFileSync(TREE_FILE, 'utf-8').trim();
      if (raw) return JSON.parse(raw);
    }
  } catch {}
  return {
    root: { id: 'cap', name: 'Root', level: 'high', parent_id: null, children: [] },
    nodes: {},
  };
}

function saveTreeData(data) {
  const dir = path.dirname(TREE_FILE);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(TREE_FILE, JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

class CapabilityTree {
  constructor() {
    this.data = loadTreeData();
    if (!this.data.root) {
      this.data.root = { id: 'cap', name: 'Root', level: 'high', parent_id: null, children: [] };
    }
    if (!this.data.nodes) this.data.nodes = {};
  }

  /**
   * 添加新节点（使用 createNode 验证结构）。
   */
  addNode(node) {
    const validation = validateNode(node);
    if (!validation.valid) throw new Error(`Invalid node: ${validation.errors.join(', ')}`);
    if (this.data.nodes[node.id]) throw new Error(`Node already exists: ${node.id}`);

    const parentId = node.parent_id;
    if (parentId === this.data.root.id) {
      this.data.root.children.push(node.id);
    } else if (this.data.nodes[parentId]) {
      if (!Array.isArray(this.data.nodes[parentId].children)) {
        this.data.nodes[parentId].children = [];
      }
      this.data.nodes[parentId].children.push(node.id);
    } else {
      throw new Error(`Parent node not found: ${parentId}`);
    }

    this.data.nodes[node.id] = node;
    this.save();
  }

  /**
   * 移除节点及其所有子节点。
   */
  removeNode(id) {
    const node = this.data.nodes[id];
    if (!node) return false;

    const childIds = Array.isArray(node.children) ? [...node.children] : [];
    for (const childId of childIds) this.removeNode(childId);

    const parentId = node.parent_id;
    if (parentId === this.data.root.id) {
      this.data.root.children = this.data.root.children.filter(cid => cid !== id);
    } else if (this.data.nodes[parentId]) {
      this.data.nodes[parentId].children = this.data.nodes[parentId].children.filter(cid => cid !== id);
    }

    delete this.data.nodes[id];
    this.save();
    return true;
  }

  getNode(id) {
    if (id === this.data.root.id) return this.data.root;
    return this.data.nodes[id] || null;
  }

  /**
   * 保存树数据到文件。
   */
  save() {
    saveTreeData(this.data);
  }

  /**
   * 更新节点。
   */
  updateNode(id, updates) {
    if (!this.data.nodes[id]) throw new Error(`Node not found: ${id}`);
    this.data.nodes[id] = { ...this.data.nodes[id], ...updates, id };
    this.save();
  }

  getChildren(id) {
    const node = this.getNode(id);
    if (!node) return [];
    if (!Array.isArray(node.children)) return [];
    return node.children.map(cid => this.getNode(cid)).filter(Boolean);
  }

  /**
   * 获取所有活跃节点（status !== 'pruned'）。
   */
  getActiveNodes() {
    return Object.values(this.data.nodes).filter(n => n.status !== 'pruned');
  }

  /**
   * 获取所有节点。
   */
  getAllNodes() {
    return Object.values(this.data.nodes);
  }

  /**
   * 触发节点（更新触发计数和时间）。
   */
  triggerNode(id) {
    const node = this.data.nodes[id];
    if (!node) return null;
    const updated = touchNode(node);
    this.data.nodes[id] = updated;
    this.save();
    return updated;
  }

  /**
   * 合并两个节点（B 合并到 A，A 保留，B 删除）。
   */
  mergeNodes(idA, idB) {
    const nodeA = this.data.nodes[idA];
    const nodeB = this.data.nodes[idB];
    if (!nodeA || !nodeB) return false;

    // 合并 linked_genes 和 linked_skills
    const mergedGenes = [...new Set([...(nodeA.linked_genes || []), ...(nodeB.linked_genes || [])])];
    const mergedSkills = [...new Set([...(nodeA.linked_skills || []), ...(nodeB.linked_skills || [])])];

    this.updateNode(idA, {
      linked_genes: mergedGenes,
      linked_skills: mergedSkills,
      trigger_count: (nodeA.trigger_count || 0) + (nodeB.trigger_count || 0),
    });

    // B 的子节点挂到 A
    const childIds = Array.isArray(nodeB.children) ? [...nodeB.children] : [];
    for (const childId of childIds) {
      this.data.nodes[childId] = { ...this.data.nodes[childId], parent_id: idA };
      if (!Array.isArray(this.data.nodes[idA].children)) this.data.nodes[idA].children = [];
      this.data.nodes[idA].children.push(childId);
    }

    this.removeNode(idB);
    return true;
  }

  /**
   * 根据信号查找相关节点。
   */
  findPath(signal) {
    const results = [];
    for (const node of Object.values(this.data.nodes)) {
      const inGenes = (node.linked_genes || []).some(g => g.includes(signal));
      const inSkills = (node.linked_skills || []).some(s => s.includes(signal));
      if (inGenes || inSkills) {
        results.push({ id: node.id, name: node.name, match: inGenes ? 'gene' : 'skill' });
      }
    }
    return results;
  }

  /**
   * 标记节点为"生长中"（新能力正在发育）。
   */
  growNode(id) {
    const node = this.data.nodes[id];
    if (!node) return false;
    this.updateNode(id, { status: 'candidate', last_triggered: new Date().toISOString() });
    return true;
  }

  /**
   * 修剪长期不活跃的节点。
   */
  pruneStale(maxAgeDays = 60) {
    const cutoff = Date.now() - maxAgeDays * 24 * 60 * 60 * 1000;
    let pruned = 0;
    for (const [id, node] of Object.entries(this.data.nodes)) {
      if (node.last_triggered && new Date(node.last_triggered).getTime() < cutoff) {
        if (this.removeNode(id)) pruned++;
      }
    }
    return pruned;
  }

  save() {
    saveTreeData(this.data);
  }

  getStats() {
    const nodes = Object.values(this.data.nodes);
    const byStatus = {};
    for (const n of nodes) {
      byStatus[n.status || 'unknown'] = (byStatus[n.status || 'unknown'] || 0) + 1;
    }
    return {
      totalNodes: nodes.length,
      rootChildren: this.data.root.children.length,
      byStatus,
    };
  }
}

module.exports = { CapabilityTree };
