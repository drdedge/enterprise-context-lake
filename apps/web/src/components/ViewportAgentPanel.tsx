import { useState } from "react";
import { ArrowUpRight, Bot, Send, X } from "lucide-react";
import { api } from "../api/client";
import { formatReceiptShort } from "../utils/platform";
import type {
  AgentCitationDTO,
  AuthContextState,
  ContextAssemblyReceiptDTO,
  GraphLens,
  TemporalSnapshot,
} from "../types/api";

interface ViewportAgentPanelProps {
  open: boolean;
  onClose: () => void;
  auth: AuthContextState;
  snapshot: TemporalSnapshot;
  activeLens: GraphLens;
  visibleEntityIds: string[];
  selectedEntityId: string | null;
  onSelectCitation: (
    citation: AgentCitationDTO,
    fallbackEntityId?: string,
  ) => void;
}

interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: AgentCitationDTO[];
  receipt?: ContextAssemblyReceiptDTO;
  toolCalls?: Array<{ tool: string; duration_ms: number }>;
}

export function ViewportAgentPanel({
  open,
  onClose,
  auth,
  snapshot,
  activeLens,
  visibleEntityIds,
  selectedEntityId,
  onSelectCitation,
}: ViewportAgentPanelProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const message = input.trim();
    if (!message || busy) return;

    const userTurn: ChatTurn = {
      id: `u-${Date.now()}`,
      role: "user",
      content: message,
    };
    setTurns((prev) => [...prev, userTurn]);
    setInput("");
    setBusy(true);

    try {
      const res = await api.chatWithAgent(auth, {
        message,
        viewport: {
          lens: activeLens,
          visible_entity_ids: visibleEntityIds.slice(0, 50),
          selected_entity_id: selectedEntityId,
          as_of: snapshot.asOf,
          recorded_as_of: snapshot.recordedAsOf,
        },
      });
      setTurns((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          role: "assistant",
          content: res.answer_markdown,
          citations: res.citations || [],
          receipt: res.receipt,
          toolCalls: res.tool_calls || [],
        },
      ]);
    } catch (err: unknown) {
      setTurns((prev) => [
        ...prev,
        {
          id: `err-${Date.now()}`,
          role: "assistant",
          content:
            err instanceof Error
              ? err.message
              : "Agent query failed for current viewport.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside
      aria-label="Docked Viewport Agent"
      data-testid="viewport-agent-panel"
      className="flex w-[360px] shrink-0 flex-col border-l border-white/[0.08] bg-[#111113]"
    >
      {/* Header */}
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-white/[0.08] px-3.5">
        <div className="flex items-center gap-2">
          <Bot className="h-4 w-4 text-[#FAFAFA]" />
          <span className="text-xs font-medium text-[#FAFAFA]">
            Viewport Agent
          </span>
          <span className="rounded border border-white/[0.08] bg-[#0A0A0B] px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-[#94949E]">
            {visibleEntityIds.length} in view
          </span>
        </div>

        <button
          type="button"
          aria-label="Close viewport agent"
          data-testid="agent-close"
          onClick={onClose}
          className="rounded p-1 text-[#94949E] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 space-y-3 overflow-y-auto p-3.5 text-xs">
        {turns.length === 0 ? (
          <div className="space-y-1.5 pt-4">
            {[
              "Summarize open conflicts in this view",
              "Which skills depend on Alpha Credit Offshore?",
              "Show Q4 2025 NAV and restatement status",
            ].map((prompt) => (
              <button
                key={prompt}
                type="button"
                onClick={() => setInput(prompt)}
                className="block w-full rounded-md border border-white/[0.08] bg-[#0A0A0B] px-3 py-2 text-left text-xs text-[#A1A1AA] hover:border-white/[0.18] hover:text-[#FAFAFA]"
              >
                {prompt}
              </button>
            ))}
          </div>
        ) : (
          turns.map((turn) => (
            <div
              key={turn.id}
              data-testid={`agent-turn-${turn.role}`}
              className={`rounded-lg border p-3 ${
                turn.role === "user"
                  ? "border-white/[0.08] bg-[#18181B] text-[#FAFAFA]"
                  : "border-white/[0.08] bg-[#0A0A0B] text-[#F4F4F5]"
              }`}
            >
              <div className="whitespace-pre-wrap leading-relaxed">
                {turn.content}
              </div>

              {turn.citations && turn.citations.length > 0 && (
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {turn.citations.map((cit, i) => {
                    const fallbackEntity = turn.citations?.find(
                      (c) => c.kind === "entity",
                    )?.id;
                    return (
                      <button
                        key={`${cit.kind}-${cit.id}-${i}`}
                        type="button"
                        data-testid={`agent-citation-${cit.id}`}
                        onClick={() => onSelectCitation(cit, fallbackEntity)}
                        className="inline-flex items-center gap-1 rounded border border-white/[0.1] bg-[#18181B] px-1.5 py-0.5 font-mono text-[10px] text-[#38BDF8] hover:border-[#38BDF8]/50"
                      >
                        <span>{cit.label || `${cit.kind}:${cit.id}`}</span>
                        <ArrowUpRight className="h-2.5 w-2.5" />
                      </button>
                    );
                  })}
                </div>
              )}

              {turn.receipt && (
                <div
                  data-testid="agent-receipt-footer"
                  className="mt-2.5 flex items-center justify-between border-t border-white/[0.06] pt-1.5 font-mono text-[10px] tabular-nums text-[#94949E]"
                >
                  <span>{formatReceiptShort(turn.receipt.receipt_id)}</span>
                  <span>
                    {turn.receipt.snapshot_version != null
                      ? `v${turn.receipt.snapshot_version}`
                      : `e${turn.receipt.data_epoch ?? 0}`}
                  </span>
                </div>
              )}
            </div>
          ))
        )}
      </div>

      {/* Input */}
      <form
        onSubmit={handleSubmit}
        className="flex shrink-0 items-center gap-1.5 border-t border-white/[0.08] bg-[#0A0A0B] p-2.5"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about current viewport…"
          aria-label="Ask Viewport Agent"
          data-testid="agent-input"
          className="h-8 flex-1 rounded-md border border-white/[0.1] bg-[#111113] px-2.5 text-xs text-[#FAFAFA] placeholder-[#94949E] focus:border-white/[0.24] focus:outline-none"
        />
        <button
          type="submit"
          aria-label="Send message"
          data-testid="agent-send-btn"
          disabled={busy || !input.trim()}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-[#FAFAFA] text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-40"
        >
          <Send className="h-3.5 w-3.5" />
        </button>
      </form>
    </aside>
  );
}
