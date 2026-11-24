import { describe, expect, it } from "vitest";
import { layoutCacheKey } from "./useGraphLayout";
import { computeDeterministicLayout, type LayoutOptions } from "./physics";
import type { GraphClusterDTO, GraphEdgeDTO, GraphNodeDTO } from "../types/api";

function node(id: string, cluster: string | null, label = id): GraphNodeDTO {
  return {
    id,
    canonical_key: id,
    label,
    entity_type: "legal_vehicle",
    cluster_id: cluster,
    wall_side: "public_side",
    degree_in: 0,
    degree_out: 0,
    is_orphan: false,
    has_conflict: false,
    open_conflict_count: 0,
    topics: [],
  } as GraphNodeDTO;
}

const clusters = [{ id: "c1", label: "One", kind: "topic" }] as unknown as GraphClusterDTO[];
const edges = [{ source: "a", target: "b" }] as unknown as GraphEdgeDTO[];
const opts: LayoutOptions = { lens: "all", spacingScale: 1, clusterCohesion: 1, pinnedPositions: {} };

describe("layoutCacheKey", () => {
  const nodes = [node("a", "c1"), node("b", "c1"), node("c", null)];

  it("ignores node order, matching the layout which sorts by id", () => {
    const k1 = layoutCacheKey(nodes, edges, clusters, opts);
    const k2 = layoutCacheKey([...nodes].reverse(), edges, clusters, opts);
    expect(k1).toBe(k2);
    const l1 = computeDeterministicLayout(nodes, edges, clusters, opts);
    const l2 = computeDeterministicLayout([...nodes].reverse(), edges, clusters, opts);
    expect(l1.positions).toEqual(l2.positions);
  });

  it("changes when any layout input changes", () => {
    const base = layoutCacheKey(nodes, edges, clusters, opts);
    const variants = [
      layoutCacheKey([node("a", "c1", "Renamed"), nodes[1], nodes[2]], edges, clusters, opts),
      layoutCacheKey([node("a", null), nodes[1], nodes[2]], edges, clusters, opts),
      layoutCacheKey(nodes, [], clusters, opts),
      layoutCacheKey(nodes, edges, [{ id: "c1", label: "Other", kind: "topic" }] as unknown as GraphClusterDTO[], opts),
      layoutCacheKey(nodes, edges, clusters, { ...opts, lens: "risk" }),
      layoutCacheKey(nodes, edges, clusters, { ...opts, spacingScale: 1.5 }),
      layoutCacheKey(nodes, edges, clusters, { ...opts, pinnedPositions: { a: { x: 1, y: 2 } } }),
    ];
    for (const v of variants) expect(v).not.toBe(base);
    expect(new Set(variants).size).toBe(variants.length);
  });

  it("treats pins independently of insertion order", () => {
    const p1 = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } };
    const p2 = { b: { x: 3, y: 4 }, a: { x: 1, y: 2 } };
    expect(layoutCacheKey(nodes, edges, clusters, { ...opts, pinnedPositions: p1 })).toBe(
      layoutCacheKey(nodes, edges, clusters, { ...opts, pinnedPositions: p2 }),
    );
  });

  it("keeps arrival order in the key when ids repeat", () => {
    const d1 = [node("a", "c1"), node("a", null), node("b", "c1")];
    const d2 = [node("a", null), node("a", "c1"), node("b", "c1")];
    expect(layoutCacheKey(d1, edges, clusters, opts)).not.toBe(layoutCacheKey(d2, edges, clusters, opts));
  });

  it("ignores null pins like the layout does", () => {
    const bad = { a: null } as unknown as LayoutOptions["pinnedPositions"];
    expect(layoutCacheKey(nodes, edges, clusters, { ...opts, pinnedPositions: bad })).toBe(
      layoutCacheKey(nodes, edges, clusters, opts),
    );
  });
});
