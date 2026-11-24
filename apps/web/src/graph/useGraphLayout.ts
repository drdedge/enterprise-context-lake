import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphClusterDTO, GraphEdgeDTO, GraphNodeDTO } from "../types/api";
import { computeDeterministicLayout, type LayoutOptions, type LayoutResult } from "./physics";

export interface LayoutRequest {
  id: number;
  nodes: GraphNodeDTO[];
  edges: GraphEdgeDTO[];
  clusters: GraphClusterDTO[];
  options: LayoutOptions;
}

export interface LayoutResponse {
  id: number;
  result?: LayoutResult;
  error?: string;
}

/** Graphs at or below this many nodes are laid out synchronously (a few tens of ms at most). */
export const SYNC_LAYOUT_MAX_NODES = 150;
const CACHE_LIMIT = 12;

const EMPTY_LAYOUT: LayoutResult = computeDeterministicLayout([], [], [], {
  lens: "",
  spacingScale: 1,
  clusterCohesion: 1,
  pinnedPositions: {},
});

/**
 * Cache key for a layout input. The layout is a pure function of these values, so two equal keys
 * always give bit-identical layouts and a cached result can be reused (e.g. switching lens back).
 */
export function layoutCacheKey(
  nodes: GraphNodeDTO[],
  edges: GraphEdgeDTO[],
  clusters: GraphClusterDTO[],
  options: LayoutOptions,
): string {
  const nodePart = nodes.map((n) => `${n.id}\u0001${n.cluster_id ?? ""}\u0001${n.is_orphan ? 1 : 0}`).join("\u0002");
  const edgePart = edges.map((e) => `${e.source}\u0001${e.target}`).join("\u0002");
  const clusterPart = clusters.map((c) => c.id).join("\u0002");
  const pins = Object.keys(options.pinnedPositions)
    .sort()
    .map((k) => `${k}:${options.pinnedPositions[k].x},${options.pinnedPositions[k].y}`)
    .join("\u0002");
  return [options.lens, options.spacingScale, options.clusterCohesion, pins, nodePart, edgePart, clusterPart].join("\u0003");
}

const cache = new Map<string, LayoutResult>();

function cacheGet(key: string): LayoutResult | undefined {
  const hit = cache.get(key);
  if (hit) {
    cache.delete(key);
    cache.set(key, hit); // LRU: mark as most recently used
  }
  return hit;
}

function cachePut(key: string, value: LayoutResult): void {
  cache.set(key, value);
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

let sharedWorker: Worker | null | undefined;

function getWorker(): Worker | null {
  if (sharedWorker !== undefined) return sharedWorker;
  try {
    sharedWorker =
      typeof Worker === "undefined"
        ? null
        : new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" });
  } catch {
    sharedWorker = null;
  }
  return sharedWorker;
}

/** While a new layout is computing, show the previous one with any new pins applied on top. */
function withPins(layout: LayoutResult, pins: LayoutOptions["pinnedPositions"]): LayoutResult {
  const ids = Object.keys(pins);
  if (ids.length === 0) return layout;
  const positions = { ...layout.positions };
  for (const id of ids) {
    const cur = positions[id];
    if (cur) positions[id] = { ...cur, x: pins[id].x, y: pins[id].y, pinned: true };
  }
  return { ...layout, positions };
}

/**
 * Deterministic graph layout that stays off the main thread for large graphs.
 *
 * Small graphs are laid out synchronously. Larger ones go to a Web Worker; until the result
 * arrives the previous layout stays on screen (`pending` is true). Results are cached per input,
 * so returning to a lens or filter that was already laid out is instant. If workers are not
 * available, it falls back to the synchronous computation.
 */
export function useGraphLayout(
  nodes: GraphNodeDTO[],
  edges: GraphEdgeDTO[],
  clusters: GraphClusterDTO[],
  options: LayoutOptions,
): { layout: LayoutResult; pending: boolean } {
  const key = useMemo(() => layoutCacheKey(nodes, edges, clusters, options), [nodes, edges, clusters, options]);

  const syncResult = useMemo(() => {
    const cached = cacheGet(key);
    if (cached) return cached;
    if (nodes.length <= SYNC_LAYOUT_MAX_NODES || getWorker() === null) {
      const result = computeDeterministicLayout(nodes, edges, clusters, options);
      cachePut(key, result);
      return result;
    }
    return null;
    // key captures every input that affects the result
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const [asyncResult, setAsyncResult] = useState<{ key: string; result: LayoutResult } | null>(null);
  const lastShown = useRef<LayoutResult>(EMPTY_LAYOUT);
  const requestSeq = useRef(0);

  useEffect(() => {
    if (syncResult) return;
    const worker = getWorker();
    if (!worker) return;
    const id = ++requestSeq.current;
    const onMessage = (event: MessageEvent<LayoutResponse>) => {
      if (event.data.id !== id) return;
      worker.removeEventListener("message", onMessage);
      const result = event.data.result ?? computeDeterministicLayout(nodes, edges, clusters, options);
      cachePut(key, result);
      setAsyncResult({ key, result });
    };
    worker.addEventListener("message", onMessage);
    const request: LayoutRequest = { id, nodes, edges, clusters, options };
    worker.postMessage(request);
    return () => worker.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, syncResult]);

  let layout: LayoutResult;
  let pending = false;
  if (syncResult) {
    layout = syncResult;
  } else if (asyncResult && asyncResult.key === key) {
    layout = asyncResult.result;
  } else {
    layout = withPins(lastShown.current, options.pinnedPositions);
    pending = true;
  }
  if (!pending) lastShown.current = layout;
  return { layout, pending };
}
