const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8080/api";

// ── Auth (N1) ────────────────────────────────────────────────
// The token lives in memory plus sessionStorage. sessionStorage rather than
// localStorage so closing the tab ends the session, and never in a cookie, since
// the API is called cross-origin from the Vite dev server.

const TOKEN_KEY = "approvalflow.token";
const IDENTITY_KEY = "approvalflow.identity";

export type Role = "submitter" | "approver" | "admin";

export interface Identity {
  subject: string;
  roles: Role[];
}

let token: string | null = sessionStorage.getItem(TOKEN_KEY);

export function currentIdentity(): Identity | null {
  const raw = sessionStorage.getItem(IDENTITY_KEY);
  return raw ? (JSON.parse(raw) as Identity) : null;
}

export function signOut(): void {
  token = null;
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(IDENTITY_KEY);
}

/** Exchange an identity for a signed JWT with the requested roles. */
export async function signIn(subject: string, roles: Role[]): Promise<Identity> {
  const res = await fetch(`${API_URL}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject, roles }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "Could not sign in");

  token = body.access_token;
  sessionStorage.setItem(TOKEN_KEY, token as string);
  const identity: Identity = { subject, roles };
  sessionStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
  return identity;
}

// ── Types ────────────────────────────────────────────────────

export interface SubmissionPayload {
  submitter: string;
  department: string;
  vendor: string;
  vendorKnown: boolean;
  invoiceNumber: string;
  currency: string;
  total: number;
  taxAmount: number;
  category: string;
  attendees: number;
  receiptPresent: boolean;
  notes: string;
  lineItems: Array<{
    description: string;
    quantity: number;
    unit_price: number;
  }>;
}

export interface InfoRequest {
  requested_by?: string;
  requested_fields?: string[];
  question?: string;
  requested_at_revision?: number;
}

export interface SubmissionStatus {
  correlation_id: string;
  status: string;
  vendor: string;
  amount_usd: string;
  category: string;
  /** Plain-language explanation of the outcome (F2). */
  reason?: string;
  rule_ids?: string[];
  revision?: number;
  duplicate_of?: string | null;
  /** What an approver has asked the submitter for (F5). */
  info_request?: InfoRequest | null;
  info_exchange?: Array<Record<string, unknown>>;
}

export interface ApprovalItem {
  correlation_id: string;
  status: string;
  vendor: string;
  amount_usd: number | string;
  category: string;
  submitter_email: string;
  /** Why it was escalated — the agent's view and the rules the router cited (F4). */
  agent_recommendation?: string | null;
  agent_confidence?: number | string | null;
  agent_reasoning?: string | null;
  rule_ids?: string[];
  escalation_reason?: string | null;
  revision?: number;
  open_question?: string | null;
  open_requested_fields?: string[] | null;
  info_exchange?: Array<Record<string, unknown>>;
}

export interface Dashboard {
  window_hours: number;
  items: number;
  decisions: number;
  routes: Record<string, number>;
  rates: { auto_approval_rate: number; escalation_rate: number; router_rounds: number };
  money_usd: { auto_approved: string; human_approved: string };
  avg_agent_confidence: number | null;
  avg_decision_latency_seconds: number | null;
  top_rules: Array<{ rule_id: string; count: number }>;
  throughput_per_hour: Array<{ hour: string; decisions: number; auto_approved: number }>;
  policy: {
    version: number;
    confidence_threshold: string;
    ceilings_usd: Record<string, string>;
    default_ceiling_usd: string;
  };
  outbox?: { depth_by_status: Record<string, number> };
}

export interface CeilingProof {
  auto_approvals_examined: number;
  ceiling_violations: number;
  confidence_violations: number;
  auto_approvals_not_made_by_router: number;
  max_auto_approved_amount_usd: string;
  highest_ceiling_in_use_usd: string;
  per_category: Array<{
    category: string;
    auto_approvals: number;
    max_amount_usd: string;
    lowest_ceiling_usd: string;
  }>;
  offending_items: Array<Record<string, string | number>>;
  holds: boolean;
  method?: string;
}

export interface PolicyResponse {
  config: Record<string, unknown>;
  rules: Array<{
    rule_id: string;
    outcome: string;
    reason: string;
    categories: string[];
    enabled: boolean;
  }>;
  hard_limits: Record<string, string>;
}

// ── Transport ────────────────────────────────────────────────

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) || {}),
  };
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`${API_URL}${path}`, { ...options, headers });
  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    if (res.status === 401) throw new Error("Please sign in to continue");
    if (res.status === 403)
      throw new Error(body.detail || "Your role is not allowed to do that");
    if (res.status === 429)
      throw new Error("Too many requests — the gateway is rate-limiting you");
    throw new Error(
      body.detail ||
        (res.status >= 500 ? "Server error — please try again later" : "Request failed")
    );
  }

  return body as T;
}

// ── Submitter ────────────────────────────────────────────────

export async function submitInvoice(
  payload: SubmissionPayload
): Promise<{ correlation_id: string; status: string; duplicate_of?: string | null }> {
  return request("/submissions", {
    method: "POST",
    body: JSON.stringify(payload),
    // Lets a retried submit replay the original answer instead of creating a twin.
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

export async function getSubmissionStatus(
  correlationId: string
): Promise<SubmissionStatus> {
  return request(`/submissions/${correlationId}/status`);
}

/** Answer an approver's information request and resume the workflow (F5). */
export async function provideInfo(
  correlationId: string,
  answer: string,
  updates: Record<string, unknown>
): Promise<{ status: string; revision: number; rejected_fields: string[] }> {
  return request(`/submissions/${correlationId}/info-response`, {
    method: "POST",
    body: JSON.stringify({ answer, updates }),
  });
}

// ── Approver ─────────────────────────────────────────────────

export async function listApprovals(
  status?: string
): Promise<{ approvals: ApprovalItem[] }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`/approvals${qs}`);
}

export async function approveSubmission(
  correlationId: string,
  approverEmail: string,
  comment: string
): Promise<{ status: string }> {
  return request(`/approvals/${correlationId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approver_email: approverEmail, comment }),
  });
}

export async function rejectSubmission(
  correlationId: string,
  approverEmail: string,
  comment: string
): Promise<{ status: string }> {
  return request(`/approvals/${correlationId}/reject`, {
    method: "POST",
    body: JSON.stringify({ approver_email: approverEmail, comment }),
  });
}

/**
 * Send back for more information (F5).
 * `question` and `requestedFields` are what the submitter actually sees, which is
 * why they are required rather than a generic comment.
 */
export async function sendBackSubmission(
  correlationId: string,
  approverEmail: string,
  question: string,
  requestedFields: string[] = []
): Promise<{ status: string }> {
  return request(`/approvals/${correlationId}/send-back`, {
    method: "POST",
    body: JSON.stringify({
      approver_email: approverEmail,
      question,
      requested_fields: requestedFields,
    }),
  });
}

// ── Controller / auditor ─────────────────────────────────────

export async function getDashboard(windowHours = 24): Promise<Dashboard> {
  return request(`/reports/dashboard?window_hours=${windowHours}`);
}

export async function getCeilingProof(): Promise<CeilingProof> {
  return request("/reports/ceiling-proof");
}

export async function getPolicy(): Promise<PolicyResponse> {
  return request("/admin/policy");
}

export async function updatePolicy(
  document: Record<string, unknown>
): Promise<{ version: number; status: string }> {
  return request("/admin/policy", {
    method: "PUT",
    body: JSON.stringify({ document }),
  });
}

export async function getAuditTrail(
  correlationId: string
): Promise<Record<string, unknown>> {
  return request(`/submissions/${correlationId}/audit`);
}
