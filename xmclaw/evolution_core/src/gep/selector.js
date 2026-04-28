'use strict';

/**
 * Gene 选择器 (xm-auto-evo 版)
 */

const { matchScore } = require('./gene');
const { loadGenes } = require('./store');

function rankGenes(signals) {
  if (!Array.isArray(signals) || signals.length === 0) return [];
  const genes = loadGenes();
  if (!Array.isArray(genes) || genes.length === 0) return [];
  return genes
    .map(gene => ({ gene, score: matchScore(gene, signals) }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score);
}

function selectGene(signals, options = {}) {
  if (!Array.isArray(signals) || signals.length === 0) return null;
  const minScore = typeof options.minScore === 'number' ? options.minScore : 0.3;
  const { preferCategory } = options;
  const genes = loadGenes();
  if (!Array.isArray(genes) || genes.length === 0) return null;

  const scored = genes
    .map(gene => {
      let score = matchScore(gene, signals);
      if (preferCategory && gene.category === preferCategory) score += 0.1;
      return { gene, score };
    })
    .filter(item => item.score >= minScore)
    .sort((a, b) => b.score - a.score);

  return scored.length > 0 ? scored[0].gene : null;
}

module.exports = { selectGene, rankGenes };
