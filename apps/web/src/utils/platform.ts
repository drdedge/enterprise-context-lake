import type { PersonaDTO, TemporalSnapshot } from "../types/api";

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const platform =
    nav.userAgentData?.platform || navigator.platform || navigator.userAgent || "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

export function getSearchShortcutLabel(): string {
  return isMacPlatform() ? "⌘K" : "Ctrl K";
}

export const TEMPORAL_SNAPSHOTS: TemporalSnapshot[] = [
  {
    id: "latest",
    label: "Latest (Live)",
    shortLabel: "Latest",
  },
  {
    id: "q4-2025-restated",
    label: "Q4 2025 Restated (2026-02-25)",
    shortLabel: "Q4 2025 Restated",
    asOf: "2025-12-31T23:59:59Z",
    recordedAsOf: "2026-02-25T00:00:00Z",
  },
  {
    id: "q4-2025-prelim",
    label: "Q4 2025 Pre-Restatement (2026-01-20)",
    shortLabel: "Q4 2025 Prelim",
    asOf: "2025-12-31T23:59:59Z",
    recordedAsOf: "2026-01-20T00:00:00Z",
  },
  {
    id: "2024-pre-rename",
    label: "2024 Pre-Rename (2024-06-15)",
    shortLabel: "2024 Pre-Rename",
    asOf: "2024-06-15T00:00:00Z",
    recordedAsOf: "2024-06-20T00:00:00Z",
  },
];

// Fallback persona options used strictly to keep the persona switcher usable
// even when the currently active persona is revoked (HTTP 401).
export const FALLBACK_PERSONAS: PersonaDTO[] = [
  {
    user_id: "user:fund-steward",
    display_name: "David Chen",
    role_summary: "Fund Steward",
    wall_side: "above_the_wall",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:pm-credit",
    display_name: "Marcus Vance",
    role_summary: "Portfolio Manager",
    wall_side: "public_side",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:public-analyst",
    display_name: "Elena Rostova",
    role_summary: "Public Analyst",
    wall_side: "public_side",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:private-deal-team",
    display_name: "Devon Brooks",
    role_summary: "Private Credit",
    wall_side: "private_side",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:rp-ir",
    display_name: "Claire Bennett",
    role_summary: "RP / IR",
    wall_side: "public_side",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:product-lead",
    display_name: "Rachel Kim",
    role_summary: "Product",
    wall_side: "public_side",
    revoked: false,
    groups_overage: false,
  },
  {
    user_id: "user:overage-director",
    display_name: "Priya Patel",
    role_summary: "Overage Director",
    wall_side: "above_the_wall",
    revoked: false,
    groups_overage: true,
  },
  {
    user_id: "user:revoked-analyst",
    display_name: "Jordan Blake",
    role_summary: "Revoked",
    wall_side: "public_side",
    revoked: true,
    groups_overage: false,
  },
];

export function formatEntityType(entityType: string): string {
  return entityType.replace(/_/g, " ");
}

export function formatShortDate(iso?: string | null): string {
  if (!iso) return "Open";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toISOString().slice(0, 10);
}

export function formatWallSideLabel(wallSide: string): string {
  switch (wallSide) {
    case "above_the_wall":
      return "Above Wall";
    case "private_side":
      return "Private";
    default:
      return "Public";
  }
}

export function formatReceiptShort(receiptId: string): string {
  if (!receiptId) return "";
  if (receiptId.startsWith("rcpt-")) return receiptId.slice(0, 13);
  return `rcpt-${receiptId.slice(0, 8)}`;
}

export function pluralize(
  count: number,
  singular: string,
  plural?: string,
): string {
  return `${count} ${count === 1 ? singular : plural || `${singular}s`}`;
}

export function shortPersonaRole(persona: PersonaDTO): string {
  const map: Record<string, string> = {
    "user:fund-steward": "Fund Steward",
    "user:pm-credit": "Portfolio Manager",
    "user:public-analyst": "Public Analyst",
    "user:private-deal-team": "Private Credit",
    "user:rp-ir": "RP / IR",
    "user:product-lead": "Product",
    "user:overage-director": "Overage Director",
    "user:revoked-analyst": "Revoked",
  };
  return map[persona.user_id] || persona.role_summary;
}
