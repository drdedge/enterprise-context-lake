import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  Edit3,
  GitBranch,
  Hash,
  Lock,
  Network,
  ShieldAlert,
  X,
} from "lucide-react";
import { api, ApiError } from "../api/client";
import { HistoryPanel } from "./HistoryPanel";
import type {
  AuthContextState,
  ClaimDTO,
  ConflictSetDTO,
  EntityDetailDTO,
  ImpactResponseDTO,
  PrincipalMeDTO,
  TemporalSnapshot,
} from "../types/api";
import {
  formatEntityType,
  formatReceiptShort,
  formatShortDate,
  formatWallSideLabel,
  pluralize,
} from "../utils/platform";

interface InspectorDrawerProps {
  entityIdOrKey: string | null;
  auth: AuthContextState;
  me: PrincipalMeDTO | null;
  snapshot: TemporalSnapshot;
  dataEpoch: number;
  onClose: () => void;
  onSelectEntity: (idOrKey: string) => void;
  onFocusInGraph: (entityId: string, direction?: "both" | "in" | "out") => void;
  onSelectTopic: (slug: string) => void;
  onMutated: () => void;
}

export function InspectorDrawer({
  entityIdOrKey,
  auth,
  me,
  snapshot,
  dataEpoch,
  onClose,
  onSelectEntity,
  onFocusInGraph,
  onSelectTopic,
  onMutated,
}: InspectorDrawerProps) {
  const [detail, setDetail] = useState<EntityDetailDTO | null>(null);
  const [impact, setImpact] = useState<ImpactResponseDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Conflict resolution state
  const [selectedWinClaim, setSelectedWinClaim] = useState<
    Record<string, string>
  >({});
  const [rationaleMap, setRationaleMap] = useState<Record<string, string>>({});
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  // Inline claim edit / proposal state
  const [editingClaimId, setEditingClaimId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [editRationale, setEditRationale] = useState("");
  const [proposingConflictId, setProposingConflictId] = useState<string | null>(
    null,
  );
  const [proposalValue, setProposalValue] = useState("");

  useEffect(() => {
    if (!entityIdOrKey) {
      setDetail(null);
      setImpact(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setActionMsg(null);
    setEditingClaimId(null);
    setProposingConflictId(null);

    api
      .getEntityDetail(auth, entityIdOrKey, {
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
      })
      .then((res) => {
        if (cancelled) return;
        setDetail(res);
        // Pre-select first claim for each open conflict
        const defaults: Record<string, string> = {};
        for (const c of res.conflicts || []) {
          if (c.claims && c.claims.length > 0) {
            defaults[c.conflict_set_id] = c.claims[0].claim_id;
          } else if (c.claim_ids && c.claim_ids.length > 0) {
            defaults[c.conflict_set_id] = c.claim_ids[0];
          }
        }
        setSelectedWinClaim(defaults);

        // Load reverse impact blast radius for this entity
        api
          .getImpact(auth, res.entity.entity_id, {
            max_depth: 4,
            as_of: snapshot.asOf,
            recorded_as_of: snapshot.recordedAsOf,
          })
          .then((imp) => {
            if (!cancelled) setImpact(imp);
          })
          .catch(() => {
            if (!cancelled) setImpact(null);
          });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setError("Record not found or not authorized.");
        } else {
          setError(err instanceof Error ? err.message : "Failed to load entity.");
        }
        setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [entityIdOrKey, auth, snapshot, dataEpoch]);

  if (!entityIdOrKey) return null;

  const isSteward = Boolean(
    me?.roles?.includes("fund_steward") ||
      me?.roles?.includes("compliance_reviewer") ||
      me?.groups?.includes("fund-stewards") ||
      me?.groups?.includes("compliance-oversight"),
  );

  const handleResolveConflict = async (conflict: ConflictSetDTO) => {
    const winId = selectedWinClaim[conflict.conflict_set_id];
    const rationale =
      (rationaleMap[conflict.conflict_set_id] || "").trim() ||
      "Steward reconciled competing sources against authoritative evidence.";
    setResolvingId(conflict.conflict_set_id);
    setActionMsg(null);
    try {
      await api.resolveConflict(auth, conflict.conflict_set_id, {
        winning_claim_id: winId,
        rationale,
        expected_data_epoch: dataEpoch,
      });
      setActionMsg({ type: "ok", text: "Conflict resolved & approved." });
      onMutated();
    } catch (err: unknown) {
      const msg =
        err instanceof ApiError
          ? `${err.status} ${err.code}: ${err.message}`
          : "Resolution failed.";
      setActionMsg({ type: "err", text: msg });
    } finally {
      setResolvingId(null);
    }
  };

  const handleProposeCorrection = async (conflict: ConflictSetDTO) => {
    if (!detail || !proposalValue.trim()) return;
    setActionMsg(null);
    try {
      await api.createClaim(auth, {
        subject_canonical_key: detail.entity.canonical_key,
        predicate: conflict.predicate,
        object_value: proposalValue.trim(),
        context_purpose: conflict.context_purpose || "general",
        scope_type: "firm",
        scope_id: "firm",
        channel: "fact",
        epistemic_status: "unverified_observation",
        source_system: "web-ui-proposal",
      });
      setProposingConflictId(null);
      setProposalValue("");
      setActionMsg({
        type: "ok",
        text: "Proposal queued in quarantine inbox.",
      });
      onMutated();
    } catch (err: unknown) {
      setActionMsg({
        type: "err",
        text: err instanceof Error ? err.message : "Proposal failed.",
      });
    }
  };

  const handleSaveClaimEdit = async (claim: ClaimDTO) => {
    if (!editValue.trim()) return;
    setActionMsg(null);
    try {
      await api.patchClaim(auth, claim.claim_id, {
        object_value: editValue.trim(),
        rationale: editRationale.trim() || "Steward inline bitemporal update",
        force_supersede: true,
        expected_data_epoch: dataEpoch,
      });
      setEditingClaimId(null);
      setEditValue("");
      setEditRationale("");
      setActionMsg({ type: "ok", text: "Claim superseded cleanly." });
      onMutated();
    } catch (err: unknown) {
      setActionMsg({
        type: "err",
        text: err instanceof Error ? err.message : "Update failed.",
      });
    }
  };

  const renderProvenance = (claim: ClaimDTO) => {
    if (claim.evidence_redacted) {
      return (
        <div
          data-testid="redacted-evidence-stub"
          className="mt-1.5 flex items-center justify-between gap-2 rounded border border-white/[0.1] bg-[#0A0A0B] px-2 py-1 text-[11px] text-[#A1A1AA]"
        >
          <span className="flex items-center gap-1.5 font-medium text-[#F4F4F5]">
            <Lock className="h-3 w-3 shrink-0 text-[#94949E]" />
            <span>
              {claim.evidence_redaction_label || "Restricted — Request Access"}
            </span>
          </span>
          {claim.evidence_request_group && (
            <span className="shrink-0 font-mono text-[10px] text-[#94949E]">
              {claim.evidence_request_group}
            </span>
          )}
        </div>
      );
    }

    if (!claim.provenance?.evidence_uri && !claim.provenance?.excerpt) {
      return null;
    }

    return (
      <div className="mt-1.5 rounded border border-white/[0.06] bg-[#0A0A0B] px-2 py-1 text-[11px]">
        {claim.provenance.evidence_uri && (
          <div className="truncate font-mono text-[#38BDF8]">
            {claim.provenance.evidence_uri}
            {claim.provenance.span_locator
              ? ` · ${claim.provenance.span_locator}`
              : ""}
          </div>
        )}
        {claim.provenance.excerpt && (
          <div className="mt-0.5 line-clamp-2 text-[#A1A1AA]">
            {claim.provenance.excerpt}
          </div>
        )}
      </div>
    );
  };

  // Resolve competing ClaimDTOs for each conflict set
  const resolveConflictClaims = (conflict: ConflictSetDTO): ClaimDTO[] => {
    if (conflict.claims && conflict.claims.length > 0) return conflict.claims;
    if (!detail) return [];
    const byId = new Map(
      [...detail.facts, ...detail.directives].map((c) => [c.claim_id, c]),
    );
    return (conflict.claim_ids || [])
      .map((id) => byId.get(id))
      .filter((c): c is ClaimDTO => Boolean(c));
  };

  return (
    <aside
      aria-label="Entity Inspector"
      data-testid="inspector-drawer"
      className="flex w-[420px] shrink-0 flex-col border-l border-white/[0.08] bg-[#111113] xl:w-[460px]"
    >
      {/* Inspector Header */}
      <div className="flex items-start justify-between gap-2 border-b border-white/[0.08] px-4 py-3">
        <div className="min-w-0">
          {detail ? (
            <>
              <div className="flex items-center gap-2">
                <h2 className="truncate text-sm font-semibold text-[#FAFAFA]">
                  {detail.entity.display_name}
                </h2>
                {detail.entity.wall_side === "private_side" && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded border border-[#F59E0B]/30 bg-[#18181B] px-1.5 py-0.5 font-mono text-[10px] text-[#FBBF24]">
                    <Lock className="h-2.5 w-2.5" />
                    MNPI
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[11px] text-[#94949E]">
                <span>{detail.entity.canonical_key}</span>
                <span>·</span>
                <span>{formatEntityType(detail.entity.entity_type)}</span>
                <span>·</span>
                <span>{formatWallSideLabel(detail.entity.wall_side)}</span>
              </div>
            </>
          ) : (
            <div className="text-sm font-medium text-[#A1A1AA]">
              {loading ? "Loading…" : "Inspector"}
            </div>
          )}
        </div>

        <button
          type="button"
          aria-label="Close inspector"
          data-testid="inspector-close"
          onClick={onClose}
          className="rounded p-1 text-[#94949E] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Action strip */}
      {detail && (
        <div className="flex items-center justify-between gap-2 border-b border-white/[0.08] bg-[#0A0A0B] px-4 py-1.5">
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              data-testid="inspector-focus-graph"
              onClick={() => onFocusInGraph(detail.entity.entity_id, "both")}
              className="flex items-center gap-1 rounded border border-white/[0.08] bg-[#111113] px-2 py-1 text-[11px] font-medium text-[#F4F4F5] hover:border-white/[0.2]"
            >
              <Network className="h-3 w-3 text-[#A1A1AA]" />
              Graph Focus
            </button>
            <button
              type="button"
              data-testid="inspector-reverse-impact"
              onClick={() => onFocusInGraph(detail.entity.entity_id, "in")}
              className="flex items-center gap-1 rounded border border-[#38BDF8]/30 bg-[#111113] px-2 py-1 text-[11px] font-medium text-[#38BDF8] hover:border-[#38BDF8]/60"
            >
              ← Reverse Impact
            </button>
          </div>
          <span className="font-mono text-[10px] tabular-nums text-[#94949E]">
            {formatReceiptShort(detail.receipt_id)}
          </span>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        {actionMsg && (
          <div
            role="status"
            data-testid="inspector-action-status"
            className={`rounded-md border px-3 py-2 text-xs ${
              actionMsg.type === "ok"
                ? "border-[#34D399]/30 bg-[#0A221A] text-[#34D399]"
                : "border-[#F43F5E]/40 bg-[#2A1219] text-[#FB7185]"
            }`}
          >
            {actionMsg.text}
          </div>
        )}

        {error && (
          <div className="rounded-md border border-white/[0.08] bg-[#18181B] px-3 py-4 text-center text-[#A1A1AA]">
            {error}
          </div>
        )}

        {loading && !detail && (
          <div className="space-y-3">
            <div className="h-16 animate-pulse rounded-lg bg-[#18181B]" />
            <div className="h-28 animate-pulse rounded-lg bg-[#18181B]" />
          </div>
        )}

        {detail && (
          <>
            {/* Open N-Way Conflicts — surfaced first */}
            {detail.conflicts.filter((c) => c.status === "open").map((conflict) => {
              const claims = resolveConflictClaims(conflict);
              const blastSkills =
                conflict.affected_skills || detail.skill_bindings || [];
              const impactLayers = impact?.layers;

              return (
                <section
                  key={conflict.conflict_set_id}
                  data-testid={`conflict-card-${conflict.conflict_set_id}`}
                  aria-label="Open N-Way Conflict"
                  className="rounded-lg border border-[#F43F5E]/35 bg-[#18181B] p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5 font-medium text-[#FB7185]">
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                      <span>
                        {claims.length}-Way Conflict · {conflict.predicate}
                      </span>
                    </div>
                    <span className="rounded border border-white/[0.08] bg-[#111113] px-1.5 py-0.5 font-mono text-[10px] text-[#A1A1AA]">
                      {conflict.context_purpose}
                    </span>
                  </div>

                  {/* Competing sources side-by-side */}
                  <div className="mt-2.5 space-y-2">
                    {claims.map((claim) => {
                      const picked =
                        selectedWinClaim[conflict.conflict_set_id] ===
                        claim.claim_id;
                      const isApproved =
                        claim.epistemic_status === "approved_fact";

                      return (
                        <div
                          key={claim.claim_id}
                          data-testid={`conflict-claim-${claim.claim_id}`}
                          className={`rounded-md border p-2.5 transition-colors ${
                            picked && isSteward
                              ? "border-white/[0.3] bg-[#111113]"
                              : "border-white/[0.08] bg-[#111113]/70"
                          }`}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <label className="flex cursor-pointer items-center gap-2">
                              {isSteward && (
                                <input
                                  type="radio"
                                  name={`win-${conflict.conflict_set_id}`}
                                  checked={picked}
                                  onChange={() =>
                                    setSelectedWinClaim((prev) => ({
                                      ...prev,
                                      [conflict.conflict_set_id]: claim.claim_id,
                                    }))
                                  }
                                  className="accent-white"
                                />
                              )}
                              <span className="font-mono text-sm font-semibold tabular-nums text-[#FAFAFA]">
                                {claim.object_value}
                              </span>
                            </label>
                            <span
                              className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] ${
                                isApproved
                                  ? "bg-[#0A221A] text-[#34D399]"
                                  : "bg-[#2A1219] text-[#FB7185]"
                              }`}
                            >
                              {claim.epistemic_status}
                            </span>
                          </div>

                          <div className="mt-1 flex items-center justify-between font-mono text-[11px] text-[#94949E]">
                            <span>{claim.provenance?.source_system}</span>
                            <span className="tabular-nums">
                              {formatShortDate(claim.valid_from)} →{" "}
                              {formatShortDate(claim.valid_to)}
                            </span>
                          </div>

                          {renderProvenance(claim)}
                        </div>
                      );
                    })}
                  </div>

                  {/* Skill Bindings & Blast Radius Preview */}
                  <div
                    data-testid="blast-radius-panel"
                    className="mt-2.5 rounded-md border border-white/[0.08] bg-[#0A0A0B] p-2.5"
                  >
                    <div className="flex items-center justify-between text-[11px] font-medium text-[#FBBF24]">
                      <span className="flex items-center gap-1">
                        <ShieldAlert className="h-3 w-3" />
                        Blast Radius
                      </span>
                      <span className="font-mono tabular-nums text-[#A1A1AA]">
                        {pluralize(blastSkills.length, "skill")}
                        {impact ? ` · ${impact.total_affected} affected` : ""}
                      </span>
                    </div>

                    {blastSkills.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {blastSkills.map((b) => (
                          <button
                            key={b.binding_id}
                            type="button"
                            onClick={() => onSelectEntity(b.skill_entity_id)}
                            className="rounded border border-white/[0.08] bg-[#18181B] px-1.5 py-0.5 font-mono text-[10px] text-[#F4F4F5] hover:border-white/[0.2]"
                          >
                            {b.skill_name || b.skill_canonical_key || b.skill_entity_id} ·{" "}
                            {b.mapping_purpose}
                          </button>
                        ))}
                      </div>
                    )}

                    {impactLayers && (
                      <div className="mt-1.5 flex flex-wrap gap-3 font-mono text-[10px] tabular-nums text-[#94949E]">
                        <span>{pluralize(impactLayers.funds.length, "fund")}</span>
                        <span>
                          {pluralize(impactLayers.positions.length, "position")}
                        </span>
                        <span>
                          {pluralize(
                            impactLayers.clients_opportunities.length,
                            "client/opp",
                            "clients/opps",
                          )}
                        </span>
                        <span>
                          {pluralize(
                            impactLayers.skills_apps.length,
                            "skill/app",
                            "skills/apps",
                          )}
                        </span>
                      </div>
                    )}
                  </div>

                  {/* Resolution / Proposal Controls */}
                  {isSteward ? (
                    <div className="mt-2.5 space-y-2">
                      <input
                        type="text"
                        value={rationaleMap[conflict.conflict_set_id] || ""}
                        onChange={(e) =>
                          setRationaleMap((prev) => ({
                            ...prev,
                            [conflict.conflict_set_id]: e.target.value,
                          }))
                        }
                        placeholder="Resolution rationale…"
                        aria-label="Resolution rationale"
                        data-testid="resolve-rationale-input"
                        className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2.5 py-1.5 text-xs text-[#FAFAFA] placeholder-[#94949E]"
                      />
                      <button
                        type="button"
                        data-testid="resolve-conflict-btn"
                        disabled={resolvingId === conflict.conflict_set_id}
                        onClick={() => handleResolveConflict(conflict)}
                        className="flex w-full items-center justify-center gap-1.5 rounded-md bg-[#FAFAFA] py-1.5 text-xs font-medium text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-50"
                      >
                        <Check className="h-3.5 w-3.5" />
                        <span>
                          {resolvingId === conflict.conflict_set_id
                            ? "Resolving…"
                            : "Resolve & Approve"}
                        </span>
                      </button>
                    </div>
                  ) : (
                    <div className="mt-2.5">
                      {proposingConflictId === conflict.conflict_set_id ? (
                        <div className="flex gap-1.5">
                          <input
                            type="text"
                            value={proposalValue}
                            onChange={(e) => setProposalValue(e.target.value)}
                            placeholder="Proposed value (quarantined)"
                            aria-label="Proposed correction value"
                            className="flex-1 rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA] placeholder-[#94949E]"
                          />
                          <button
                            type="button"
                            onClick={() => handleProposeCorrection(conflict)}
                            className="rounded bg-[#FAFAFA] px-2.5 py-1 text-xs font-medium text-[#0A0A0B]"
                          >
                            Submit
                          </button>
                        </div>
                      ) : (
                        <button
                          type="button"
                          data-testid="propose-correction-btn"
                          onClick={() =>
                            setProposingConflictId(conflict.conflict_set_id)
                          }
                          className="w-full rounded-md border border-white/[0.12] bg-[#111113] py-1.5 text-xs font-medium text-[#F4F4F5] hover:border-white/[0.24]"
                        >
                          Propose Correction
                        </button>
                      )}
                    </div>
                  )}
                </section>
              );
            })}

            {/* Identity: Description, Aliases, Crosswalk Identifiers, Lineage */}
            <section className="space-y-2.5 border-b border-white/[0.08] pb-3.5">
              {detail.entity.description && (
                <p className="leading-relaxed text-[#A1A1AA]">
                  {detail.entity.description}
                </p>
              )}

              {detail.entity.aliases.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {detail.entity.aliases.map((alias) => (
                    <span
                      key={alias}
                      className="rounded border border-white/[0.08] bg-[#18181B] px-1.5 py-0.5 text-[11px] text-[#A1A1AA]"
                    >
                      {alias}
                    </span>
                  ))}
                </div>
              )}

              {detail.identifiers.length > 0 && (
                <div>
                  <h3 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                    Identifiers
                  </h3>
                  <div className="space-y-1">
                    {detail.identifiers.map((id, i) => (
                      <div
                        key={id.identifier_id || `${id.scheme}-${id.identifier_value}-${i}`}
                        className="flex items-center justify-between rounded bg-[#0A0A0B] px-2 py-1 font-mono text-[11px]"
                      >
                        <span className="uppercase text-[#94949E]">
                          {id.scheme}
                        </span>
                        <span className="tabular-nums text-[#FAFAFA]">
                          {id.identifier_value}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {detail.lineage.length > 0 && (
                <div>
                  <h3 className="mb-1.5 flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                    <GitBranch className="h-3 w-3" />
                    Lineage
                  </h3>
                  <div className="space-y-1.5">
                    {detail.lineage.map((ev) => (
                      <div
                        key={ev.event_id}
                        className="rounded border border-white/[0.08] bg-[#0A0A0B] p-2"
                      >
                        <div className="flex items-center justify-between font-mono text-[11px]">
                          <span className="text-[#38BDF8]">
                            {ev.event_type.replace(/_/g, " ")}
                          </span>
                          <span className="tabular-nums text-[#94949E]">
                            {formatShortDate(ev.effective_date)}
                          </span>
                        </div>
                        <div className="mt-0.5 text-[#A1A1AA]">{ev.summary}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </section>

            {/* Channel 1: Facts */}
            <section
              data-testid="inspector-facts"
              className="space-y-2 border-b border-white/[0.08] pb-3.5"
            >
              <div className="flex items-center justify-between">
                <h3 className="text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                  Facts
                </h3>
                <span className="font-mono text-[11px] tabular-nums text-[#94949E]">
                  {detail.facts.length}
                </span>
              </div>

              {detail.facts.length === 0 ? (
                <div className="text-[#94949E]">No facts in active window</div>
              ) : (
                <div className="space-y-2">
                  {detail.facts.map((claim) => {
                    const isEditing = editingClaimId === claim.claim_id;
                    const statusColor =
                      claim.epistemic_status === "approved_fact"
                        ? "text-[#34D399]"
                        : claim.epistemic_status === "disputed"
                          ? "text-[#FB7185]"
                          : "text-[#FBBF24]";

                    return (
                      <div
                        key={claim.claim_id}
                        data-testid={`fact-claim-${claim.claim_id}`}
                        className="rounded-md border border-white/[0.08] bg-[#18181B]/60 p-2.5"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <span className="font-mono text-[11px] text-[#A1A1AA]">
                            {claim.predicate}
                            {claim.context_purpose !== "general"
                              ? ` · ${claim.context_purpose}`
                              : ""}
                          </span>
                          <div className="flex items-center gap-1.5">
                            <span className={`font-mono text-[10px] ${statusColor}`}>
                              {claim.epistemic_status}
                            </span>
                            {isSteward && !isEditing && (
                              <button
                                type="button"
                                aria-label={`Edit ${claim.predicate}`}
                                onClick={() => {
                                  setEditingClaimId(claim.claim_id);
                                  setEditValue(claim.object_value);
                                  setEditRationale("");
                                }}
                                className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[#94949E] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
                              >
                                <Edit3 className="h-3 w-3" />
                              </button>
                            )}
                          </div>
                        </div>

                        {isEditing ? (
                          <div className="mt-1.5 space-y-1.5">
                            <input
                              type="text"
                              value={editValue}
                              onChange={(e) => setEditValue(e.target.value)}
                              aria-label="New claim value"
                              className="w-full rounded border border-white/[0.16] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
                            />
                            <input
                              type="text"
                              value={editRationale}
                              onChange={(e) => setEditRationale(e.target.value)}
                              placeholder="Rationale"
                              aria-label="Edit rationale"
                              className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA] placeholder-[#94949E]"
                            />
                            <div className="flex justify-end gap-1.5">
                              <button
                                type="button"
                                onClick={() => setEditingClaimId(null)}
                                className="px-2 py-0.5 text-[11px] text-[#A1A1AA]"
                              >
                                Cancel
                              </button>
                              <button
                                type="button"
                                onClick={() => handleSaveClaimEdit(claim)}
                                className="rounded bg-[#FAFAFA] px-2 py-0.5 text-[11px] font-medium text-[#0A0A0B]"
                              >
                                Supersede
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="mt-1 flex flex-wrap items-baseline gap-2 font-mono text-[13px] font-medium tabular-nums text-[#FAFAFA]">
                            <span>
                              {claim.object_value}
                              {!claim.object_entity_id &&
                              /^-?\d+(\.\d+)?$/.test(claim.object_value.trim()) &&
                              claim.facets?.unit
                                ? ` ${claim.facets.unit}`
                                : ""}
                            </span>
                            {claim.object_entity_id &&
                              claim.facets?.numeric_value != null && (
                                <span className="rounded bg-[#0A0A0B] px-1.5 py-0.5 text-[11px] font-normal text-[#A1A1AA]">
                                  {claim.facets.numeric_value}
                                  {claim.facets.unit ? ` ${claim.facets.unit}` : ""}
                                </span>
                              )}
                          </div>
                        )}

                        {/* Plane 3 facets + Bitemporal window */}
                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 font-mono text-[10px] text-[#94949E]">
                          {claim.facets && (
                            <>
                              <span className="rounded bg-[#0A0A0B] px-1.5 py-0.5 text-[#A1A1AA]">
                                {claim.facets.domain_nature}
                              </span>
                              <span className="rounded bg-[#0A0A0B] px-1.5 py-0.5 text-[#A1A1AA]">
                                {claim.facets.epistemic_temporal}
                                {claim.facets.actual_stage
                                  ? `:${claim.facets.actual_stage}`
                                  : ""}
                              </span>
                              <span className="rounded bg-[#0A0A0B] px-1.5 py-0.5 text-[#A1A1AA]">
                                {claim.facets.modality}
                              </span>
                            </>
                          )}
                          <span className="ml-auto tabular-nums">
                            {formatShortDate(claim.valid_from)} →{" "}
                            {formatShortDate(claim.valid_to)}
                          </span>
                        </div>

                        {renderProvenance(claim)}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>

            {/* Channel 2: Directives */}
            {detail.directives.length > 0 && (
              <section
                data-testid="inspector-directives"
                className="space-y-2 border-b border-white/[0.08] pb-3.5"
              >
                <div className="flex items-center justify-between">
                  <h3 className="text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                    Directives
                  </h3>
                  <span className="font-mono text-[11px] tabular-nums text-[#94949E]">
                    {detail.directives.length}
                  </span>
                </div>
                <div className="space-y-1.5">
                  {detail.directives.map((d) => (
                    <div
                      key={d.claim_id}
                      className="rounded-md border border-white/[0.08] bg-[#0A0A0B] p-2.5"
                    >
                      <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
                        <span className="text-[#A1A1AA]">
                          {d.scope_type} · {d.predicate}
                        </span>
                        {d.is_mandatory && (
                          <span className="rounded bg-[#18181B] px-1.5 py-0.5 text-[#38BDF8]">
                            mandatory
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-[#F4F4F5]">{d.object_value}</p>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* Topics & Reverse Navigation */}
            {detail.topics.length > 0 && (
              <section className="space-y-1.5 border-b border-white/[0.08] pb-3.5">
                <h3 className="text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                  Topics
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {detail.topics.map((t) => (
                    <button
                      key={t.topic_id}
                      type="button"
                      onClick={() => onSelectTopic(t.slug)}
                      className="inline-flex items-center gap-1 rounded border border-white/[0.08] bg-[#18181B] px-2 py-0.5 text-[11px] text-[#F4F4F5] hover:border-white/[0.2]"
                    >
                      <Hash className="h-3 w-3 text-[#38BDF8]" />
                      {t.pref_label}
                    </button>
                  ))}
                </div>
              </section>
            )}

            {/* Coverages & Positions */}
            {(detail.coverages.length > 0 || detail.positions.length > 0) && (
              <section className="space-y-2 border-b border-white/[0.08] pb-3.5">
                {detail.coverages.length > 0 && (
                  <div>
                    <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                      Coverage
                    </h3>
                    <div className="space-y-1">
                      {detail.coverages.map((cov) => (
                        <div
                          key={cov.coverage_id}
                          className="flex items-center justify-between rounded bg-[#0A0A0B] px-2 py-1 font-mono text-[11px]"
                        >
                          <span className="text-[#A1A1AA]">
                            {cov.coverage_role.replace(/_/g, " ")}
                          </span>
                          <span className="text-[#FAFAFA]">
                            {cov.coverage_status}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {detail.positions.length > 0 && (
                  <div>
                    <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                      Positions
                    </h3>
                    <div className="space-y-1">
                      {detail.positions.map((pos) => (
                        <div
                          key={pos.position_id}
                          className="flex items-center justify-between rounded bg-[#0A0A0B] px-2 py-1 font-mono text-[11px] tabular-nums"
                        >
                          <span className="text-[#A1A1AA]">
                            {pos.position_type}
                          </span>
                          <span className="text-[#FAFAFA]">
                            {pos.market_value != null
                              ? `${pos.currency || "USD"} ${pos.market_value.toLocaleString()}`
                              : pos.weight_bps != null
                                ? `${pos.weight_bps} bps`
                                : "—"}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </section>
            )}

            {/* Backlinks */}
            {detail.backlinks.length > 0 && (
              <section className="space-y-1.5">
                <h3 className="text-[11px] font-medium uppercase tracking-wider text-[#94949E]">
                  Connections
                </h3>
                <div className="space-y-1">
                  {detail.backlinks.slice(0, 12).map((b, i) => (
                    <button
                      key={`${b.entity_id}-${i}`}
                      type="button"
                      onClick={() => onSelectEntity(b.canonical_key || b.entity_id)}
                      className="flex w-full items-center justify-between rounded border border-white/[0.06] bg-[#0A0A0B] px-2.5 py-1.5 text-left hover:border-white/[0.16]"
                    >
                      <span className="truncate font-medium text-[#F4F4F5]">
                        {b.display_name}
                      </span>
                      <span className="flex shrink-0 items-center gap-1 font-mono text-[10px] text-[#94949E]">
                        {b.predicate && <span>{b.predicate}</span>}
                        <ArrowUpRight className="h-3 w-3" />
                      </span>
                    </button>
                  ))}
                </div>
              </section>
            )}

            {/* Version history (git-graph style, ADR-007) */}
            <HistoryPanel
              entityId={detail.entity.entity_id}
              auth={auth}
              dataEpoch={dataEpoch}
              onMutated={onMutated}
            />
          </>
        )}
      </div>
    </aside>
  );
}
