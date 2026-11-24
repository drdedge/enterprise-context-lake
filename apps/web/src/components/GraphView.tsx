import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Filter,
  Maximize2,
  Minus,
  Pin,
  Plus,
  RotateCcw,
  Search,
  Settings2,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { useGraphLayout } from "../graph/useGraphLayout";
import {
  HOVER_SPOTLIGHT_DEBOUNCE_MS,
  PHI,
  computePhiDecaySpotlight,
  computeZoomLod,
  type Point2D,
} from "../graph/physics";
import type {
  AdapterConfigDTO,
  AuthContextState,
  DomainNature,
  EpistemicTemporal,
  GraphDirection,
  GraphEdgeDTO,
  GraphLens,
  GraphNodeDTO,
  GraphResponseDTO,
  ImpactResponseDTO,
  IngestionChannel,
  Modality,
  TemporalSnapshot,
  TopicDTO,
} from "../types/api";
import { formatEntityType, pluralize } from "../utils/platform";

const PIN_STORAGE_KEY = "ecl:graph-pins:v1";

interface GraphViewProps {
  auth: AuthContextState;
  snapshot: TemporalSnapshot;
  topics: TopicDTO[];
  adapters: AdapterConfigDTO | null;
  onUpdateAdapters: (
    patch: Partial<Pick<AdapterConfigDTO, "store" | "retrieval" | "llm_mode">>,
  ) => Promise<void>;
  lens: GraphLens;
  onChangeLens: (lens: GraphLens) => void;
  focusNodeId: string | null;
  focusDirection: GraphDirection;
  onChangeFocus: (nodeId: string | null, direction?: GraphDirection) => void;
  selectedEntityId: string | null;
  onSelectEntity: (idOrKey: string) => void;
  onVisibleEntityIdsChange: (ids: string[]) => void;
  dataEpoch: number;
}

const LENS_TABS: Array<{ id: GraphLens; label: string }> = [
  { id: "topic", label: "Topic" },
  { id: "fund_hierarchy", label: "Fund Hierarchy" },
  { id: "issuer", label: "Issuer" },
  { id: "app", label: "App" },
  { id: "skill", label: "Skill" },
  { id: "project", label: "Project" },
];

function loadPins(): Record<string, Record<string, Point2D>> {
  try {
    const raw = window.localStorage.getItem(PIN_STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Record<string, Record<string, Point2D>>;
  } catch {
    return {};
  }
}

function savePins(pins: Record<string, Record<string, Point2D>>): void {
  try {
    window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(pins));
  } catch {
    // Ignore storage quota errors
  }
}

export function GraphView({
  auth,
  snapshot,
  topics,
  adapters,
  onUpdateAdapters,
  lens,
  onChangeLens,
  focusNodeId,
  focusDirection,
  onChangeFocus,
  selectedEntityId,
  onSelectEntity,
  onVisibleEntityIdsChange,
  dataEpoch,
}: GraphViewProps) {
  const [graph, setGraph] = useState<GraphResponseDTO | null>(null);
  const [loading, setLoading] = useState(false);

  // Control bar state (all popovers strictly CLOSED by default)
  const [searchQuery, setSearchQuery] = useState("");
  const [isolateMatches, setIsolateMatches] = useState(false);
  const [openPopover, setOpenPopover] = useState<null | "filter" | "display">(
    null,
  );
  const [scopeMode, setScopeMode] = useState<"global" | "local">("global");
  const [hops, setHops] = useState<1 | 2>(1);

  // Facet filters inside Filter popover
  const [domainNature, setDomainNature] = useState<"" | DomainNature>("");
  const [epistemicTemporal, setEpistemicTemporal] = useState<
    "" | EpistemicTemporal
  >("");
  const [modality, setModality] = useState<"" | Modality>("");
  const [coverageStatus, setCoverageStatus] = useState("");
  const [ingestionChannel, setIngestionChannel] = useState<
    "" | IngestionChannel
  >("");
  const [topicFilter, setTopicFilter] = useState("");
  const [conflictsOnly, setConflictsOnly] = useState(false);

  // Display tuning inside Display popover
  const [spacingScale, setSpacingScale] = useState(1.0);
  const [clusterCohesion, setClusterCohesion] = useState(
    Number(PHI.toFixed(3)),
  );
  const [showHulls, setShowHulls] = useState(true);
  const [showOrphanRing, setShowOrphanRing] = useState(true);

  // Spatial memory pins per lens
  const [allPins, setAllPins] = useState<Record<string, Record<string, Point2D>>>(
    () => loadPins(),
  );

  // Interactive spotlight, sticky focus, impact BFS, context menu
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const hoverTimerRef = useRef<number | null>(null);
  const [stickyDirectionActive, setStickyDirectionActive] = useState(false);
  const [impactTrace, setImpactTrace] = useState<ImpactResponseDTO | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    node: GraphNodeDTO;
    x: number;
    y: number;
  } | null>(null);

  // Viewport transform (pan & zoom)
  const containerRef = useRef<HTMLDivElement>(null);
  const controlBarRef = useRef<HTMLDivElement>(null);
  const [viewportSize, setViewportSize] = useState({ width: 960, height: 680 });
  const [transform, setTransform] = useState({ x: 480, y: 340, scale: 0.92 });

  // Pointer drag state (pan canvas OR drag-to-pin node)
  const suppressClickRef = useRef(false);
  const dragRef = useRef<
    | null
    | {
        mode: "pan";
        startX: number;
        startY: number;
        origX: number;
        origY: number;
      }
    | {
        mode: "node";
        nodeId: string;
        startClientX: number;
        startClientY: number;
        origWorldX: number;
        origWorldY: number;
        moved: boolean;
        captured: boolean;
      }
  >(null);

  // Keep viewport measurements in sync
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        setViewportSize({ width: rect.width, height: rect.height });
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Close popovers and context menu on outside click / Escape
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (
        controlBarRef.current &&
        !controlBarRef.current.contains(e.target as Node)
      ) {
        setOpenPopover(null);
      }
      setContextMenu(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpenPopover(null);
        setContextMenu(null);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  // Activate sticky directional mode if navigated externally with focusNodeId
  useEffect(() => {
    if (focusNodeId && focusDirection !== "both") {
      setStickyDirectionActive(true);
    }
  }, [focusNodeId, focusDirection]);

  // Fetch graph from API
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setImpactTrace(null);

    api
      .getGraph(auth, {
        lens,
        focus_id: scopeMode === "local" && focusNodeId ? focusNodeId : undefined,
        hops: scopeMode === "local" ? hops : 2,
        direction: scopeMode === "local" ? focusDirection : "both",
        topic: topicFilter || undefined,
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
      })
      .then((res) => {
        if (!cancelled) setGraph(res);
      })
      .catch(() => {
        if (!cancelled) setGraph(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [
    auth,
    lens,
    scopeMode,
    focusNodeId,
    hops,
    focusDirection,
    topicFilter,
    snapshot,
    dataEpoch,
  ]);

  // Client-side orthogonal facet filtering & search matching
  const { visibleNodes, visibleEdges, matchedNodeIds } = useMemo(() => {
    const allNodes = graph?.nodes || [];
    const allEdges = graph?.edges || [];

    const facetFiltered = allNodes.filter((n) => {
      if (conflictsOnly && !n.has_conflict) return false;
      if (domainNature && n.domain_nature && n.domain_nature !== domainNature) {
        return false;
      }
      if (
        domainNature === "financial" &&
        epistemicTemporal &&
        n.epistemic_temporal &&
        n.epistemic_temporal !== epistemicTemporal
      ) {
        return false;
      }
      if (modality && n.modality && n.modality !== modality) return false;
      if (
        coverageStatus &&
        n.coverage_status &&
        n.coverage_status !== coverageStatus
      ) {
        return false;
      }
      if (
        ingestionChannel &&
        n.ingestion_channel &&
        n.ingestion_channel !== ingestionChannel
      ) {
        return false;
      }
      if (topicFilter && n.topics.length > 0 && !n.topics.includes(topicFilter)) {
        // Keep topic root node itself or matching cluster node
        if (!n.canonical_key.includes(topicFilter)) return false;
      }
      return true;
    });

    const q = searchQuery.trim().toLowerCase();
    const matches = new Set<string>();
    if (q) {
      for (const n of facetFiltered) {
        if (
          n.label.toLowerCase().includes(q) ||
          n.canonical_key.toLowerCase().includes(q) ||
          n.entity_type.toLowerCase().includes(q) ||
          n.topics.some((t) => t.toLowerCase().includes(q))
        ) {
          matches.add(n.id);
        }
      }
    }

    const finalNodes =
      q && isolateMatches
        ? facetFiltered.filter((n) => matches.has(n.id))
        : facetFiltered;
    const idSet = new Set(finalNodes.map((n) => n.id));
    const finalEdges = allEdges.filter(
      (e) => idSet.has(e.source) && idSet.has(e.target),
    );

    return {
      visibleNodes: finalNodes,
      visibleEdges: finalEdges,
      matchedNodeIds: matches,
    };
  }, [
    graph,
    conflictsOnly,
    domainNature,
    epistemicTemporal,
    modality,
    coverageStatus,
    ingestionChannel,
    topicFilter,
    searchQuery,
    isolateMatches,
  ]);

  // Keep viewport agent grounded in currently visible nodes
  useEffect(() => {
    onVisibleEntityIdsChange(visibleNodes.map((n) => n.id));
  }, [visibleNodes, onVisibleEntityIdsChange]);

  const lensPins = useMemo(() => allPins[lens] || {}, [allPins, lens]);

  // Deterministic Barnes-Hut + φ-asymmetric compound + Golden-Angle orphan layout.
  // Large graphs are laid out in a Web Worker (the previous layout stays on screen meanwhile)
  // and every result is cached, so switching back to a lens is instant.
  const graphClusters = useMemo(() => graph?.clusters || [], [graph?.clusters]);
  const layoutOptions = useMemo(
    () => ({ lens, spacingScale, clusterCohesion, pinnedPositions: lensPins }),
    [lens, spacingScale, clusterCohesion, lensPins],
  );
  const { layout, pending: layoutPending } = useGraphLayout(
    visibleNodes,
    visibleEdges,
    graphClusters,
    layoutOptions,
  );

  // Fit bounds helper
  const fitToView = useCallback(() => {
    const { minX, maxX, minY, maxY } = layout.bounds;
    const bw = Math.max(maxX - minX, 240);
    const bh = Math.max(maxY - minY, 200);
    const pad = 56;
    const scaleX = (viewportSize.width - pad * 2) / bw;
    const scaleY = (viewportSize.height - pad * 2) / bh;
    const nextScale = Math.max(0.45, Math.min(1.15, Math.min(scaleX, scaleY)));
    const cx = (minX + maxX) * 0.5;
    const cy = (minY + maxY) * 0.5;
    setTransform({
      x: viewportSize.width * 0.5 - cx * nextScale,
      y: viewportSize.height * 0.5 - cy * nextScale,
      scale: Number(nextScale.toFixed(3)),
    });
  }, [layout.bounds, viewportSize.width, viewportSize.height]);

  // Focus-zoom helper centering on a directional / impact subgraph while keeping full SVG/DOM intact
  const fitToNodeIds = useCallback(
    (ids: string[]) => {
      const pts = ids
        .map((id) => layout.positions[id])
        .filter((p): p is NonNullable<typeof p> => Boolean(p));
      if (pts.length === 0) return;
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (const p of pts) {
        if (p.x - 135 < minX) minX = p.x - 135;
        if (p.x + 135 > maxX) maxX = p.x + 135;
        if (p.y - 85 < minY) minY = p.y - 85;
        if (p.y + 85 > maxY) maxY = p.y + 85;
      }
      const bw = Math.max(maxX - minX, 260);
      const bh = Math.max(maxY - minY, 220);
      const pad = 64;
      const scaleX = (viewportSize.width - pad * 2) / bw;
      const scaleY = (viewportSize.height - pad * 2) / bh;
      const nextScale = Math.max(0.78, Math.min(1.08, Math.min(scaleX, scaleY)));
      const cx = (minX + maxX) * 0.5;
      const cy = (minY + maxY) * 0.5;
      setTransform({
        x: viewportSize.width * 0.5 - cx * nextScale,
        y: viewportSize.height * 0.5 - cy * nextScale + 12,
        scale: Number(nextScale.toFixed(3)),
      });
    },
    [layout.positions, viewportSize.width, viewportSize.height],
  );

  // Auto-fit cleanly whenever lens or node set changes
  const graphSignature = `${lens}:${visibleNodes.length}:${graph?.data_epoch ?? 0}`;
  // While a large layout is computing in the worker, the positions are stale; defer the fit until it lands
  const fitPendingRef = useRef(false);
  useEffect(() => {
    if (visibleNodes.length > 0 && !stickyDirectionActive && !impactTrace) {
      if (layoutPending) {
        fitPendingRef.current = true;
      } else {
        fitToView();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphSignature, viewportSize.width, viewportSize.height]);
  useEffect(() => {
    if (!layoutPending && fitPendingRef.current) {
      fitPendingRef.current = false;
      if (!stickyDirectionActive && !impactTrace) fitToView();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutPending, fitToView]);

  // Trigger Impact BFS
  const runImpactBfs = async (nodeId: string) => {
    try {
      const res = await api.getImpact(auth, nodeId, {
        max_depth: 4,
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
      });
      setImpactTrace(res);
      setStickyDirectionActive(true);
      onChangeFocus(nodeId, "in");
      const ids = [res.root_node_id, ...(res.nodes || []).map((n) => n.id)];
      fitToNodeIds(ids);
    } catch {
      setImpactTrace(null);
    }
  };

  // Focus-zoom smoothly when sticky directional focus is active
  useEffect(() => {
    if (!stickyDirectionActive || !focusNodeId) return;
    if (impactTrace) {
      const ids = [
        impactTrace.root_node_id,
        ...(impactTrace.nodes || []).map((n) => n.id),
      ];
      fitToNodeIds(ids);
      return;
    }
    const ids = new Set<string>([focusNodeId]);
    for (const edge of visibleEdges) {
      if (
        (focusDirection === "in" || focusDirection === "both") &&
        edge.target === focusNodeId
      ) {
        ids.add(edge.source);
      }
      if (
        (focusDirection === "out" || focusDirection === "both") &&
        edge.source === focusNodeId
      ) {
        ids.add(edge.target);
      }
    }
    fitToNodeIds(Array.from(ids));
  }, [
    stickyDirectionActive,
    focusNodeId,
    focusDirection,
    impactTrace,
    visibleEdges,
    fitToNodeIds,
  ]);

  // Deterministic staggered label position & curvature for converging/diverging edges
  const edgeLabelLayout = useMemo(() => {
    const byTarget = new Map<string, GraphEdgeDTO[]>();
    const bySource = new Map<string, GraphEdgeDTO[]>();
    for (const e of visibleEdges) {
      const tList = byTarget.get(e.target) || [];
      tList.push(e);
      byTarget.set(e.target, tList);
      const sList = bySource.get(e.source) || [];
      sList.push(e);
      bySource.set(e.source, sList);
    }

    const slotsFor = (count: number): number[] => {
      if (count <= 1) return [0.5];
      if (count === 2) return [0.35, 0.65];
      if (count === 3) return [0.32, 0.5, 0.68];
      return Array.from({ length: count }, (_, i) =>
        Number((0.26 + (i / (count - 1)) * 0.48).toFixed(3)),
      );
    };

    const result = new Map<string, { t: number; curveSign: number }>();
    for (const e of visibleEdges) {
      const tGroup = byTarget.get(e.target) || [e];
      const sGroup = bySource.get(e.source) || [e];
      const group = tGroup.length >= sGroup.length ? tGroup : sGroup;
      const sorted = [...group].sort((a, b) => a.id.localeCompare(b.id));
      const idx = Math.max(
        0,
        sorted.findIndex((item) => item.id === e.id),
      );
      const slots = slotsFor(sorted.length);
      const curveSign = sorted.length > 1 ? (idx % 2 === 0 ? -1.35 : 1.35) : 1;
      result.set(e.id, { t: slots[idx] ?? 0.5, curveSign });
    }
    return result;
  }, [visibleEdges]);

  // Determine active spotlight root & φ-decay opacities
  const activeRootId = hoveredNodeId || (stickyDirectionActive ? focusNodeId : null);
  const activeSpotlightDirection: GraphDirection = hoveredNodeId
    ? "both"
    : focusDirection;

  const spotlightMap = useMemo(() => {
    if (!activeRootId) return null;
    return computePhiDecaySpotlight(
      activeRootId,
      visibleEdges,
      activeSpotlightDirection,
    );
  }, [activeRootId, visibleEdges, activeSpotlightDirection]);

  const impactNodeIds = useMemo(() => {
    if (!impactTrace) return null;
    const s = new Set<string>([impactTrace.root_node_id]);
    for (const n of impactTrace.nodes || []) s.add(n.id);
    return s;
  }, [impactTrace]);

  const impactEdgeIds = useMemo(() => {
    if (!impactTrace) return null;
    return new Set<string>((impactTrace.edges || []).map((e) => e.id));
  }, [impactTrace]);

  const lod = computeZoomLod(transform.scale);

  // Hover 300ms debounce handlers
  const handleNodePointerEnter = (nodeId: string) => {
    if (hoverTimerRef.current) window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      setHoveredNodeId(nodeId);
    }, HOVER_SPOTLIGHT_DEBOUNCE_MS);
  };

  const handleNodePointerLeave = () => {
    if (hoverTimerRef.current) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
    setHoveredNodeId(null);
  };

  useEffect(() => {
    return () => {
      if (hoverTimerRef.current) window.clearTimeout(hoverTimerRef.current);
    };
  }, []);

  // Pinning helpers
  const pinNode = (nodeId: string, pt: Point2D) => {
    setAllPins((prev) => {
      const next = {
        ...prev,
        [lens]: { ...(prev[lens] || {}), [nodeId]: pt },
      };
      savePins(next);
      return next;
    });
  };

  const unpinNode = (nodeId: string) => {
    setAllPins((prev) => {
      const currentLens = { ...(prev[lens] || {}) };
      delete currentLens[nodeId];
      const next = { ...prev, [lens]: currentLens };
      savePins(next);
      return next;
    });
  };

  const resetLensPins = () => {
    setAllPins((prev) => {
      const next = { ...prev, [lens]: {} };
      savePins(next);
      return next;
    });
  };

  // Canvas & node pointer interactions
  const handleCanvasPointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    setContextMenu(null);
    dragRef.current = {
      mode: "pan",
      startX: e.clientX,
      startY: e.clientY,
      origX: transform.x,
      origY: transform.y,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const handleNodePointerDown = (
    e: React.PointerEvent<SVGGElement>,
    node: GraphNodeDTO,
  ) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    setContextMenu(null);
    suppressClickRef.current = false;
    const pos = layout.positions[node.id];
    if (!pos) return;
    dragRef.current = {
      mode: "node",
      nodeId: node.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      origWorldX: pos.x,
      origWorldY: pos.y,
      moved: false,
      captured: false,
    };
  };

  const handleSvgPointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (drag.mode === "pan") {
      setTransform((prev) => ({
        ...prev,
        x: drag.origX + (e.clientX - drag.startX),
        y: drag.origY + (e.clientY - drag.startY),
      }));
    } else if (drag.mode === "node") {
      const dxScreen = e.clientX - drag.startClientX;
      const dyScreen = e.clientY - drag.startClientY;
      if (Math.hypot(dxScreen, dyScreen) > 4) {
        drag.moved = true;
        suppressClickRef.current = true;
        if (!drag.captured) {
          e.currentTarget.setPointerCapture(e.pointerId);
          drag.captured = true;
        }
        const wx = Math.round((drag.origWorldX + dxScreen / transform.scale) * 10) / 10;
        const wy = Math.round((drag.origWorldY + dyScreen / transform.scale) * 10) / 10;
        pinNode(drag.nodeId, { x: wx, y: wy });
      }
    }
  };

  const handleSvgPointerUp = (e: React.PointerEvent<SVGSVGElement>) => {
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    dragRef.current = null;
  };

  const handleWheel = (e: React.WheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    setTransform((prev) => {
      const nextScale = Math.max(0.35, Math.min(2.4, prev.scale * factor));
      const worldX = (mx - prev.x) / prev.scale;
      const worldY = (my - prev.y) / prev.scale;
      return {
        scale: Number(nextScale.toFixed(3)),
        x: mx - worldX * nextScale,
        y: my - worldY * nextScale,
      };
    });
  };

  const adjustZoom = (factor: number) => {
    const cx = viewportSize.width * 0.5;
    const cy = viewportSize.height * 0.5;
    setTransform((prev) => {
      const nextScale = Math.max(0.35, Math.min(2.4, prev.scale * factor));
      const worldX = (cx - prev.x) / prev.scale;
      const worldY = (cy - prev.y) / prev.scale;
      return {
        scale: Number(nextScale.toFixed(3)),
        x: cx - worldX * nextScale,
        y: cy - worldY * nextScale,
      };
    });
  };

  const activeFilterCount = [
    domainNature,
    epistemicTemporal,
    modality,
    coverageStatus,
    ingestionChannel,
    topicFilter,
    conflictsOnly ? "1" : "",
  ].filter(Boolean).length;

  const focusedNode = visibleNodes.find(
    (n) => n.id === focusNodeId || n.id === selectedEntityId,
  );
  const hoveredNode = visibleNodes.find((n) => n.id === hoveredNodeId);

  const directionFocusAttr = impactTrace
    ? "impact"
    : stickyDirectionActive && focusNodeId
      ? focusDirection === "in"
        ? "reverse"
        : focusDirection === "out"
          ? "forward"
          : "both"
      : "none";

  const nodeStrokeColor = (node: GraphNodeDTO) => {
    if (node.has_conflict) return "#F43F5E";
    if (node.wall_side === "private_side") return "#F59E0B";
    if (node.entity_type === "skill" || node.entity_type === "app") return "#38BDF8";
    if (
      node.entity_type === "fund_family" ||
      node.entity_type === "legal_vehicle" ||
      node.entity_type === "reporting_group" ||
      node.entity_type === "strategy"
    ) {
      return "#34D399";
    }
    if (node.entity_type === "topic") return "#38BDF8";
    return "#A1A1AA";
  };

  const nodeRadius = (node: GraphNodeDTO) => {
    if (node.is_orphan) return 7;
    const deg = node.degree_in + node.degree_out;
    if (deg >= 5 || node.entity_type === "fund_family" || node.entity_type === "topic") {
      return 10.5;
    }
    return 8.5;
  };

  const computeNodeVisualState = (node: GraphNodeDTO) => {
    // Priority 1: Search foreground dimming
    if (searchQuery.trim() && !isolateMatches) {
      const matched = matchedNodeIds.has(node.id);
      return {
        opacity: matched ? 1.0 : 0.12,
        tier: matched ? "0" : "dim",
      };
    }

    // Priority 2: Impact BFS overlay
    if (impactNodeIds) {
      if (node.id === impactTrace?.root_node_id) {
        return { opacity: 1.0, tier: "0" };
      }
      if (impactNodeIds.has(node.id)) {
        return { opacity: 0.92, tier: "1" };
      }
      return { opacity: 0.12, tier: "dim" };
    }

    // Priority 3: 300ms-debounced φ-decay hover spotlight or sticky directional focus
    if (spotlightMap) {
      const hit = spotlightMap.get(node.id);
      if (hit) return { opacity: hit.opacity, tier: hit.tier };
      return { opacity: 0.12, tier: "dim" };
    }

    return { opacity: 1.0, tier: "none" };
  };

  const computeEdgeVisual = (edge: GraphEdgeDTO) => {
    const isRootIn = activeRootId && edge.target === activeRootId;
    const isRootOut = activeRootId && edge.source === activeRootId;
    const inImpact = impactEdgeIds?.has(edge.id) ?? false;

    if (edge.has_conflict) {
      return {
        stroke: "#F43F5E",
        marker: "url(#arrow-conflict)",
        opacity: activeRootId && !isRootIn && !isRootOut && !inImpact ? 0.25 : 0.95,
        strokeWidth: 1.75,
        dashed: true,
        showLabel: Boolean(isRootIn || isRootOut || inImpact || lod === "detail"),
      };
    }

    if (inImpact) {
      return {
        stroke: "#38BDF8",
        marker: "url(#arrow-upstream)",
        opacity: 0.95,
        strokeWidth: 2,
        dashed: false,
        showLabel: true,
      };
    }

    if (isRootIn && (activeSpotlightDirection === "both" || activeSpotlightDirection === "in")) {
      return {
        stroke: "#38BDF8",
        marker: "url(#arrow-upstream)",
        opacity: 0.95,
        strokeWidth: 1.85,
        dashed: false,
        showLabel: true,
      };
    }

    if (isRootOut && (activeSpotlightDirection === "both" || activeSpotlightDirection === "out")) {
      return {
        stroke: "#F59E0B",
        marker: "url(#arrow-downstream)",
        opacity: 0.95,
        strokeWidth: 1.85,
        dashed: false,
        showLabel: true,
      };
    }

    if (activeRootId || impactNodeIds || (searchQuery.trim() && !isolateMatches)) {
      const sHit = spotlightMap?.has(edge.source) ?? false;
      const tHit = spotlightMap?.has(edge.target) ?? false;
      return {
        stroke: "rgba(255,255,255,0.22)",
        marker: "url(#arrow-default)",
        opacity: sHit && tHit ? 0.36 : 0.07,
        strokeWidth: 1.1,
        dashed: false,
        showLabel: false,
      };
    }

    return {
      stroke: "rgba(255,255,255,0.20)",
      marker: "url(#arrow-default)",
      opacity: 0.72,
      strokeWidth: 1.2,
      dashed: false,
      showLabel: lod === "detail",
    };
  };

  return (
    <div
      data-testid="graph-view"
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-[#0A0A0B]"
    >
      {/* Single Compact 40px Graph Control Bar — ZERO permanent sidebars! */}
      <div
        ref={controlBarRef}
        data-testid="graph-control-bar"
        className="relative z-30 flex h-10 shrink-0 items-center justify-between gap-2 border-b border-white/[0.08] bg-[#111113] px-3"
      >
        {/* Lens tabs */}
        <div
          role="tablist"
          aria-label="Graph lens"
          className="flex items-center gap-0.5 overflow-x-auto"
        >
          {LENS_TABS.map((tab) => {
            const active = lens === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={active}
                data-testid={`graph-lens-${tab.id}`}
                onClick={() => {
                  onChangeLens(tab.id);
                  setImpactTrace(null);
                  setStickyDirectionActive(false);
                }}
                className={`shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  active
                    ? "bg-[#18181B] text-[#FAFAFA]"
                    : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>

        {/* Right controls: Search + Isolate, Filter popover, Display popover, Global/Local */}
        <div className="flex shrink-0 items-center gap-1.5">
          <div className="relative flex items-center">
            <Search className="pointer-events-none absolute left-2 h-3.5 w-3.5 text-[#94949E]" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Find node…"
              aria-label="Search graph nodes"
              data-testid="graph-search-input"
              className="h-7 w-28 rounded-l-md border border-white/[0.08] bg-[#0A0A0B] pl-7 pr-2 text-xs text-[#FAFAFA] placeholder-[#94949E] focus:border-white/[0.2] focus:outline-none xl:w-36"
            />
            <button
              type="button"
              data-testid="graph-isolate-toggle"
              aria-pressed={isolateMatches}
              onClick={() => setIsolateMatches((v) => !v)}
              className={`h-7 rounded-r-md border border-l-0 px-2 text-[11px] font-medium transition-colors ${
                isolateMatches
                  ? "border-white/[0.2] bg-[#FAFAFA] text-[#0A0A0B]"
                  : "border-white/[0.08] bg-[#18181B] text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Isolate
            </button>
          </div>

          {/* Filter popover trigger (closed by default) */}
          <div className="relative">
            <button
              type="button"
              data-testid="graph-filter-trigger"
              aria-expanded={openPopover === "filter"}
              onClick={() =>
                setOpenPopover(openPopover === "filter" ? null : "filter")
              }
              className={`flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-medium transition-colors ${
                activeFilterCount > 0 || openPopover === "filter"
                  ? "border-white/[0.2] bg-[#18181B] text-[#FAFAFA]"
                  : "border-white/[0.08] bg-[#0A0A0B] text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              <Filter className="h-3 w-3" />
              <span>Filter</span>
              {activeFilterCount > 0 && (
                <span className="rounded bg-white/15 px-1 font-mono text-[10px] tabular-nums text-[#FAFAFA]">
                  {activeFilterCount}
                </span>
              )}
            </button>

            {openPopover === "filter" && (
              <div
                role="dialog"
                aria-label="Graph filters"
                data-testid="graph-filter-popover"
                className="absolute right-0 top-9 z-40 w-72 space-y-2.5 rounded-lg border border-white/[0.08] bg-[#111113] p-3 text-xs shadow-2xl"
              >
                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">
                    Domain Nature
                  </span>
                  <select
                    data-testid="graph-filter-domain"
                    value={domainNature}
                    onChange={(e) => {
                      const next = e.target.value as "" | DomainNature;
                      setDomainNature(next);
                      if (next !== "financial") setEpistemicTemporal("");
                    }}
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                  >
                    <option value="">All</option>
                    <option value="financial">Financial</option>
                    <option value="non_financial">Non-Financial</option>
                  </select>
                </label>

                {domainNature === "financial" && (
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] text-[#A1A1AA]">
                      Financial Horizon
                    </span>
                    <select
                      data-testid="graph-filter-temporal"
                      value={epistemicTemporal}
                      onChange={(e) =>
                        setEpistemicTemporal(
                          e.target.value as "" | EpistemicTemporal,
                        )
                      }
                      className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                    >
                      <option value="">All</option>
                      <option value="actual">Actuals</option>
                      <option value="estimate">Estimate</option>
                      <option value="forecast">Forecast</option>
                      <option value="guidance">Guidance</option>
                      <option value="pro_forma">Pro Forma</option>
                    </select>
                  </label>
                )}

                <div className="grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] text-[#A1A1AA]">Modality</span>
                    <select
                      value={modality}
                      onChange={(e) =>
                        setModality(e.target.value as "" | Modality)
                      }
                      className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                    >
                      <option value="">All</option>
                      <option value="quantitative">Quant</option>
                      <option value="qualitative">Qual</option>
                    </select>
                  </label>

                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] text-[#A1A1AA]">Coverage</span>
                    <select
                      value={coverageStatus}
                      onChange={(e) => setCoverageStatus(e.target.value)}
                      className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                    >
                      <option value="">All</option>
                      <option value="held">Held</option>
                      <option value="watchlist">Watchlist</option>
                      <option value="pipeline">Pipeline</option>
                      <option value="benchmark">Benchmark</option>
                      <option value="passed">Passed</option>
                    </select>
                  </label>
                </div>

                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">
                    Ingestion Channel
                  </span>
                  <select
                    value={ingestionChannel}
                    onChange={(e) =>
                      setIngestionChannel(
                        e.target.value as "" | IngestionChannel,
                      )
                    }
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                  >
                    <option value="">All</option>
                    <option value="automated_feed">Automated Feed</option>
                    <option value="manual_upload">Manual Upload</option>
                    <option value="human_authored">Human Authored</option>
                    <option value="agent_extracted">Agent Extracted</option>
                  </select>
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">Topic</span>
                  <select
                    data-testid="graph-filter-topic"
                    value={topicFilter}
                    onChange={(e) => setTopicFilter(e.target.value)}
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
                  >
                    <option value="">All Topics</option>
                    {topics
                      .filter((t) => t.status === "active" || t.status === "proposed")
                      .map((t) => (
                        <option key={t.topic_id} value={t.slug}>
                          {t.pref_label}
                        </option>
                      ))}
                  </select>
                </label>

                <label className="flex items-center justify-between pt-1">
                  <span className="text-[#F4F4F5]">Open conflicts only</span>
                  <input
                    type="checkbox"
                    checked={conflictsOnly}
                    onChange={(e) => setConflictsOnly(e.target.checked)}
                    className="accent-white"
                  />
                </label>

                {activeFilterCount > 0 && (
                  <button
                    type="button"
                    onClick={() => {
                      setDomainNature("");
                      setEpistemicTemporal("");
                      setModality("");
                      setCoverageStatus("");
                      setIngestionChannel("");
                      setTopicFilter("");
                      setConflictsOnly(false);
                    }}
                    className="w-full rounded border border-white/[0.1] bg-[#18181B] py-1 text-[11px] text-[#FAFAFA] hover:bg-[#202024]"
                  >
                    Clear Filters
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Display / Tuning popover trigger (closed by default) */}
          <div className="relative">
            <button
              type="button"
              data-testid="graph-display-trigger"
              aria-expanded={openPopover === "display"}
              onClick={() =>
                setOpenPopover(openPopover === "display" ? null : "display")
              }
              className={`flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-medium transition-colors ${
                openPopover === "display"
                  ? "border-white/[0.2] bg-[#18181B] text-[#FAFAFA]"
                  : "border-white/[0.08] bg-[#0A0A0B] text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              <Settings2 className="h-3 w-3" />
              <span>Display</span>
            </button>

            {openPopover === "display" && (
              <div
                role="dialog"
                aria-label="Graph display tuning"
                data-testid="graph-display-popover"
                className="absolute right-0 top-9 z-40 w-72 space-y-3 rounded-lg border border-white/[0.08] bg-[#111113] p-3 text-xs shadow-2xl"
              >
                <label className="block space-y-1">
                  <div className="flex justify-between">
                    <span className="text-[#A1A1AA]">Spacing Scale</span>
                    <span className="font-mono tabular-nums text-[#FAFAFA]">
                      {spacingScale.toFixed(2)}×
                    </span>
                  </div>
                  <input
                    type="range"
                    min={0.7}
                    max={1.6}
                    step={0.05}
                    value={spacingScale}
                    onChange={(e) => setSpacingScale(Number(e.target.value))}
                    className="w-full accent-white"
                  />
                </label>

                <label className="block space-y-1">
                  <div className="flex justify-between">
                    <span className="text-[#A1A1AA]">φ-Cluster Cohesion</span>
                    <span className="font-mono tabular-nums text-[#FAFAFA]">
                      {clusterCohesion.toFixed(3)}
                    </span>
                  </div>
                  <input
                    type="range"
                    min={1.0}
                    max={2.2}
                    step={0.05}
                    value={clusterCohesion}
                    onChange={(e) => setClusterCohesion(Number(e.target.value))}
                    className="w-full accent-white"
                  />
                </label>

                <div className="space-y-1.5 border-t border-white/[0.08] pt-2">
                  <label className="flex items-center justify-between">
                    <span className="text-[#F4F4F5]">Cluster Hulls</span>
                    <input
                      type="checkbox"
                      checked={showHulls}
                      onChange={(e) => setShowHulls(e.target.checked)}
                      className="accent-white"
                    />
                  </label>
                  <label className="flex items-center justify-between">
                    <span className="text-[#F4F4F5]">Orphan Ring (137.5°)</span>
                    <input
                      type="checkbox"
                      checked={showOrphanRing}
                      onChange={(e) => setShowOrphanRing(e.target.checked)}
                      className="accent-white"
                    />
                  </label>
                </div>

                <div className="flex gap-2">
                  <button
                    type="button"
                    data-testid="graph-reset-pins"
                    onClick={resetLensPins}
                    className="flex flex-1 items-center justify-center gap-1 rounded border border-white/[0.1] bg-[#18181B] py-1 text-[11px] text-[#FAFAFA] hover:bg-[#202024]"
                  >
                    <RotateCcw className="h-3 w-3" />
                    Reset Pins
                  </button>
                  <button
                    type="button"
                    onClick={fitToView}
                    className="flex flex-1 items-center justify-center gap-1 rounded border border-white/[0.1] bg-[#18181B] py-1 text-[11px] text-[#FAFAFA] hover:bg-[#202024]"
                  >
                    <Maximize2 className="h-3 w-3" />
                    Fit View
                  </button>
                </div>

                {adapters && (
                  <div className="space-y-2 border-t border-white/[0.08] pt-2">
                    <div className="text-[11px] font-medium text-[#A1A1AA]">
                      Runtime Adapters
                    </div>
                    <select
                      aria-label="Store adapter"
                      value={adapters.store}
                      onChange={(e) =>
                        onUpdateAdapters({
                          store: e.target.value as AdapterConfigDTO["store"],
                        })
                      }
                      className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-[11px] text-[#FAFAFA]"
                    >
                      <option value="sqlite_bitemporal">sqlite_bitemporal</option>
                      <option value="okf_markdown_vault">
                        okf_markdown_vault
                      </option>
                    </select>
                    <select
                      aria-label="Retrieval adapter"
                      value={adapters.retrieval}
                      onChange={(e) =>
                        onUpdateAdapters({
                          retrieval: e.target
                            .value as AdapterConfigDTO["retrieval"],
                        })
                      }
                      className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-[11px] text-[#FAFAFA]"
                    >
                      <option value="lexical_manifest">lexical_manifest</option>
                      <option value="facet_graph">facet_graph</option>
                    </select>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Global | Local directional hop scope */}
          <div
            role="group"
            aria-label="Graph scope"
            className="flex items-center rounded-md border border-white/[0.08] bg-[#0A0A0B] p-0.5"
          >
            <button
              type="button"
              data-testid="graph-scope-global"
              onClick={() => setScopeMode("global")}
              className={`min-h-6 rounded px-2 py-0.5 text-[11px] font-medium ${
                scopeMode === "global"
                  ? "bg-[#18181B] text-[#FAFAFA]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Global
            </button>
            <button
              type="button"
              data-testid="graph-scope-local"
              onClick={() => setScopeMode("local")}
              className={`min-h-6 rounded px-2 py-0.5 text-[11px] font-medium ${
                scopeMode === "local"
                  ? "bg-[#18181B] text-[#FAFAFA]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Local
            </button>
          </div>

          {scopeMode === "local" && (
            <div className="flex items-center gap-1">
              {([1, 2] as const).map((h) => (
                <button
                  key={h}
                  type="button"
                  data-testid={`graph-hops-${h}`}
                  onClick={() => setHops(h)}
                  className={`h-6 w-6 rounded border font-mono text-[11px] tabular-nums ${
                    hops === h
                      ? "border-white/[0.2] bg-[#18181B] text-[#FAFAFA]"
                      : "border-white/[0.08] text-[#A1A1AA]"
                  }`}
                >
                  {h}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Diagnostic Interactive SVG Canvas */}
      <div
        ref={containerRef}
        data-testid="graph-viewport"
        data-lod={lod}
        data-direction-focus={directionFocusAttr}
        className="relative flex-1 overflow-hidden bg-[#0A0A0B]"
      >
        {/* Compact Floating Sticky Focus Pill */}
        {focusedNode && (
          <div
            data-testid="graph-focus-pill"
            className="absolute left-1/2 top-3 z-20 flex max-w-[96%] -translate-x-1/2 items-center gap-1 whitespace-nowrap rounded-full border border-white/[0.12] bg-[#111113]/95 px-2.5 py-1 text-xs shadow-xl backdrop-blur"
          >
            <span className="max-w-[155px] shrink-0 truncate pr-1 font-medium text-[#FAFAFA]">
              {focusedNode.label}
            </span>
            <button
              type="button"
              data-testid="focus-dir-reverse"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(true);
                onChangeFocus(focusedNode.id, "in");
              }}
              className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
                stickyDirectionActive && focusDirection === "in" && !impactTrace
                  ? "bg-[#38BDF8]/20 text-[#38BDF8]"
                  : "text-[#A1A1AA] hover:text-[#38BDF8]"
              }`}
            >
              ← Upstream
            </button>
            <button
              type="button"
              data-testid="focus-dir-both"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(true);
                onChangeFocus(focusedNode.id, "both");
              }}
              className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
                stickyDirectionActive && focusDirection === "both" && !impactTrace
                  ? "bg-[#18181B] text-[#FAFAFA]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Both
            </button>
            <button
              type="button"
              data-testid="focus-dir-forward"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(true);
                onChangeFocus(focusedNode.id, "out");
              }}
              className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
                stickyDirectionActive && focusDirection === "out" && !impactTrace
                  ? "bg-[#F59E0B]/20 text-[#FBBF24]"
                  : "text-[#A1A1AA] hover:text-[#FBBF24]"
              }`}
            >
              Downstream →
            </button>
            <button
              type="button"
              data-testid="focus-impact-bfs"
              onClick={() => runImpactBfs(focusedNode.id)}
              className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
                impactTrace
                  ? "bg-[#FAFAFA] text-[#0A0A0B]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Impact BFS
            </button>
            <button
              type="button"
              data-testid="focus-clear-btn"
              aria-label="Clear graph focus"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(false);
                onChangeFocus(null, "both");
                fitToView();
              }}
              className="inline-flex min-h-6 min-w-6 shrink-0 items-center justify-center rounded-full text-[#94949E] hover:bg-white/[0.08] hover:text-[#FAFAFA]"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}

        <svg
          width="100%"
          height="100%"
          role="group"
          aria-label={`Diagnostic Context Graph (${lens} lens)`}
          data-testid="graph-svg"
          onPointerDown={handleCanvasPointerDown}
          onPointerMove={handleSvgPointerMove}
          onPointerUp={handleSvgPointerUp}
          onWheel={handleWheel}
          className="h-full w-full cursor-grab select-none active:cursor-grabbing"
        >
          <defs>
            <pattern
              id="obsidian-dot-grid"
              width="28"
              height="28"
              patternUnits="userSpaceOnUse"
            >
              <circle cx="1" cy="1" r="0.85" fill="rgba(255,255,255,0.055)" />
            </pattern>
            <marker
              id="arrow-default"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5.5"
              markerHeight="5.5"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="rgba(255,255,255,0.34)" />
            </marker>
            <marker
              id="arrow-upstream"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#38BDF8" />
            </marker>
            <marker
              id="arrow-downstream"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#F59E0B" />
            </marker>
            <marker
              id="arrow-conflict"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#F43F5E" />
            </marker>
          </defs>

          <rect width="100%" height="100%" fill="url(#obsidian-dot-grid)" />

          <g
            transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}
          >
            {/* Golden-angle orphan perimeter guide ring */}
            {showOrphanRing &&
              visibleNodes.some((n) => layout.positions[n.id]?.isOrphan) && (
                <g data-testid="graph-orphan-ring" pointerEvents="none">
                  <circle
                    cx={0}
                    cy={0}
                    r={layout.orphanRingRadius}
                    fill="none"
                    stroke="rgba(255,255,255,0.09)"
                    strokeWidth={1}
                    strokeDasharray="4 6"
                  />
                  <text
                    x={0}
                    y={-layout.orphanRingRadius - 10}
                    textAnchor="middle"
                    fill="#94949E"
                    fontSize={10}
                    fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                  >
                    ORPHAN PERIMETER · 137.5°
                  </text>
                </g>
              )}

            {/* Compound Cluster Hulls */}
            {showHulls &&
              layout.hulls.map((hull) => (
                <g
                  key={hull.id}
                  data-testid={`graph-cluster-${hull.id}`}
                  pointerEvents="none"
                >
                  <rect
                    x={hull.x}
                    y={hull.y}
                    width={hull.width}
                    height={hull.height}
                    rx={16}
                    fill="rgba(255,255,255,0.022)"
                    stroke="rgba(255,255,255,0.08)"
                    strokeWidth={1}
                    pointerEvents="none"
                  />
                  <text
                    x={hull.x + 12}
                    y={hull.y + 18}
                    fill="#A1A1AA"
                    fontSize={10.5}
                    fontWeight={500}
                    fontFamily="Inter, -apple-system, sans-serif"
                    pointerEvents="none"
                  >
                    {hull.label}
                  </text>
                </g>
              ))}

            {/* Directed Edges */}
            {visibleEdges.map((edge) => {
              const s = layout.positions[edge.source];
              const t = layout.positions[edge.target];
              if (!s || !t) return null;

              const vis = computeEdgeVisual(edge);
              const dx = t.x - s.x;
              const dy = t.y - s.y;
              const dist = Math.hypot(dx, dy) || 1;
              const targetR = 13;
              const sourceR = 11;
              const x1 = s.x + (dx / dist) * sourceR;
              const y1 = s.y + (dy / dist) * sourceR;
              const x2 = t.x - (dx / dist) * targetR;
              const y2 = t.y - (dy / dist) * targetR;

              // Staggered quadratic Bézier offset prevents converging arrows & pills from stacking
              const stagger = edgeLabelLayout.get(edge.id) || {
                t: 0.5,
                curveSign: 1,
              };
              const curveAmp = Math.min(24, dist * 0.09) * stagger.curveSign;
              const mx = (x1 + x2) * 0.5 - (dy / dist) * curveAmp;
              const my = (y1 + y2) * 0.5 + (dx / dist) * curveAmp;

              const u = 1 - stagger.t;
              const lx = u * u * x1 + 2 * u * stagger.t * mx + stagger.t * stagger.t * x2;
              const ly = u * u * y1 + 2 * u * stagger.t * my + stagger.t * stagger.t * y2;

              return (
                <g
                  key={edge.id}
                  data-testid={`graph-edge-${edge.id}`}
                  pointerEvents="none"
                  style={{ opacity: vis.opacity, transition: "opacity 160ms ease" }}
                >
                  <path
                    d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
                    fill="none"
                    stroke={vis.stroke}
                    strokeWidth={vis.strokeWidth}
                    strokeDasharray={vis.dashed ? "4 3" : undefined}
                    markerEnd={vis.marker}
                  />
                  {vis.showLabel && edge.label && (
                    <g transform={`translate(${lx}, ${ly})`}>
                      <rect
                        x={-edge.label.length * 3.1 - 5}
                        y={-8}
                        width={edge.label.length * 6.2 + 10}
                        height={15}
                        rx={3}
                        fill="#0A0A0B"
                        fillOpacity={0.92}
                        stroke="rgba(255,255,255,0.08)"
                        strokeWidth={0.75}
                      />
                      <text
                        x={0}
                        y={3}
                        textAnchor="middle"
                        fill="#F4F4F5"
                        fontSize={9.5}
                        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                      >
                        {edge.label}
                      </text>
                    </g>
                  )}
                </g>
              );
            })}

            {/* Interactive Nodes */}
            {visibleNodes.map((node) => {
              const pos = layout.positions[node.id];
              if (!pos) return null;

              const vis = computeNodeVisualState(node);
              const r = nodeRadius(node);
              const stroke = nodeStrokeColor(node);
              const isSelected =
                selectedEntityId === node.id ||
                selectedEntityId === node.canonical_key ||
                focusNodeId === node.id;
              const isPinned = pos.pinned;

              return (
                <g
                  key={node.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`${node.label} (${formatEntityType(node.entity_type)})`}
                  data-testid={`graph-node-${node.id}`}
                  data-node-id={node.id}
                  data-canonical-key={node.canonical_key}
                  data-node-spotlight={vis.tier}
                  data-node-orphan={pos.isOrphan ? "true" : "false"}
                  data-node-pinned={isPinned ? "true" : "false"}
                  data-node-conflict={node.has_conflict ? "true" : "false"}
                  transform={`translate(${pos.x}, ${pos.y})`}
                  style={{
                    opacity: vis.opacity,
                    transition: "opacity 160ms ease",
                    cursor: "pointer",
                  }}
                  onPointerEnter={() => handleNodePointerEnter(node.id)}
                  onPointerLeave={handleNodePointerLeave}
                  onPointerDown={(e) => handleNodePointerDown(e, node)}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (suppressClickRef.current) {
                      suppressClickRef.current = false;
                      return;
                    }
                    onChangeFocus(node.id, focusDirection);
                    onSelectEntity(node.canonical_key || node.id);
                  }}
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    if (isPinned) unpinNode(node.id);
                  }}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const rect = containerRef.current?.getBoundingClientRect();
                    setContextMenu({
                      node,
                      x: e.clientX - (rect?.left || 0),
                      y: e.clientY - (rect?.top || 0),
                    });
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onChangeFocus(node.id, focusDirection);
                      onSelectEntity(node.canonical_key || node.id);
                    }
                  }}
                >
                  {/* Generous centered transparent hit target */}
                  <circle r={Math.max(15, r + 6)} fill="transparent" />

                  {/* Selection / Conflict subtle outer ring */}
                  {(isSelected || node.has_conflict) && (
                    <circle
                      r={r + 4.5}
                      fill="none"
                      stroke={node.has_conflict ? "#F43F5E" : "#FAFAFA"}
                      strokeWidth={isSelected ? 1.5 : 1.1}
                      strokeOpacity={isSelected ? 0.9 : 0.65}
                    />
                  )}

                  {/* Core node circle */}
                  <circle
                    r={r}
                    fill={isSelected ? "#202024" : "#111113"}
                    stroke={stroke}
                    strokeWidth={isSelected ? 2 : 1.5}
                  />

                  {/* Center dot */}
                  <circle
                    r={2.5}
                    fill={node.has_conflict ? "#F43F5E" : stroke}
                  />

                  {/* Pin badge indicator */}
                  {isPinned && (
                    <circle
                      cx={r - 1}
                      cy={-r + 1}
                      r={3}
                      fill="#FAFAFA"
                      stroke="#0A0A0B"
                      strokeWidth={1}
                    />
                  )}
                </g>
              );
            })}

            {/* Dedicated non-interactive SVG label layer (above nodes, zero WCAG 2.5.3 mismatch) */}
            <g
              data-testid="graph-labels-layer"
              pointerEvents="none"
              aria-hidden="true"
            >
              {visibleNodes.map((node) => {
                const pos = layout.positions[node.id];
                if (!pos) return null;
                const vis = computeNodeVisualState(node);
                const r = nodeRadius(node);
                const isSelected =
                  selectedEntityId === node.id ||
                  selectedEntityId === node.canonical_key ||
                  focusNodeId === node.id;
                const showLabel =
                  lod !== "cluster" ||
                  node.has_conflict ||
                  isSelected ||
                  vis.tier === "0" ||
                  vis.tier === "1";
                if (!showLabel) return null;

                const shortLabel =
                  node.label.length > 26
                    ? `${node.label.slice(0, 25)}…`
                    : node.label;
                const labelW = Math.max(54, shortLabel.length * 6.1 + 12);

                return (
                  <g
                    key={`lbl-${node.id}`}
                    transform={`translate(${pos.x}, ${pos.y + r + 13})`}
                    style={{
                      opacity: vis.opacity,
                      transition: "opacity 160ms ease",
                    }}
                  >
                    <rect
                      x={-labelW * 0.5}
                      y={-9}
                      width={labelW}
                      height={16}
                      rx={4}
                      fill="#0A0A0B"
                      fillOpacity={0.88}
                    />
                    <text
                      x={0}
                      y={2.5}
                      textAnchor="middle"
                      fill="#F4F4F5"
                      fontSize={10.5}
                      fontWeight={isSelected ? 600 : 400}
                      fontFamily="Inter, -apple-system, sans-serif"
                    >
                      {shortLabel}
                    </text>
                    {lod === "detail" && (
                      <text
                        x={0}
                        y={16}
                        textAnchor="middle"
                        fill="#94949E"
                        fontSize={9}
                        fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
                      >
                        {node.canonical_key}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          </g>
        </svg>

        {/* Right-click context menu */}
        {contextMenu && (
          <div
            role="menu"
            data-testid="graph-context-menu"
            style={{
              left: Math.min(contextMenu.x, viewportSize.width - 210),
              top: Math.min(contextMenu.y, viewportSize.height - 230),
            }}
            onMouseDown={(e) => e.stopPropagation()}
            className="absolute z-40 w-52 rounded-lg border border-white/[0.1] bg-[#111113] p-1 text-xs shadow-2xl"
          >
            <div className="truncate border-b border-white/[0.08] px-2.5 py-1.5 font-medium text-[#FAFAFA]">
              {contextMenu.node.label}
            </div>
            <button
              type="button"
              role="menuitem"
              data-testid="ctx-reverse-impact"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(true);
                onChangeFocus(contextMenu.node.id, "in");
                setContextMenu(null);
              }}
              className="flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-[#38BDF8] hover:bg-white/[0.06]"
            >
              <span>Reverse Impact</span>
              <span className="font-mono text-[10px]">←</span>
            </button>
            <button
              type="button"
              role="menuitem"
              data-testid="ctx-forward-impact"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(true);
                onChangeFocus(contextMenu.node.id, "out");
                setContextMenu(null);
              }}
              className="flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-[#FBBF24] hover:bg-white/[0.06]"
            >
              <span>Forward Impact</span>
              <span className="font-mono text-[10px]">→</span>
            </button>
            <button
              type="button"
              role="menuitem"
              data-testid="ctx-impact-bfs"
              onClick={() => {
                void runImpactBfs(contextMenu.node.id);
                setContextMenu(null);
              }}
              className="flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-[#F4F4F5] hover:bg-white/[0.06]"
            >
              <span>Trace Impact BFS</span>
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                const pos = layout.positions[contextMenu.node.id];
                if (pos?.pinned) {
                  unpinNode(contextMenu.node.id);
                } else if (pos) {
                  pinNode(contextMenu.node.id, { x: pos.x, y: pos.y });
                }
                setContextMenu(null);
              }}
              className="flex w-full items-center gap-1.5 rounded px-2.5 py-1.5 text-left text-[#F4F4F5] hover:bg-white/[0.06]"
            >
              <Pin className="h-3 w-3 text-[#A1A1AA]" />
              <span>
                {layout.positions[contextMenu.node.id]?.pinned
                  ? "Unpin Position"
                  : "Pin Position"}
              </span>
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                onSelectEntity(
                  contextMenu.node.canonical_key || contextMenu.node.id,
                );
                setContextMenu(null);
              }}
              className="flex w-full items-center rounded px-2.5 py-1.5 text-left text-[#F4F4F5] hover:bg-white/[0.06]"
            >
              Open Inspector
            </button>
            <button
              type="button"
              role="menuitem"
              data-testid="ctx-clear-focus"
              onClick={() => {
                setImpactTrace(null);
                setStickyDirectionActive(false);
                onChangeFocus(null, "both");
                setContextMenu(null);
                fitToView();
              }}
              className="mt-0.5 flex w-full items-center rounded border-t border-white/[0.08] px-2.5 py-1.5 text-left text-[#A1A1AA] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
            >
              Clear Focus
            </button>
          </div>
        )}

        {/* Quiet bottom-left readout: hovered node metrics OR compact graph stats */}
        <div className="pointer-events-none absolute bottom-3 left-3 z-10">
          {hoveredNode ? (
            <div
              data-testid="graph-hover-readout"
              className="rounded-lg border border-white/[0.1] bg-[#111113]/95 px-3 py-2 text-xs shadow-xl backdrop-blur"
            >
              <div className="font-medium text-[#FAFAFA]">
                {hoveredNode.label}
              </div>
              <div className="mt-0.5 flex items-center gap-2 font-mono text-[11px] tabular-nums text-[#A1A1AA]">
                <span>{formatEntityType(hoveredNode.entity_type)}</span>
                <span>·</span>
                <span className="text-[#38BDF8]">
                  ←{hoveredNode.degree_in}
                </span>
                <span className="text-[#FBBF24]">
                  {hoveredNode.degree_out}→
                </span>
                {hoveredNode.has_conflict && (
                  <span className="text-[#FB7185]">
                    {pluralize(hoveredNode.open_conflict_count || 1, "conflict")}
                  </span>
                )}
              </div>
            </div>
          ) : (
            <div
              data-testid="graph-stats"
              className="flex items-center gap-3 rounded-md border border-white/[0.08] bg-[#111113]/90 px-2.5 py-1 font-mono text-[11px] tabular-nums text-[#A1A1AA]"
            >
              <span>{pluralize(visibleNodes.length, "node")}</span>
              <span>{pluralize(visibleEdges.length, "edge")}</span>
              <span>
                {pluralize(
                  visibleNodes.filter((n) => layout.positions[n.id]?.isOrphan).length,
                  "orphan",
                )}
              </span>
              <span className="text-[#94949E]">LOD:{lod}</span>
              {loading && <span>syncing…</span>}
              {layoutPending && <span data-testid="graph-layout-pending">laying out…</span>}
            </div>
          )}
        </div>

        {/* Bottom-right crisp zoom controls */}
        <div className="absolute bottom-3 right-3 z-10 flex items-center gap-1 rounded-md border border-white/[0.08] bg-[#111113]/95 p-1">
          <button
            type="button"
            aria-label="Zoom out"
            data-testid="graph-zoom-out"
            onClick={() => adjustZoom(0.82)}
            className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[#A1A1AA] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
          >
            <Minus className="h-3.5 w-3.5" />
          </button>
          <span
            data-testid="graph-zoom-pct"
            className="min-w-[42px] text-center font-mono text-[11px] tabular-nums text-[#F4F4F5]"
          >
            {Math.round(transform.scale * 100)}%
          </span>
          <button
            type="button"
            aria-label="Zoom in"
            data-testid="graph-zoom-in"
            onClick={() => adjustZoom(1.22)}
            className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[#A1A1AA] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            aria-label="Fit graph to viewport"
            data-testid="graph-zoom-fit"
            onClick={fitToView}
            className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[#A1A1AA] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
          >
            <Maximize2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
