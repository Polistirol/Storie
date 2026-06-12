/**
 * Assegna coordinate da CSV ai nodi del grafo.
 * Ordine: sequenza in nodes.json; le Era restano da positions.json.
 */
(function (root) {
  const DEFAULT_ERA_IDS = ['infanzia', 'gioventu', 'adultita', 'vecchiaia'];

  /** @returns {{ x: number, y: number }[]} */
  function parsePointsCsv(text) {
    const points = [];
    const lines = String(text).trim().split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) continue;
      if (i === 0 && /^x\s*,\s*y/i.test(line.replace(/\s/g, ''))) continue;
      const comma = line.indexOf(',');
      if (comma < 0) continue;
      const x = parseFloat(line.slice(0, comma));
      const y = parseFloat(line.slice(comma + 1));
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      points.push({ x, y });
    }
    return points;
  }

  function isEraNode(node, eraIds) {
    return eraIds.includes(node.id) || node.type === 'Era';
  }

  /**
   * @param {{
   *   nodes: { id: string, type?: string }[],
   *   points: { x: number, y: number }[],
   *   eraIds?: string[],
   *   eraPositions?: Record<string, { x: number, y: number, fixed?: boolean }>,
   *   skipNodeIds?: Set<string> | string[],
   *   fixed?: boolean
   * }} options
   */
  function assignPointsToNodes(options) {
    const {
      nodes,
      points,
      eraIds = DEFAULT_ERA_IDS,
      eraPositions = {},
      skipNodeIds = new Set(['adriano']),
      fixed = true
    } = options;

    const skip = skipNodeIds instanceof Set ? skipNodeIds : new Set(skipNodeIds);
    const layoutById = {};

    for (const eraId of eraIds) {
      const pos = eraPositions[eraId];
      if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
        layoutById[eraId] = {
          x: pos.x,
          y: pos.y,
          fixed: pos.fixed !== false
        };
      }
    }

    let pointIdx = 0;
    let assigned = 0;
    let skippedEras = 0;
    let skippedExcluded = 0;

    for (const node of nodes) {
      if (skip.has(node.id)) {
        skippedExcluded++;
        continue;
      }
      if (isEraNode(node, eraIds)) {
        skippedEras++;
        continue;
      }
      if (pointIdx >= points.length) break;

      const pt = points[pointIdx++];
      layoutById[node.id] = { x: pt.x, y: pt.y, fixed };
      assigned++;
    }

    return {
      layoutById,
      assigned,
      pointsUsed: pointIdx,
      pointsTotal: points.length,
      skippedEras,
      skippedExcluded,
      nodesEligible: nodes.filter(n => !skip.has(n.id) && !isEraNode(n, eraIds)).length
    };
  }

  root.PointAssign = {
    DEFAULT_ERA_IDS,
    parsePointsCsv,
    assignPointsToNodes
  };
})(typeof globalThis !== 'undefined' ? globalThis : window);
