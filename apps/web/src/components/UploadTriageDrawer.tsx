import { useEffect, useRef, useState } from "react";
import {
  Check,
  FileUp,
  Lock,
  ShieldCheck,
  Upload,
  X,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "../api/client";
import type {
  AuthContextState,
  ClaimDTO,
  EntitySummaryDTO,
  PrincipalMeDTO,
  TopicDTO,
  UploadResponseDTO,
} from "../types/api";
import { formatShortDate } from "../utils/platform";

interface UploadTriageDrawerProps {
  open: boolean;
  onClose: () => void;
  auth: AuthContextState;
  me: PrincipalMeDTO | null;
  entities: EntitySummaryDTO[];
  topics: TopicDTO[];
  dataEpoch: number;
  onMutated: () => void;
}

const ACL_GROUP_OPTIONS = [
  "firm-all",
  "custodian-legal-docs",
  "investment-team",
  "fund-stewards",
  "private-credit-deal-team",
];

export function UploadTriageDrawer({
  open,
  onClose,
  auth,
  me,
  entities,
  topics,
  dataEpoch,
  onMutated,
}: UploadTriageDrawerProps) {
  const [tab, setTab] = useState<"upload" | "triage">("upload");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [subjectKey, setSubjectKey] = useState("");
  const [topicSlug, setTopicSlug] = useState("");
  const [wallSide, setWallSide] = useState<"public_side" | "private_side">(
    "public_side",
  );
  const [aclGroups, setAclGroups] = useState<string[]>(["firm-all"]);
  const [notes, setNotes] = useState("");
  const [uploading, setUploading] = useState(false);
  const [receipt, setReceipt] = useState<UploadResponseDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Triage inbox queue
  const [inboxItems, setInboxItems] = useState<ClaimDTO[]>([]);
  const [loadingInbox, setLoadingInbox] = useState(false);
  const [triageBusyId, setTriageBusyId] = useState<string | null>(null);
  const [triageStatus, setTriageStatus] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const isSteward = Boolean(
    me?.roles?.includes("fund_steward") ||
      me?.roles?.includes("compliance_reviewer") ||
      me?.groups?.includes("fund-stewards") ||
      me?.groups?.includes("compliance-oversight"),
  );

  const loadInbox = async () => {
    setLoadingInbox(true);
    try {
      const res = await api.getInbox(auth);
      setInboxItems(res.items || []);
    } catch {
      setInboxItems([]);
    } finally {
      setLoadingInbox(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    setError(null);
    setTriageStatus(null);
    void loadInbox();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, auth.userId, dataEpoch]);

  if (!open) return null;

  const toggleAclGroup = (grp: string) => {
    setAclGroups((prev) =>
      prev.includes(grp) ? prev.filter((g) => g !== grp) : [...prev, grp],
    );
  };

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedFile) return;
    setUploading(true);
    setError(null);
    try {
      const res = await api.uploadFile(auth, {
        file: selectedFile,
        subject_canonical_key: subjectKey || undefined,
        topic_slug: topicSlug || undefined,
        wall_side: wallSide,
        evidence_acl_groups: aclGroups,
        notes: notes.trim() || undefined,
      });
      setReceipt(res);
      setSelectedFile(null);
      setNotes("");
      await loadInbox();
      onMutated();
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : "Upload failed.",
      );
    } finally {
      setUploading(false);
    }
  };

  const handleApprove = async (claimId: string) => {
    setTriageBusyId(claimId);
    setTriageStatus(null);
    try {
      await api.approveInboxClaim(auth, claimId, {
        rationale: "Approved from manual upload triage queue",
        expected_data_epoch: dataEpoch,
      });
      setTriageStatus(`Approved ${claimId}`);
      await loadInbox();
      onMutated();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Approval failed.");
    } finally {
      setTriageBusyId(null);
    }
  };

  const handleReject = async (claimId: string) => {
    setTriageBusyId(claimId);
    setTriageStatus(null);
    try {
      await api.rejectInboxClaim(auth, claimId, {
        rationale: "Rejected during steward triage",
        expected_data_epoch: dataEpoch,
      });
      setTriageStatus(`Rejected ${claimId}`);
      await loadInbox();
      onMutated();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Rejection failed.");
    } finally {
      setTriageBusyId(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/60"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Manual Upload and Triage"
        data-testid="upload-drawer"
        className="flex h-full w-full max-w-[520px] flex-col border-l border-white/[0.1] bg-[#111113] shadow-2xl"
      >
        {/* Header */}
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-white/[0.08] px-4">
          <div className="flex items-center gap-1">
            <button
              type="button"
              data-testid="tab-upload"
              onClick={() => setTab("upload")}
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                tab === "upload"
                  ? "bg-[#18181B] text-[#FAFAFA]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              Upload
            </button>
            <button
              type="button"
              data-testid="tab-triage"
              onClick={() => setTab("triage")}
              className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium ${
                tab === "triage"
                  ? "bg-[#18181B] text-[#FAFAFA]"
                  : "text-[#A1A1AA] hover:text-[#FAFAFA]"
              }`}
            >
              <span>Quarantine Triage</span>
              <span className="rounded bg-[#202024] px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-[#FBBF24]">
                {inboxItems.length}
              </span>
            </button>
          </div>

          <button
            type="button"
            aria-label="Close upload drawer"
            data-testid="upload-close"
            onClick={onClose}
            className="rounded p-1 text-[#94949E] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 text-xs">
          {error && (
            <div className="mb-3 rounded-md border border-[#F43F5E]/40 bg-[#2A1219] px-3 py-2 text-[#FB7185]">
              {error}
            </div>
          )}

          {triageStatus && (
            <div className="mb-3 rounded-md border border-[#34D399]/30 bg-[#0A221A] px-3 py-2 text-[#34D399]">
              {triageStatus}
            </div>
          )}

          {tab === "upload" ? (
            <form onSubmit={handleUpload} className="space-y-4">
              {/* Dropzone */}
              <div
                data-testid="upload-dropzone"
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragging(false);
                  const file = e.dataTransfer.files?.[0];
                  if (file) setSelectedFile(file);
                }}
                onClick={() => fileInputRef.current?.click()}
                className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed px-4 py-7 transition-colors ${
                  dragging
                    ? "border-white/50 bg-[#18181B]"
                    : "border-white/[0.16] bg-[#0A0A0B] hover:border-white/[0.28]"
                }`}
              >
                <FileUp className="mb-2 h-5 w-5 text-[#A1A1AA]" />
                <div className="font-medium text-[#FAFAFA]">
                  {selectedFile
                    ? selectedFile.name
                    : "Drop PDF, CSV, Markdown, or JSON"}
                </div>
                <div className="mt-1 font-mono text-[11px] tabular-nums text-[#94949E]">
                  {selectedFile
                    ? `${(selectedFile.size / 1024).toFixed(1)} KB`
                    : "SHA-256 · quarantined unverified_observation"}
                </div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf,.csv,.md,.txt,.json"
                  aria-label="Choose file to upload"
                  data-testid="upload-file-input"
                  onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
                  className="hidden"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">
                    Subject Entity
                  </span>
                  <select
                    data-testid="upload-subject-select"
                    value={subjectKey}
                    onChange={(e) => setSubjectKey(e.target.value)}
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1.5 font-mono text-xs text-[#FAFAFA]"
                  >
                    <option value="">Auto-detect</option>
                    {entities.map((e) => (
                      <option key={e.entity_id} value={e.canonical_key}>
                        {e.display_name}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">Topic</span>
                  <select
                    data-testid="upload-topic-select"
                    value={topicSlug}
                    onChange={(e) => setTopicSlug(e.target.value)}
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1.5 text-xs text-[#FAFAFA]"
                  >
                    <option value="">Optional</option>
                    {topics
                      .filter((t) => t.status === "active")
                      .map((t) => (
                        <option key={t.topic_id} value={t.slug}>
                          {t.pref_label}
                        </option>
                      ))}
                  </select>
                </label>
              </div>

              <div className="space-y-1.5">
                <span className="text-[11px] text-[#A1A1AA]">Wall Side</span>
                <div className="flex gap-2">
                  {(["public_side", "private_side"] as const).map((w) => {
                    const disabled =
                      w === "private_side" && me?.wall_side === "public_side";
                    return (
                      <button
                        key={w}
                        type="button"
                        disabled={disabled}
                        onClick={() => setWallSide(w)}
                        className={`rounded border px-2.5 py-1 font-mono text-[11px] ${
                          wallSide === w
                            ? "border-white/[0.25] bg-[#18181B] text-[#FAFAFA]"
                            : "border-white/[0.08] bg-[#0A0A0B] text-[#A1A1AA]"
                        } disabled:opacity-35`}
                      >
                        {w}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="space-y-1.5">
                <span className="flex items-center gap-1 text-[11px] text-[#A1A1AA]">
                  <Lock className="h-3 w-3" />
                  Evidence ACL Groups
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {ACL_GROUP_OPTIONS.map((grp) => {
                    const active = aclGroups.includes(grp);
                    return (
                      <button
                        key={grp}
                        type="button"
                        data-testid={`upload-acl-${grp}`}
                        onClick={() => toggleAclGroup(grp)}
                        className={`rounded border px-2 py-1 font-mono text-[11px] transition-colors ${
                          active
                            ? "border-white/[0.25] bg-[#18181B] text-[#FAFAFA]"
                            : "border-white/[0.08] bg-[#0A0A0B] text-[#94949E] hover:text-[#FAFAFA]"
                        }`}
                      >
                        {grp}
                      </button>
                    );
                  })}
                </div>
              </div>

              <label className="flex flex-col gap-1">
                <span className="text-[11px] text-[#A1A1AA]">Source Notes</span>
                <textarea
                  rows={2}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Optional provenance note…"
                  className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2.5 py-1.5 text-xs text-[#FAFAFA] placeholder-[#94949E]"
                />
              </label>

              <button
                type="submit"
                data-testid="upload-submit-btn"
                disabled={!selectedFile || uploading}
                className="flex w-full items-center justify-center gap-1.5 rounded-md bg-[#FAFAFA] py-2 text-xs font-medium text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-40"
              >
                <Upload className="h-3.5 w-3.5" />
                <span>
                  {uploading ? "Hashing & Quarantining…" : "Upload to Quarantine"}
                </span>
              </button>

              {/* SHA-256 Receipt */}
              {receipt && (
                <section
                  data-testid="upload-receipt"
                  aria-label="Upload SHA-256 Receipt"
                  className="space-y-2 rounded-lg border border-white/[0.12] bg-[#0A0A0B] p-3"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 font-medium text-[#FAFAFA]">
                      <ShieldCheck className="h-4 w-4 text-[#34D399]" />
                      {receipt.filename}
                    </span>
                    <span className="rounded bg-[#2A1E08] px-1.5 py-0.5 font-mono text-[10px] text-[#FBBF24]">
                      {receipt.epistemic_status}
                    </span>
                  </div>
                  <div
                    title={receipt.content_sha256}
                    className="truncate font-mono text-[10.5px] text-[#A1A1AA]"
                  >
                    SHA-256: {receipt.content_sha256}
                  </div>
                  {receipt.extracted_preview && (
                    <pre
                      tabIndex={0}
                      aria-label="Extracted upload preview"
                      className="max-h-28 overflow-auto whitespace-pre-wrap rounded border border-white/[0.06] bg-[#111113] p-2 font-mono text-[11px] text-[#A1A1AA]"
                    >
                      {receipt.extracted_preview}
                    </pre>
                  )}
                  <div className="flex items-center justify-between font-mono text-[11px] tabular-nums text-[#94949E]">
                    <span>
                      {receipt.quarantined_claim_ids.length} quarantined candidate
                      {receipt.quarantined_claim_ids.length === 1 ? "" : "s"}
                    </span>
                    <button
                      type="button"
                      onClick={() => setTab("triage")}
                      className="text-[#FAFAFA] hover:underline"
                    >
                      Open Triage →
                    </button>
                  </div>
                </section>
              )}
            </form>
          ) : (
            <div data-testid="triage-queue" className="space-y-2.5">
              {loadingInbox ? (
                <div className="py-8 text-center text-[#94949E]">Loading…</div>
              ) : inboxItems.length === 0 ? (
                <div className="rounded-lg border border-white/[0.08] bg-[#0A0A0B] px-4 py-8 text-center text-[#94949E]">
                  Quarantine inbox empty
                </div>
              ) : (
                inboxItems.map((item) => (
                  <div
                    key={item.claim_id}
                    data-testid={`triage-item-${item.claim_id}`}
                    className="rounded-lg border border-white/[0.08] bg-[#0A0A0B] p-3"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="font-mono text-[11px] text-[#A1A1AA]">
                          {item.subject_canonical_key || item.subject_id} ·{" "}
                          {item.predicate}
                        </div>
                        <div className="mt-1 font-mono text-sm font-medium text-[#FAFAFA]">
                          {item.object_value}
                        </div>
                      </div>
                      <span className="shrink-0 rounded bg-[#2A1E08] px-1.5 py-0.5 font-mono text-[10px] text-[#FBBF24]">
                        {item.epistemic_status}
                      </span>
                    </div>

                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 font-mono text-[10px] text-[#94949E]">
                      <span>{item.provenance?.source_system}</span>
                      {item.provenance?.content_sha256 && (
                        <span>
                          sha:{item.provenance.content_sha256.slice(0, 12)}
                        </span>
                      )}
                      <span className="tabular-nums">
                        {formatShortDate(item.recorded_at)}
                      </span>
                    </div>

                    {item.evidence_redacted && (
                      <div
                        data-testid="redacted-evidence-stub"
                        className="mt-2 flex items-center gap-1.5 rounded border border-white/[0.08] bg-[#111113] px-2 py-1 text-[11px] text-[#A1A1AA]"
                      >
                        <Lock className="h-3 w-3" />
                        <span>
                          {item.evidence_redaction_label ||
                            "Restricted — Request Access"}
                        </span>
                      </div>
                    )}

                    <div className="mt-2.5 flex items-center justify-end gap-2 border-t border-white/[0.06] pt-2">
                      {isSteward ? (
                        <>
                          <button
                            type="button"
                            disabled={triageBusyId === item.claim_id}
                            data-testid={`triage-reject-${item.claim_id}`}
                            onClick={() => void handleReject(item.claim_id)}
                            className="flex items-center gap-1 rounded border border-white/[0.1] bg-[#18181B] px-2.5 py-1 text-[11px] font-medium text-[#FB7185] hover:border-[#F43F5E]/40"
                          >
                            <XCircle className="h-3 w-3" />
                            Reject
                          </button>
                          <button
                            type="button"
                            disabled={triageBusyId === item.claim_id}
                            data-testid={`triage-approve-${item.claim_id}`}
                            onClick={() => void handleApprove(item.claim_id)}
                            className="flex items-center gap-1 rounded bg-[#FAFAFA] px-2.5 py-1 text-[11px] font-medium text-[#0A0A0B] hover:bg-[#E4E4E7]"
                          >
                            <Check className="h-3 w-3" />
                            Approve
                          </button>
                        </>
                      ) : (
                        <span className="font-mono text-[10px] text-[#94949E]">
                          Steward review required
                        </span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
