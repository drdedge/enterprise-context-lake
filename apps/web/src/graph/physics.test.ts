import { describe, expect, it } from "vitest";
import {
  GOLDEN_ANGLE_RAD,
  PHI,
  computeDeterministicLayout,
  computePhiDecaySpotlight,
  computeZoomLod,
} from "./physics";
import type { GraphClusterDTO, GraphEdgeDTO, GraphNodeDTO } from "../types/api";

const sampleNodes: GraphNodeDTO[] = [
  {
    id: "n1",
    canonical_key: "veh:alpha",
    label: "Alpha Offshore",
    entity_type: "legal_vehicle",
    cluster_id: "c1",
    wall_side: "public_side",
    degree_in: 1,
    degree_out: 1,
    is_orphan: false,
    has_conflict: true,
    open_conflict_count: 1,
    topics: ["cashflows-liquidity"],
  },
  {
    id: "n2",
    canonical_key: "skill:cashflow",
    label: "Cashflow Skill",
    entity_type: "skill",
    cluster_id: "c1",
    wall_side: "public_side",
    degree_in: 0,
    degree_out: 1,
    is_orphan: false,
    has_conflict: false,
    open_conflict_count: 0,
    topics: ["cashflows-liquidity"],
  },
  {
    id: "n3",
    canonical_key: "issuer:apex",
    label: "Apex Industrial",
    entity_type: "issuer",
    cluster_id: "c2",
    wall_side: "public_side",
    degree_in: 1,
    degree_out: 0,
    is_orphan: false,
    has_conflict: false,
    open_conflict_count: 0,
    topics: ["lme-restructuring"],
  },
  {
    id: "n4",
    canonical_key: "src:orphan-1",
    label: "Orphan Source 1",
    entity_type: "data_source",
    cluster_id: null,
    wall_side: "public_side",
    degree_in: 0,
    degree_out: 0,
    is_orphan: true,
    has_conflict: false,
    open_conflict_count: 0,
    topics: [],
  },
  {
    id: "n5",
    canonical_key: "issuer:orphan-2",
    label: "Orphan Issuer 2",
    entity_type: "issuer",
    cluster_id: null,
    wall_side: "public_side",
    degree_in: 0,
    degree_out: 0,
    is_orphan: true,
    has_conflict: false,
    open_conflict_count: 0,
    topics: [],
  },
];

const sampleEdges: GraphEdgeDTO[] = [
  {
    id: "e1",
    source: "n2",
    target: "n1",
    predicate: "maps_as",
    label: "legal_vehicle",
    has_conflict: false,
  },
  {
    id: "e2",
    source: "n1",
    target: "n3",
    predicate: "holds_position",
    label: "long",
    has_conflict: false,
  },
];

const sampleClusters: GraphClusterDTO[] = [
  { id: "c1", label: "Cashflows & Liquidity", kind: "topic", member_count: 2 },
  { id: "c2", label: "LME & Restructuring", kind: "topic", member_count: 1 },
];

describe("deterministic graph physics", () => {
  it("produces bit-identical layouts across runs and preserves pins", () => {
    const first = computeDeterministicLayout(
      sampleNodes,
      sampleEdges,
      sampleClusters,
      { lens: "topic", spacingScale: 1, clusterCohesion: PHI, pinnedPositions: {} },
    );
    const second = computeDeterministicLayout(
      [...sampleNodes].reverse(),
      sampleEdges,
      sampleClusters,
      { lens: "topic", spacingScale: 1, clusterCohesion: PHI, pinnedPositions: {} },
    );
    expect(first.positions).toEqual(second.positions);

    const pinned = computeDeterministicLayout(
      sampleNodes,
      sampleEdges,
      sampleClusters,
      {
        lens: "topic",
        spacingScale: 1,
        clusterCohesion: PHI,
        pinnedPositions: { n1: { x: 42, y: -17 } },
      },
    );
    expect(pinned.positions.n1.x).toBe(42);
    expect(pinned.positions.n1.y).toBe(-17);
    expect(pinned.positions.n1.pinned).toBe(true);
  });

  it("places orphan nodes along the golden-angle perimeter ring", () => {
    const layout = computeDeterministicLayout(
      sampleNodes,
      sampleEdges,
      sampleClusters,
      { lens: "topic", spacingScale: 1, clusterCohesion: PHI, pinnedPositions: {} },
    );
    const p4 = layout.positions.n4;
    const p5 = layout.positions.n5;
    expect(p4.isOrphan).toBe(true);
    expect(p5.isOrphan).toBe(true);

    const r4 = Math.hypot(p4.x, p4.y);
    const r5 = Math.hypot(p5.x, p5.y);
    expect(Math.abs(r4 - layout.orphanRingRadius)).toBeLessThan(1);
    expect(Math.abs(r5 - layout.orphanRingRadius)).toBeLessThan(1);

    const a4 = Math.atan2(p4.y, p4.x);
    const a5 = Math.atan2(p5.y, p5.x);
    let diff = a5 - a4;
    while (diff < 0) diff += Math.PI * 2;
    expect(Math.abs(diff - GOLDEN_ANGLE_RAD)).toBeLessThan(0.02);
  });

  it("computes phi-decay hop spotlight and zoom LOD thresholds", () => {
    const spot = computePhiDecaySpotlight("n2", sampleEdges, "out");
    expect(spot.get("n2")?.opacity).toBe(1.0);
    expect(spot.get("n1")?.opacity).toBeCloseTo(0.618, 2);
    expect(spot.get("n3")?.opacity).toBeCloseTo(0.382, 2);

    expect(computeZoomLod(0.4)).toBe("cluster");
    expect(computeZoomLod(0.85)).toBe("entity");
    expect(computeZoomLod(1.25)).toBe("detail");
  });

  it("separates multi-cluster hulls and sibling node labels cleanly without overlap", () => {
    const denseNodes: GraphNodeDTO[] = [];
    const denseEdges: GraphEdgeDTO[] = [];
    const denseClusters: GraphClusterDTO[] = [];
    for (let c = 0; c < 4; c++) {
      const cid = `cluster:${c}`;
      denseClusters.push({
        id: cid,
        label: `Cluster ${c}`,
        kind: "type",
        member_count: 3,
      });
      for (let m = 0; m < 3; m++) {
        denseNodes.push({
          id: `c${c}-n${m}`,
          canonical_key: `key:c${c}-n${m}`,
          label: `Entity ${c}-${m}`,
          entity_type: "legal_vehicle",
          cluster_id: cid,
          wall_side: "public_side",
          degree_in: 1,
          degree_out: 1,
          is_orphan: false,
          has_conflict: false,
          open_conflict_count: 0,
          topics: [],
        });
      }
      denseEdges.push(
        {
          id: `e-${c}-01`,
          source: `c${c}-n0`,
          target: `c${c}-n1`,
          predicate: "rel",
          label: "rel",
          has_conflict: false,
        },
        {
          id: `e-${c}-12`,
          source: `c${c}-n1`,
          target: `c${c}-n2`,
          predicate: "rel",
          label: "rel",
          has_conflict: false,
        },
      );
      if (c > 0) {
        denseEdges.push({
          id: `e-inter-${c}`,
          source: `c${c - 1}-n0`,
          target: `c${c}-n0`,
          predicate: "links",
          label: "links",
          has_conflict: false,
        });
      }
    }

    const res = computeDeterministicLayout(
      denseNodes,
      denseEdges,
      denseClusters,
      { lens: "fund_hierarchy", spacingScale: 1, clusterCohesion: PHI, pinnedPositions: {} },
    );

    // Assert zero pairwise cluster hull intersections
    for (let i = 0; i < res.hulls.length; i++) {
      for (let j = i + 1; j < res.hulls.length; j++) {
        const a = res.hulls[i];
        const b = res.hulls[j];
        const overlapX =
          Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
        const overlapY =
          Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
        expect(overlapX > 0 && overlapY > 0).toBe(false);
      }
    }

    // Assert zero colliding node label boxes
    const pts = Object.values(res.positions);
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const ax = Math.abs(pts[i].x - pts[j].x);
        const ay = Math.abs(pts[i].y - pts[j].y);
        expect(ax < 160 && ay < 68).toBe(false);
      }
    }
  });
});
