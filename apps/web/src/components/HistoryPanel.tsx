import { useCallback, useEffect, useMemo, useState } from "react";
import { Bot, GitBranch, GitCommitVertical, RotateCcw, Terminal, Upload, User } from "lucide-react";
import { ApiError, versionsApi } from "../api/client";
import type {
  AuthContextState,
  ChangeSetDTO,
  DiffDTO,
  HistoryDTO,
  HistoryVersionDTO,
  RedlineSegment,
} from "../types/api";
import { formatShortDate } from "../utils/platform";

/**
 * Per-item history in the style of an editor's git graph (ADR-007).
 * Main lane: merged versions, newest on top. Side lane: open, rejected or conflicting proposals,
 * drawn branching off the version they were based on. Click a dot for its redline; shift-click a
 * second dot to compare any two versions.
 */

interface HistoryPanelProps {
  entityId: string;
  auth: AuthContextState;
  dataEpoch: number;
  onMutated: () => void;
}

const ROW_H = 44;
const LANE_X = [14, 38];
const DOT_R = 5;

const CHANNEL_STYLE: Record<string, string> = {
  production: "border-[#34D399]/40 text-[#34D399]",
  staging: "border-[#FBBF24]/40 text-[#FBBF24]",
  dev: "border-[#38BDF8]/40 text-[#38BDF8]",
};

function SourceIcon({ source }: { source: string }) {
  const cls = "h-3 w-3 shrink-0";
  if (source === "agent") return <Bot className={cls} aria-label="agent" />;
  if (source === "harness") return <Terminal className={cls} aria-label="harness" />;
  if (source === "upload") return <Upload className={cls} aria-label="upload" />;
  return <User className={cls} aria-label={source} />;
}

export function Redline({ segments }: { segments: RedlineSegment[] }) {
  if (!segments.length) return <span className="text-[#94949E]">No text change</span>;
  return (
    <p className="whitespace-pre-wrap break-words leading-relaxed" data-testid="redline">
      {segments.map((s, i) =>
        s.op === "equal" ? (
          <span key={i} className="text-[#A1A1AA]">{s.text}</span>
        ) : s.op === "delete" ? (
          <del key={i} className="bg-[#2A1219] text-[#FB7185] decoration-[#FB7185]">{s.text}</del>
        ) : (
          <ins key={i} className="bg-[#0A221A] text-[#34D399] no-underline decoration-[#34D399] [text-decoration-line:underline]">{s.text}</ins>
        ),
      )}
    </p>
  );
}

type Row =
  | { kind: "version"; v: HistoryVersionDTO }
  | { kind: "proposal"; p: HistoryDTO["proposals"][number] };

export function HistoryPanel({ entityId, auth, dataEpoch, onMutated }: HistoryPanelProps) {
  const [open, setOpen] = useState(false);
  const [targets, setTargets] = useState<{ type: string; id: string; label: string }[]>([]);
  const [target, setTarget] = useState<{ type: string; id: string } | null>(null);
  const [history, setHistory] = useState<HistoryDTO | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [diff, setDiff] = useState<DiffDTO | null>(null);
  const [proposal, setProposal] = useState<ChangeSetDTO | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Pieces of context about this entity: the entity record and each claim history ("slot")
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    versionsApi
      .claimSlots(auth, entityId)
      .then((res) => {
        if (cancelled) return;
        const list = [
          { type: "entity", id: entityId, label: "Entity record" },
          ...res.slots.map((s) => ({ type: "claim", id: s.record_id, label: s.label.split(" · ").pop() || s.label })),
        ];
        setTargets(list);
        setTarget((cur) => (cur && list.some((t) => t.id === cur.id) ? cur : { type: "entity", id: entityId }));
      })
      .catch(() => !cancelled && setTargets([{ type: "entity", id: entityId, label: "Entity record" }]));
    return () => {
      cancelled = true;
    };
  }, [open, entityId, auth, dataEpoch]);

  const load = useCallback(async () => {
    if (!target) return;
    try {
      const h = await versionsApi.history(auth, target.type, target.id);
      setHistory(h);
      setSelected(h.versions.length ? [h.versions[0].version_no] : []);
      setProposal(null);
      setMsg(null);
    } catch (e) {
      setHistory(null);
      setMsg(e instanceof ApiError && e.status === 404 ? "No history you can see for this item." : String(e));
    }
  }, [auth, target]);

  useEffect(() => {
    if (open) void load();
  }, [open, load, dataEpoch]);

  // Redline for the selected version (against its parent) or between two selected versions
  useEffect(() => {
    if (!history || !target || !selected.length) {
      setDiff(null);
      return;
    }
    const [a, b] = selected.length === 2 ? [...selected].sort((x, y) => x - y) : [undefined, selected[0]];
    versionsApi
      .diff(auth, target.type, target.id, a, b)
      .then(setDiff)
      .catch(() => setDiff(null));
  }, [history, selected, auth, target]);

  const rows: Row[] = useMemo(() => {
    if (!history) return [];
    const out: Row[] = [];
    const props = [...history.proposals];
    for (const v of history.versions) {
      // Proposals based on this version are drawn just above it, branching off it
      for (const p of props.filter((p) => p.base_version === v.version_no)) out.push({ kind: "proposal", p });
      out.push({ kind: "version", v });
    }
    for (const p of props.filter((p) => !history.versions.some((v) => v.version_no === p.base_version)))
      out.unshift({ kind: "proposal", p });
    return out;
  }, [history]);

  const rowOfVersion = (n: number | null) => rows.findIndex((r) => r.kind === "version" && r.v.version_no === n);

  const pick = (n: number, shift: boolean) => {
    setProposal(null);
    setSelected((cur) => (shift && cur.length >= 1 && cur[0] !== n ? [cur[0], n] : [n]));
  };

  const openProposal = async (changeId: string) => {
    try {
      setProposal(await versionsApi.getChangeSet(auth, changeId));
    } catch (e) {
      setMsg(String(e));
    }
  };

  const act = async (fn: () => Promise<ChangeSetDTO>, done: string) => {
    setBusy(true);
    try {
      const cs = await fn();
      setMsg(`${done}: ${cs.status}`);
      onMutated();
      await load();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const head = history?.versions.find((v) => v.version_no === selected[selected.length - 1]);
  const graphH = rows.length * ROW_H;

  return (
    <section className="space-y-2" data-testid="history-panel">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-[#94949E] hover:text-[#FAFAFA]"
      >
        <GitBranch className="h-3 w-3" /> History {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="space-y-2">
          {targets.length > 1 && (
            <select
              aria-label="Piece of context"
              className="w-full rounded border border-white/[0.08] bg-[#0A0A0B] px-2 py-1 text-[11px] text-[#F4F4F5]"
              value={target ? `${target.type}::${target.id}` : ""}
              onChange={(e) => {
                const [type, ...rest] = e.target.value.split("::");
                setTarget({ type, id: rest.join("::") });
              }}
            >
              {targets.map((t) => (
                <option key={`${t.type}::${t.id}`} value={`${t.type}::${t.id}`}>
                  {t.label}
                </option>
              ))}
            </select>
          )}
          {msg && <div className="rounded border border-white/[0.08] bg-[#0A0A0B] px-2 py-1 text-[11px] text-[#A1A1AA]">{msg}</div>}

          {history && (
            <div className="relative rounded border border-white/[0.06] bg-[#0A0A0B]" style={{ height: graphH }}>
              <svg className="absolute left-0 top-0" width={52} height={graphH} aria-hidden>
                {/* main lane */}
                {rows.some((r) => r.kind === "version") && (
                  <line
                    x1={LANE_X[0]}
                    x2={LANE_X[0]}
                    y1={ROW_H / 2 + Math.max(0, rows.findIndex((r) => r.kind === "version")) * ROW_H}
                    y2={graphH - ROW_H / 2}
                    stroke="#3F3F46"
                    strokeWidth={2}
                  />
                )}
                {rows.map((r, i) => {
                  const y = i * ROW_H + ROW_H / 2;
                  if (r.kind === "version") {
                    const sel = selected.includes(r.v.version_no);
                    const reverted = Boolean(r.v.reverted_by_change_id);
                    return (
                      <circle
                        key={`v${r.v.version_no}`}
                        cx={LANE_X[0]}
                        cy={y}
                        r={DOT_R}
                        fill={sel ? "#38BDF8" : reverted ? "#18181B" : "#A1A1AA"}
                        stroke={r.v.is_head ? "#38BDF8" : "#52525B"}
                        strokeWidth={2}
                      />
                    );
                  }
                  const baseRow = rowOfVersion(r.p.base_version);
                  const by = baseRow >= 0 ? baseRow * ROW_H + ROW_H / 2 : graphH;
                  const color = r.p.status === "proposed" ? "#FBBF24" : r.p.status === "conflict" ? "#FB7185" : "#52525B";
                  return (
                    <g key={`p${r.p.change_id}`}>
                      <path
                        d={`M ${LANE_X[0]} ${by} C ${LANE_X[0]} ${by - ROW_H / 2}, ${LANE_X[1]} ${y + ROW_H / 2}, ${LANE_X[1]} ${y}`}
                        fill="none"
                        stroke={color}
                        strokeWidth={2}
                        strokeDasharray={r.p.status === "proposed" ? undefined : "3 3"}
                      />
                      <circle cx={LANE_X[1]} cy={y} r={DOT_R} fill="#0A0A0B" stroke={color} strokeWidth={2} />
                    </g>
                  );
                })}
              </svg>
              {rows.map((r, i) => (
                <button
                  key={r.kind === "version" ? `rv${r.v.version_no}` : `rp${r.p.change_id}`}
                  type="button"
                  onClick={(e) => (r.kind === "version" ? pick(r.v.version_no, e.shiftKey) : void openProposal(r.p.change_id))}
                  className={`absolute left-[52px] right-1 flex flex-col justify-center rounded px-1.5 text-left hover:bg-white/[0.04] ${
                    r.kind === "version" && selected.includes(r.v.version_no) ? "bg-white/[0.05]" : ""
                  }`}
                  style={{ top: i * ROW_H, height: ROW_H }}
                  data-testid={r.kind === "version" ? `history-version-${r.v.version_no}` : `history-proposal-${r.p.change_id}`}
                >
                  {r.kind === "version" ? (
                    <>
                      <span className="flex items-center gap-1.5 truncate text-[11px] text-[#F4F4F5]">
                        <span className="font-mono text-[10px] text-[#94949E]">v{r.v.version_no}</span>
                        <span className={`truncate ${r.v.reverted_by_change_id ? "line-through text-[#94949E]" : ""}`}>
                          {r.v.title || r.v.op}
                        </span>
                        {r.v.channels.map((c) => (
                          <span key={c} className={`shrink-0 rounded border px-1 font-mono text-[9px] ${CHANNEL_STYLE[c] || ""}`}>
                            {c}
                          </span>
                        ))}
                      </span>
                      <span className="flex items-center gap-1 truncate font-mono text-[10px] text-[#94949E]">
                        <SourceIcon source={r.v.source} />
                        {r.v.author_name || r.v.author_id}
                        {r.v.agent_id ? ` · ${r.v.agent_id}` : ""} · {formatShortDate(r.v.recorded_at)}
                        {r.v.approvals.length > 0 && ` · approved by ${r.v.approvals.filter((a) => a.decision === "approve").map((a) => a.approver_name || a.approver_id).join(", ")}`}
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="flex items-center gap-1.5 truncate text-[11px] text-[#FBBF24]">
                        <GitCommitVertical className="h-3 w-3" /> {r.p.title}
                        <span className="font-mono text-[9px] uppercase text-[#94949E]">{r.p.status}</span>
                      </span>
                      <span className="truncate font-mono text-[10px] text-[#94949E]">
                        {r.p.author_name} · needs {r.p.approvers.join(" + ")}
                      </span>
                    </>
                  )}
                </button>
              ))}
            </div>
          )}

          {/* Proposal detail with approve / reject */}
          {proposal && (
            <div className="space-y-1.5 rounded border border-[#FBBF24]/30 bg-[#18181B] p-2">
              <div className="text-[11px] font-medium text-[#FAFAFA]">{proposal.title}</div>
              {proposal.reason && <div className="text-[11px] text-[#A1A1AA]">{proposal.reason}</div>}
              {proposal.items.map((it) => (
                <div key={it.seq} className="text-[11px]">
                  {it.redline && it.redline.length > 0 ? (
                    <Redline segments={it.redline} />
                  ) : (
                    <ul className="font-mono text-[10px] text-[#A1A1AA]">
                      {(it.changes || []).map((c) => (
                        <li key={c.field}>
                          {c.field}: {JSON.stringify(c.old)} → {JSON.stringify(c.new)}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
              {proposal.status === "proposed" && (
                <div className="flex gap-1.5">
                  <button
                    type="button"
                    disabled={busy || !proposal.can_approve}
                    title={proposal.can_approve ? "" : `Needs ${proposal.approvers.join(" + ")}`}
                    onClick={() => void act(() => versionsApi.approve(auth, proposal.change_id), "Approved")}
                    className="rounded border border-[#34D399]/40 px-2 py-0.5 text-[11px] text-[#34D399] disabled:opacity-40"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void act(() => versionsApi.reject(auth, proposal.change_id), "Rejected")}
                    className="rounded border border-[#FB7185]/40 px-2 py-0.5 text-[11px] text-[#FB7185] disabled:opacity-40"
                  >
                    {proposal.author_id === auth.userId ? "Withdraw" : "Reject"}
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Redline of the selected version(s) */}
          {!proposal && diff && (
            <div className="space-y-1.5 rounded border border-white/[0.06] bg-[#18181B] p-2 text-[11px]" data-testid="history-diff">
              <div className="flex items-center justify-between font-mono text-[10px] text-[#94949E]">
                <span>
                  {diff.from_version ? `v${diff.from_version} → v${diff.to_version}` : `v${diff.to_version} (first version)`}
                  {selected.length < 2 && " · shift-click another version to compare"}
                </span>
                {head && selected.length === 1 && !head.reverted_by_change_id && !["baseline", "import"].includes(head.op) && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void act(() => versionsApi.revert(auth, head.change_id), "Revert")}
                    className="flex shrink-0 items-center gap-1 whitespace-nowrap rounded border border-white/[0.1] px-1.5 py-0.5 text-[#F4F4F5] hover:border-white/[0.3]"
                  >
                    <RotateCcw className="h-3 w-3" /> Revert this change
                  </button>
                )}
              </div>
              {diff.restricted ? (
                <div className="text-[#A1A1AA]">{diff.message}</div>
              ) : (
                <>
                  {diff.redline.length > 0 && <Redline segments={diff.redline} />}
                  <ul className="space-y-0.5 font-mono text-[10px] text-[#A1A1AA]">
                    {diff.changes
                      .filter((c) => c.field !== diff.text_field)
                      .slice(0, 12)
                      .map((c) => (
                        <li key={c.field} className="truncate">
                          <span className="text-[#F4F4F5]">{c.field}</span>:{" "}
                          {c.kind !== "added" && <del className="text-[#FB7185]">{JSON.stringify(c.old)}</del>}{" "}
                          {c.kind !== "removed" && <ins className="text-[#34D399] no-underline">{JSON.stringify(c.new)}</ins>}
                        </li>
                      ))}
                  </ul>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
