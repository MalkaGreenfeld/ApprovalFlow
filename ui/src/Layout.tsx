import { useState, type ReactNode } from "react";
import { currentIdentity, signIn, signOut, type Identity, type Role } from "./api";

export type TabName = "submit" | "status" | "approvals" | "dashboard" | "policy";

const TABS: { key: TabName; label: string; roles: Role[] }[] = [
  { key: "submit", label: "New Submission", roles: ["submitter"] },
  { key: "status", label: "Track Status", roles: ["submitter"] },
  { key: "approvals", label: "Approval Queue", roles: ["approver"] },
  { key: "dashboard", label: "Dashboard", roles: ["approver", "admin"] },
  { key: "policy", label: "Policy", roles: ["admin"] },
];

/** Demo identities, one per role, so the role model is visible in the UI (N1). */
const DEMO_USERS: { label: string; subject: string; roles: Role[] }[] = [
  { label: "Submitter", subject: "dana.cohen@northwind.example", roles: ["submitter"] },
  { label: "Approver", subject: "manager@northwind.example", roles: ["approver", "submitter"] },
  { label: "Controller (admin)", subject: "controller@northwind.example", roles: ["admin"] },
];

interface LayoutProps {
  activeTab: TabName;
  onTabChange: (tab: TabName) => void;
  children: ReactNode;
}

function tabClass(active: boolean): string {
  const base =
    "px-3 py-2 rounded-md text-sm transition-colors cursor-pointer border-none bg-transparent";
  return active
    ? `${base} bg-white/20 text-white font-semibold`
    : `${base} text-blue-100 hover:bg-white/10`;
}

export function Layout({ activeTab, onTabChange, children }: LayoutProps) {
  const [identity, setIdentity] = useState<Identity | null>(currentIdentity());
  const [authError, setAuthError] = useState("");

  const handleSignIn = async (index: number) => {
    setAuthError("");
    const user = DEMO_USERS[index];
    try {
      setIdentity(await signIn(user.subject, user.roles));
    } catch (e: unknown) {
      setAuthError(e instanceof Error ? e.message : "Sign-in failed");
    }
  };

  const handleSignOut = () => {
    signOut();
    setIdentity(null);
  };

  return (
    <div className="min-h-screen flex flex-col font-sans">
      <header className="bg-slate-800 text-white px-6 py-4 flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-xl font-bold m-0">ApprovalFlow</h1>

        <nav className="flex gap-1 flex-wrap">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              className={tabClass(activeTab === tab.key)}
              aria-current={activeTab === tab.key ? "page" : undefined}
              onClick={() => onTabChange(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <div className="flex items-center gap-2 text-sm">
          {identity ? (
            <>
              <span className="text-blue-100">
                {identity.subject}{" "}
                <span className="opacity-70">({identity.roles.join(", ")})</span>
              </span>
              <button
                className="px-3 py-1.5 rounded-md bg-white/15 text-white border-none cursor-pointer hover:bg-white/25"
                onClick={handleSignOut}
              >
                Sign out
              </button>
            </>
          ) : (
            <select
              id="sign-in-as"
              name="sign-in-as"
              aria-label="Sign in as"
              defaultValue=""
              className="px-2 py-1.5 rounded-md bg-white text-slate-800 text-sm"
              style={{ colorScheme: "light" }}
              onChange={(e) => e.target.value !== "" && handleSignIn(Number(e.target.value))}
            >
              <option value="">Sign in as…</option>
              {DEMO_USERS.map((user, index) => (
                <option key={user.subject} value={index}>
                  {user.label}
                </option>
              ))}
            </select>
          )}
        </div>
      </header>

      {authError && (
        <div className="bg-red-50 text-red-700 px-6 py-2 text-sm">{authError}</div>
      )}

      <main className="flex-1 p-6 max-w-5xl mx-auto w-full box-border">{children}</main>
    </div>
  );
}
