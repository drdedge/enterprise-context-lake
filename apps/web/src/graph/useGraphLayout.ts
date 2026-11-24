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
  // The layout sorts nodes by id first, so node order does not change the result
  const nodePart = nodes
    .map((n) => `${n.id}\u0001${n.cluster_id ?? ""}\u0001${n.is_orphan ? 1 : 0}\u0001${n.label}`)
    .sort()
    .join("\u0002");
  const edgePart = edges.map((e) => `${e.source}\u0001${e.target}`).join("\u0002");
  const clusterPart = clusters.map((c) => `${c.id}\u0001${c.label}\u0001${c.kind}`).join("\u0002");
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

type Job = { key: string; request: Omit<LayoutRequest, "id">; listeners: Set<(key: string) => void> };

/**
 * One shared worker, one layout in flight at a time. If inputs change while it is busy (e.g. a pin
 * on every pointer move during a drag), only the newest request waits; older ones are dropped.
 * Every finished layout is cached, even if nobody is looking at it any more.
 */
class LayoutWorkerClient {
  private worker: Worker | null | undefined;
  private inFlight: { id: number; job: Job } | null = null;
  private queued: Job | null = null;
  private seq = 0;
  private subscribers = new Set<(key: string) => void>();

  available(): boolean {
    return this.getWorker() !== null;
  }

  subscribe(fn: (key: string) => void): () => void {
    this.subscribers.add(fn);
    return () => this.subscribers.delete(fn);
  }

  request(key: string, request: Omit<LayoutRequest, "id">): void {
    if (this.inFlight?.job.key === key || this.queued?.key === key) return;
    const job: Job = { key, request, listeners: new Set() };
    if (this.inFlight) {
      this.queued = job; // replaces any older queued request
      return;
    }
    this.start(job);
  }

  private getWorker(): Worker | null {
    if (this.worker !== undefined) return this.worker;
    try {
      if (typeof Worker === "undefined") {
        this.worker = null;
      } else {
        const w = new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" });
        w.addEventListener("message", (e: MessageEvent<LayoutResponse>) => this.onMessage(e.data));
        w.addEventListener("error", () => this.fail());
        w.addEventListener("messageerror", () => this.fail());
        this.worker = w;
      }
    } catch {
      this.worker = null;
    }
    return this.worker;
  }

  private start(job: Job): void {
    const w = this.getWorker();
    if (!w) {
      this.finish(job, computeDeterministicLayout(job.request.nodes, job.request.edges, job.request.clusters, job.request.options));
      return;
    }
    const id = ++this.seq;
    this.inFlight = { id, job };
    try {
      w.postMessage({ id, ...job.request } satisfies LayoutRequest);
    } catch {
      this.fail();
    }
  }

  private onMessage(data: LayoutResponse): void {
    const cur = this.inFlight;
    if (!cur || data.id !== cur.id) return;
    const { nodes, edges, clusters, options } = cur.job.request;
    this.finish(cur.job, data.result ?? computeDeterministicLayout(nodes, edges, clusters, options));
  }

  /** Worker failed to load or crashed: stop using it and finish pending work synchronously. */
  private fail(): void {
    try {
      this.worker?.terminate();
    } catch {
      /* ignore */
    }
    this.worker = null;
    const cur = this.inFlight?.job;
    this.inFlight = null;
    if (cur) this.finish(cur, computeDeterministicLayout(cur.request.nodes, cur.request.edges, cur.request.clusters, cur.request.options));
  }

  private finish(job: Job, result: LayoutResult): void {
    cachePut(job.key, result);
    this.inFlight = null;
    for (const fn of this.subscribers) fn(job.key);
    const next = this.queued;
    this.queued = null;
    if (next) {
      if (cacheGet(next.key)) {
        for (const fn of this.subscribers) fn(next.key);
      } else {
        this.start(next);
      }
    }
  }
}

const workerClient = new LayoutWorkerClient();

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
  const [, setTick] = useState(0);

  // Re-render when the worker finishes the layout we are waiting for
  useEffect(
    () =>
      workerClient.subscribe((doneKey) => {
        if (doneKey === key) setTick((t) => t + 1);
      }),
    [key],
  );

  let result = cacheGet(key);
  if (!result && (nodes.length <= SYNC_LAYOUT_MAX_NODES || !workerClient.available())) {
    result = computeDeterministicLayout(nodes, edges, clusters, options);
    cachePut(key, result);
  }

  useEffect(() => {
    if (!cacheGet(key)) workerClient.request(key, { nodes, edges, clusters, options });
    // key captures every input that affects the result
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const lastShown = useRef<LayoutResult>(EMPTY_LAYOUT);
  useEffect(() => {
    if (result) lastShown.current = result;
  });

  if (result) return { layout: result, pending: false };
  return { layout: withPins(lastShown.current, options.pinnedPositions), pending: true };
}
