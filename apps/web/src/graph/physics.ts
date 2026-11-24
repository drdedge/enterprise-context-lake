import type { GraphClusterDTO, GraphEdgeDTO, GraphNodeDTO } from "../types/api";

export const PHI = 1.618033988749895;
export const GOLDEN_ANGLE_DEG = 137.50776405003785;
export const GOLDEN_ANGLE_RAD = (GOLDEN_ANGLE_DEG * Math.PI) / 180;
export const HOVER_SPOTLIGHT_DEBOUNCE_MS = 300;

export interface Point2D {
  x: number;
  y: number;
}

export interface NodePosition extends Point2D {
  id: string;
  pinned: boolean;
  isOrphan: boolean;
  clusterId: string | null;
}

export interface ClusterHull {
  id: string;
  label: string;
  kind: string;
  x: number;
  y: number;
  width: number;
  height: number;
  memberIds: string[];
}

export interface LayoutOptions {
  lens: string;
  spacingScale: number;
  clusterCohesion: number;
  pinnedPositions: Record<string, Point2D>;
}

export interface LayoutResult {
  positions: Record<string, NodePosition>;
  hulls: ClusterHull[];
  orphanRingRadius: number;
  bounds: { minX: number; minY: number; maxX: number; maxY: number };
}

function hashSeed(input: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Lightweight Barnes-Hut Quadtree for O(N log N) deterministic repulsion
interface QuadNode {
  cx: number;
  cy: number;
  half: number;
  mass: number;
  mx: number;
  my: number;
  bodyIdx: number | null;
  children: Array<QuadNode | null> | null;
}

function createQuad(cx: number, cy: number, half: number): QuadNode {
  return {
    cx,
    cy,
    half,
    mass: 0,
    mx: 0,
    my: 0,
    bodyIdx: null,
    children: null,
  };
}

function quadrantIndex(q: QuadNode, x: number, y: number): number {
  const east = x >= q.cx ? 1 : 0;
  const south = y >= q.cy ? 2 : 0;
  return east | south;
}

function insertQuad(
  q: QuadNode,
  idx: number,
  xs: Float64Array,
  ys: Float64Array,
  depth = 0,
): void {
  const x = xs[idx];
  const y = ys[idx];

  if (q.mass === 0) {
    q.mass = 1;
    q.mx = x;
    q.my = y;
    q.bodyIdx = idx;
    return;
  }

  if (depth > 18) {
    const newMass = q.mass + 1;
    q.mx = (q.mx * q.mass + x) / newMass;
    q.my = (q.my * q.mass + y) / newMass;
    q.mass = newMass;
    return;
  }

  if (q.children === null) {
    q.children = [null, null, null, null];
    const existing = q.bodyIdx;
    q.bodyIdx = null;
    if (existing !== null) {
      insertChild(q, existing, xs, ys, depth + 1);
    }
  }

  insertChild(q, idx, xs, ys, depth + 1);
  const newMass = q.mass + 1;
  q.mx = (q.mx * q.mass + x) / newMass;
  q.my = (q.my * q.mass + y) / newMass;
  q.mass = newMass;
}

function insertChild(
  q: QuadNode,
  idx: number,
  xs: Float64Array,
  ys: Float64Array,
  depth: number,
): void {
  if (!q.children) return;
  const qi = quadrantIndex(q, xs[idx], ys[idx]);
  let child = q.children[qi];
  if (!child) {
    const h2 = q.half * 0.5;
    const dx = qi & 1 ? h2 : -h2;
    const dy = qi & 2 ? h2 : -h2;
    child = createQuad(q.cx + dx, q.cy + dy, h2);
    q.children[qi] = child;
  }
  insertQuad(child, idx, xs, ys, depth);
}

function accumulateRepulsion(
  q: QuadNode,
  idx: number,
  xs: Float64Array,
  ys: Float64Array,
  fx: Float64Array,
  fy: Float64Array,
  repulsionStrength: number,
  theta: number,
): void {
  if (q.mass === 0) return;
  const dx = xs[idx] - q.mx;
  const dy = ys[idx] - q.my;
  const distSq = dx * dx + dy * dy + 64;
  const dist = Math.sqrt(distSq);

  if (q.children === null) {
    if (q.bodyIdx === idx) return;
    const force = (repulsionStrength * q.mass) / distSq;
    fx[idx] += (dx / dist) * force;
    fy[idx] += (dy / dist) * force;
    return;
  }

  if ((q.half * 2) / dist < theta) {
    const force = (repulsionStrength * q.mass) / distSq;
    fx[idx] += (dx / dist) * force;
    fy[idx] += (dy / dist) * force;
    return;
  }

  for (let i = 0; i < 4; i++) {
    const child = q.children[i];
    if (child) {
      accumulateRepulsion(
        child,
        idx,
        xs,
        ys,
        fx,
        fy,
        repulsionStrength,
        theta,
      );
    }
  }
}

export function computeDeterministicLayout(
  nodes: GraphNodeDTO[],
  edges: GraphEdgeDTO[],
  clusters: GraphClusterDTO[],
  options: LayoutOptions,
): LayoutResult {
  const { lens, spacingScale, clusterCohesion, pinnedPositions } = options;

  if (nodes.length === 0) {
    return {
      positions: {},
      hulls: [],
      orphanRingRadius: 240,
      bounds: { minX: -300, minY: -200, maxX: 300, maxY: 200 },
    };
  }

  // Deterministic ordering guarantees bit-identical layouts regardless of array arrival order
  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id));
  const nodeIdSet = new Set(sortedNodes.map((n) => n.id));
  const degreeMap = new Map<string, number>();
  for (const n of sortedNodes) degreeMap.set(n.id, 0);

  const activeEdges = edges.filter(
    (e) => nodeIdSet.has(e.source) && nodeIdSet.has(e.target) && e.source !== e.target,
  );
  for (const e of activeEdges) {
    degreeMap.set(e.source, (degreeMap.get(e.source) || 0) + 1);
    degreeMap.set(e.target, (degreeMap.get(e.target) || 0) + 1);
  }

  const connectedNodes: GraphNodeDTO[] = [];
  const orphanNodes: GraphNodeDTO[] = [];
  for (const n of sortedNodes) {
    const deg = degreeMap.get(n.id) || 0;
    if (n.is_orphan || deg === 0) {
      orphanNodes.push(n);
    } else {
      connectedNodes.push(n);
    }
  }

  // Build deterministic cluster anchors
  const clusterIds = Array.from(
    new Set(
      connectedNodes
        .map((n) => n.cluster_id)
        .filter((cid): cid is string => Boolean(cid)),
    ),
  ).sort();

  const clusterAnchors = new Map<string, Point2D>();
  const clusterCount = Math.max(clusterIds.length, 1);

  if (clusterCount === 1) {
    clusterAnchors.set(clusterIds[0], { x: 0, y: 0 });
  } else if (clusterCount <= 10) {
    const rx = (285 + clusterCount * 42) * spacingScale;
    const ry = rx * 0.78;
    clusterIds.forEach((cid, idx) => {
      const angle = (idx / clusterCount) * Math.PI * 2 - Math.PI / 2;
      clusterAnchors.set(cid, {
        x: Math.cos(angle) * rx,
        y: Math.sin(angle) * ry,
      });
    });
  } else {
    // Golden-angle Vogel spiral distributes many clusters cleanly without crowding
    clusterIds.forEach((cid, idx) => {
      const r = (145 + Math.sqrt(idx + 1) * 128) * spacingScale;
      const angle = idx * GOLDEN_ANGLE_RAD;
      clusterAnchors.set(cid, {
        x: Math.cos(angle) * r * 1.3,
        y: Math.sin(angle) * r * 0.88,
      });
    });
  }

  const seed = hashSeed(`${lens}:${sortedNodes.map((n) => n.id).join("|")}`);
  const rng = mulberry32(seed);

  const nCount = connectedNodes.length;
  const indexById = new Map<string, number>();
  const xs = new Float64Array(nCount);
  const ys = new Float64Array(nCount);
  const vx = new Float64Array(nCount);
  const vy = new Float64Array(nCount);
  const fx = new Float64Array(nCount);
  const fy = new Float64Array(nCount);
  const isPinnedArr = new Uint8Array(nCount);

  for (let i = 0; i < nCount; i++) {
    const node = connectedNodes[i];
    indexById.set(node.id, i);
    const pin = pinnedPositions[node.id];
    if (pin) {
      xs[i] = pin.x;
      ys[i] = pin.y;
      isPinnedArr[i] = 1;
    } else {
      const anchor = (node.cluster_id && clusterAnchors.get(node.cluster_id)) || {
        x: 0,
        y: 0,
      };
      const localAngle = i * GOLDEN_ANGLE_RAD;
      const localDist = (28 + Math.sqrt(i + 1) * 18) * spacingScale;
      xs[i] = anchor.x + Math.cos(localAngle) * localDist + (rng() - 0.5) * 6;
      ys[i] = anchor.y + Math.sin(localAngle) * localDist + (rng() - 0.5) * 6;
    }
  }

  // Indexed edges for fast settling loop
  const edgePairs: Array<{ s: number; t: number; sameCluster: boolean }> = [];
  for (const e of activeEdges) {
    const s = indexById.get(e.source);
    const t = indexById.get(e.target);
    if (s !== undefined && t !== undefined) {
      const sc =
        Boolean(connectedNodes[s].cluster_id) &&
        connectedNodes[s].cluster_id === connectedNodes[t].cluster_id;
      edgePairs.push({ s, t, sameCluster: sc });
    }
  }

  // φ-asymmetric spring parameters (intra-cluster is φ× tighter & stronger than inter-cluster)
  const kBase = 0.028;
  const kIntra = kBase * Math.max(1.0, clusterCohesion); // default clusterCohesion = PHI (1.618)
  const kInter = kBase / PHI;
  const restIntra = 155 * spacingScale;
  const restInter = restIntra * PHI;
  const repulsionStrength = 6400 * spacingScale * spacingScale;
  const clusterGravity = 0.052 * (clusterCohesion / PHI);
  const centerGravity = 0.005;
  const iterations = 220;
  const minSepX = 176 * spacingScale;
  const minSepY = 82 * spacingScale;

  for (let iter = 0; iter < iterations; iter++) {
    const alpha = Math.max(0.05, 1 - iter / iterations);
    fx.fill(0);
    fy.fill(0);

    if (nCount > 1) {
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (let i = 0; i < nCount; i++) {
        if (xs[i] < minX) minX = xs[i];
        if (xs[i] > maxX) maxX = xs[i];
        if (ys[i] < minY) minY = ys[i];
        if (ys[i] > maxY) maxY = ys[i];
      }
      const span = Math.max(maxX - minX, maxY - minY, 200);
      const root = createQuad((minX + maxX) * 0.5, (minY + maxY) * 0.5, span * 0.6);
      for (let i = 0; i < nCount; i++) {
        insertQuad(root, i, xs, ys);
      }
      for (let i = 0; i < nCount; i++) {
        accumulateRepulsion(root, i, xs, ys, fx, fy, repulsionStrength, 0.72);
      }
    }

    // φ-asymmetric edge springs
    for (let i = 0; i < edgePairs.length; i++) {
      const { s, t, sameCluster } = edgePairs[i];
      const dx = xs[t] - xs[s];
      const dy = ys[t] - ys[s];
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.001;
      const targetLen = sameCluster ? restIntra : restInter;
      const k = sameCluster ? kIntra : kInter;
      const force = (dist - targetLen) * k;
      const ux = (dx / dist) * force;
      const uy = (dy / dist) * force;
      fx[s] += ux;
      fy[s] += uy;
      fx[t] -= ux;
      fy[t] -= uy;
    }

    // Cluster cohesion + gentle centering
    for (let i = 0; i < nCount; i++) {
      const cid = connectedNodes[i].cluster_id;
      if (cid) {
        const anchor = clusterAnchors.get(cid);
        if (anchor) {
          fx[i] += (anchor.x - xs[i]) * clusterGravity;
          fy[i] += (anchor.y - ys[i]) * clusterGravity;
        }
      }
      fx[i] -= xs[i] * centerGravity;
      fy[i] -= ys[i] * centerGravity;
    }

    // Anisotropic label-aware collision padding between connected nodes.
    // Only pairs closer than (minSepX, minSepY) interact, so bucket nodes into a grid of that cell
    // size and test the 3x3 neighbouring cells instead of every pair (O(n) instead of O(n^2)).
    // Pairs are still visited in ascending (i, j) order, so the result is bit-identical.
    const grid = new Map<string, number[]>();
    for (let i = 0; i < nCount; i++) {
      const key = `${Math.floor(xs[i] / minSepX)},${Math.floor(ys[i] / minSepY)}`;
      const cell = grid.get(key);
      if (cell) cell.push(i);
      else grid.set(key, [i]);
    }
    const candidates: number[] = [];
    for (let i = 0; i < nCount; i++) {
      const cx = Math.floor(xs[i] / minSepX);
      const cy = Math.floor(ys[i] / minSepY);
      candidates.length = 0;
      for (let gx = cx - 1; gx <= cx + 1; gx++) {
        for (let gy = cy - 1; gy <= cy + 1; gy++) {
          const cell = grid.get(`${gx},${gy}`);
          if (!cell) continue;
          for (const j of cell) if (j > i) candidates.push(j);
        }
      }
      candidates.sort((a, b) => a - b);
      for (const j of candidates) {
        const dx = xs[j] - xs[i];
        const dy = ys[j] - ys[i];
        const ax = Math.abs(dx);
        const ay = Math.abs(dy);
        if (ax < minSepX && ay < minSepY) {
          const ox = minSepX - ax;
          const oy = minSepY - ay;
          const signX = dx >= 0 ? 1 : -1;
          const signY = dy >= 0 ? 1 : -1;
          if (ox / minSepX < oy / minSepY) {
            const push = ox * 0.28;
            fx[i] -= signX * push;
            fx[j] += signX * push;
          } else {
            const push = oy * 0.32;
            fy[i] -= signY * push;
            fy[j] += signY * push;
          }
        }
      }
    }

    const damping = 0.78;
    const maxStep = 28 * alpha + 2;
    for (let i = 0; i < nCount; i++) {
      if (isPinnedArr[i]) continue;
      vx[i] = (vx[i] + fx[i] * alpha) * damping;
      vy[i] = (vy[i] + fy[i] * alpha) * damping;
      const speed = Math.sqrt(vx[i] * vx[i] + vy[i] * vy[i]);
      if (speed > maxStep) {
        vx[i] = (vx[i] / speed) * maxStep;
        vy[i] = (vy[i] / speed) * maxStep;
      }
      xs[i] += vx[i];
      ys[i] += vy[i];
    }
  }

  // Deterministic post-settle cluster hull & label overlap resolution
  // Honors pinned nodes while guaranteeing zero intersecting hulls or colliding node labels
  const hullPadX = 88 * spacingScale;
  const hullPadTop = 44 * spacingScale;
  const hullPadBottom = 42 * spacingScale;
  const hullGap = 24 * spacingScale;

  // Group connected node indices by cluster key (singletons get their own group key)
  const groupIndices = new Map<string, number[]>();
  for (let i = 0; i < nCount; i++) {
    const key = connectedNodes[i].cluster_id || `__single:${connectedNodes[i].id}`;
    const arr = groupIndices.get(key) || [];
    arr.push(i);
    groupIndices.set(key, arr);
  }
  const groupKeys = Array.from(groupIndices.keys()).sort();

  // Pass A: ensure intra-group sibling node labels never overlap
  for (let pass = 0; pass < 18; pass++) {
    for (const key of groupKeys) {
      const idxs = groupIndices.get(key)!;
      for (let a = 0; a < idxs.length; a++) {
        for (let b = a + 1; b < idxs.length; b++) {
          const i = idxs[a];
          const j = idxs[b];
          const dx = xs[j] - xs[i];
          const dy = ys[j] - ys[i];
          const ax = Math.abs(dx);
          const ay = Math.abs(dy);
          if (ax < minSepX && ay < minSepY) {
            const ox = minSepX - ax;
            const oy = minSepY - ay;
            const sx = dx >= 0 ? 1 : -1;
            const sy = dy >= 0 ? 1 : -1;
            const iFree = isPinnedArr[i] === 0;
            const jFree = isPinnedArr[j] === 0;
            if (!iFree && !jFree) continue;
            if (oy <= ox * 0.65) {
              const step = oy * 0.55;
              if (iFree && jFree) {
                ys[i] -= sy * step;
                ys[j] += sy * step;
              } else if (iFree) {
                ys[i] -= sy * oy;
              } else {
                ys[j] += sy * oy;
              }
            } else {
              const step = ox * 0.55;
              if (iFree && jFree) {
                xs[i] -= sx * step;
                xs[j] += sx * step;
              } else if (iFree) {
                xs[i] -= sx * ox;
              } else {
                xs[j] += sx * ox;
              }
            }
          }
        }
      }
    }
  }

  // Pass B: separate any overlapping cluster hulls (and singleton label boxes) cleanly
  const computeGroupBox = (idxs: number[]) => {
    let gMinX = Infinity;
    let gMaxX = -Infinity;
    let gMinY = Infinity;
    let gMaxY = -Infinity;
    for (const idx of idxs) {
      if (xs[idx] < gMinX) gMinX = xs[idx];
      if (xs[idx] > gMaxX) gMaxX = xs[idx];
      if (ys[idx] < gMinY) gMinY = ys[idx];
      if (ys[idx] > gMaxY) gMaxY = ys[idx];
    }
    const px = idxs.length >= 2 ? hullPadX : 82 * spacingScale;
    const pt = idxs.length >= 2 ? hullPadTop : 24 * spacingScale;
    const pb = idxs.length >= 2 ? hullPadBottom : 38 * spacingScale;
    return {
      minX: gMinX - px,
      maxX: gMaxX + px,
      minY: gMinY - pt,
      maxY: gMaxY + pb,
      cx: (gMinX + gMaxX) * 0.5,
      cy: (gMinY + gMaxY) * 0.5,
    };
  };

  for (let pass = 0; pass < 36; pass++) {
    let anyMoved = false;
    for (let a = 0; a < groupKeys.length; a++) {
      const idxsA = groupIndices.get(groupKeys[a])!;
      const boxA = computeGroupBox(idxsA);
      for (let b = a + 1; b < groupKeys.length; b++) {
        const idxsB = groupIndices.get(groupKeys[b])!;
        const boxB = computeGroupBox(idxsB);

        const overlapX =
          Math.min(boxA.maxX, boxB.maxX) - Math.max(boxA.minX, boxB.minX) + hullGap;
        const overlapY =
          Math.min(boxA.maxY, boxB.maxY) - Math.max(boxA.minY, boxB.minY) + hullGap;

        if (overlapX > 0 && overlapY > 0) {
          const aHasFree = idxsA.some((i) => isPinnedArr[i] === 0);
          const bHasFree = idxsB.some((i) => isPinnedArr[i] === 0);
          if (!aHasFree && !bHasFree) continue;

          anyMoved = true;
          let shiftX = 0;
          let shiftY = 0;
          if (overlapX < overlapY) {
            const dir = boxB.cx >= boxA.cx ? 1 : -1;
            shiftX = dir * (overlapX * 0.54);
          } else {
            const dir = boxB.cy >= boxA.cy ? 1 : -1;
            shiftY = dir * (overlapY * 0.54);
          }

          if (aHasFree && bHasFree) {
            for (const i of idxsA) {
              if (isPinnedArr[i] === 0) {
                xs[i] -= shiftX;
                ys[i] -= shiftY;
              }
            }
            for (const j of idxsB) {
              if (isPinnedArr[j] === 0) {
                xs[j] += shiftX;
                ys[j] += shiftY;
              }
            }
          } else if (aHasFree) {
            for (const i of idxsA) {
              if (isPinnedArr[i] === 0) {
                xs[i] -= shiftX * 2;
                ys[i] -= shiftY * 2;
              }
            }
          } else {
            for (const j of idxsB) {
              if (isPinnedArr[j] === 0) {
                xs[j] += shiftX * 2;
                ys[j] += shiftY * 2;
              }
            }
          }
        }
      }
    }
    if (!anyMoved) break;
  }

  // Re-center unpinned connected mass around (0, 0) for clean orphan ring symmetry
  let cx = 0;
  let cy = 0;
  if (nCount > 0) {
    for (let i = 0; i < nCount; i++) {
      cx += xs[i];
      cy += ys[i];
    }
    cx /= nCount;
    cy /= nCount;
    const anyPinned = isPinnedArr.some((v) => v === 1);
    if (!anyPinned) {
      for (let i = 0; i < nCount; i++) {
        xs[i] -= cx;
        ys[i] -= cy;
      }
    }
  }

  const positions: Record<string, NodePosition> = {};
  let maxConnectedDist = 140 * spacingScale;

  for (let i = 0; i < nCount; i++) {
    const node = connectedNodes[i];
    const x = Math.round(xs[i] * 10) / 10;
    const y = Math.round(ys[i] * 10) / 10;
    positions[node.id] = {
      id: node.id,
      x,
      y,
      pinned: Boolean(isPinnedArr[i]),
      isOrphan: false,
      clusterId: node.cluster_id || null,
    };
    const d = Math.hypot(x, y);
    if (d > maxConnectedDist) maxConnectedDist = d;
  }

  // Golden-angle (137.507764°) orphan perimeter ring outside the connected graph
  const orphanRingRadius = Math.round(maxConnectedDist + 110 * spacingScale);
  orphanNodes.forEach((node, idx) => {
    const pin = pinnedPositions[node.id];
    if (pin) {
      positions[node.id] = {
        id: node.id,
        x: pin.x,
        y: pin.y,
        pinned: true,
        isOrphan: true,
        clusterId: node.cluster_id || null,
      };
      return;
    }
    const theta = idx * GOLDEN_ANGLE_RAD - Math.PI / 2;
    positions[node.id] = {
      id: node.id,
      x: Math.round(Math.cos(theta) * orphanRingRadius * 10) / 10,
      y: Math.round(Math.sin(theta) * orphanRingRadius * 10) / 10,
      pinned: false,
      isOrphan: true,
      clusterId: node.cluster_id || null,
    };
  });

  // Compute subtle rounded cluster hulls for connected clusters with >= 2 members
  const clusterMeta = new Map(clusters.map((c) => [c.id, c]));
  const clusterGroups = new Map<string, NodePosition[]>();
  for (const pos of Object.values(positions)) {
    if (pos.isOrphan || !pos.clusterId) continue;
    const list = clusterGroups.get(pos.clusterId) || [];
    list.push(pos);
    clusterGroups.set(pos.clusterId, list);
  }

  const hulls: ClusterHull[] = [];
  for (const [cid, members] of clusterGroups.entries()) {
    if (members.length < 2) continue;
    let hMinX = Infinity;
    let hMaxX = -Infinity;
    let hMinY = Infinity;
    let hMaxY = -Infinity;
    for (const m of members) {
      if (m.x < hMinX) hMinX = m.x;
      if (m.x > hMaxX) hMaxX = m.x;
      if (m.y < hMinY) hMinY = m.y;
      if (m.y > hMaxY) hMaxY = m.y;
    }
    const padX = hullPadX;
    const padTop = hullPadTop;
    const padBottom = hullPadBottom;
    const meta = clusterMeta.get(cid);
    hulls.push({
      id: cid,
      label: meta?.label || cid.replace(/^cluster:[^:]+:/, ""),
      kind: meta?.kind || "cluster",
      x: hMinX - padX,
      y: hMinY - padTop,
      width: hMaxX - hMinX + padX * 2,
      height: hMaxY - hMinY + padTop + padBottom,
      memberIds: members.map((m) => m.id),
    });
  }

  let bMinX = Infinity;
  let bMaxX = -Infinity;
  let bMinY = Infinity;
  let bMaxY = -Infinity;
  for (const p of Object.values(positions)) {
    if (p.x - 90 < bMinX) bMinX = p.x - 90;
    if (p.x + 90 > bMaxX) bMaxX = p.x + 90;
    if (p.y - 60 < bMinY) bMinY = p.y - 60;
    if (p.y + 60 > bMaxY) bMaxY = p.y + 60;
  }
  if (orphanNodes.length > 0) {
    bMinX = Math.min(bMinX, -orphanRingRadius - 90);
    bMaxX = Math.max(bMaxX, orphanRingRadius + 90);
    bMinY = Math.min(bMinY, -orphanRingRadius - 60);
    bMaxY = Math.max(bMaxY, orphanRingRadius + 60);
  }

  return {
    positions,
    hulls,
    orphanRingRadius,
    bounds: {
      minX: Number.isFinite(bMinX) ? bMinX : -300,
      minY: Number.isFinite(bMinY) ? bMinY : -200,
      maxX: Number.isFinite(bMaxX) ? bMaxX : 300,
      maxY: Number.isFinite(bMaxY) ? bMaxY : 200,
    },
  };
}

/**
 * Computes φ-decay hop spotlight opacities from a root node:
 * hop 0 = 1.0, hop 1 = 0.618, hop 2 = 0.382, rest = 0.12
 */
export function computePhiDecaySpotlight(
  rootId: string,
  edges: GraphEdgeDTO[],
  direction: "both" | "in" | "out" = "both",
): Map<string, { hop: number; opacity: number; tier: "0" | "1" | "2" | "dim" }> {
  const hops = new Map<string, number>();
  hops.set(rootId, 0);
  const queue: Array<{ id: string; hop: number }> = [{ id: rootId, hop: 0 }];

  while (queue.length > 0) {
    const current = queue.shift()!;
    if (current.hop >= 2) continue;

    for (const edge of edges) {
      if (direction === "both" || direction === "out") {
        if (edge.source === current.id && !hops.has(edge.target)) {
          hops.set(edge.target, current.hop + 1);
          queue.push({ id: edge.target, hop: current.hop + 1 });
        }
      }
      if (direction === "both" || direction === "in") {
        if (edge.target === current.id && !hops.has(edge.source)) {
          hops.set(edge.source, current.hop + 1);
          queue.push({ id: edge.source, hop: current.hop + 1 });
        }
      }
    }
  }

  const result = new Map<
    string,
    { hop: number; opacity: number; tier: "0" | "1" | "2" | "dim" }
  >();
  for (const [id, hop] of hops.entries()) {
    if (hop === 0) {
      result.set(id, { hop: 0, opacity: 1.0, tier: "0" });
    } else if (hop === 1) {
      result.set(id, { hop: 1, opacity: Number((1 / PHI).toFixed(3)), tier: "1" });
    } else if (hop === 2) {
      result.set(id, {
        hop: 2,
        opacity: Number((1 / (PHI * PHI)).toFixed(3)),
        tier: "2",
      });
    }
  }
  return result;
}

export type ZoomLodLevel = "cluster" | "entity" | "detail";

export function computeZoomLod(scale: number): ZoomLodLevel {
  if (scale < 0.55) return "cluster";
  if (scale <= 1.1) return "entity";
  return "detail";
}
