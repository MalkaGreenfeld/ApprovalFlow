import { useState } from "react";
import { Layout, type TabName } from "./Layout";
import { SubmitForm } from "./SubmitForm";
import { StatusView } from "./StatusView";
import { ApproverQueue } from "./ApproverQueue";
import { Dashboard } from "./Dashboard";
import { PolicyAdmin } from "./PolicyAdmin";

function MainContent({ tab }: { tab: TabName }) {
  switch (tab) {
    case "submit":
      return <SubmitForm />;
    case "status":
      return <StatusView />;
    case "approvals":
      return <ApproverQueue />;
    case "dashboard":
      return <Dashboard />;
    case "policy":
      return <PolicyAdmin />;
  }
}

export default function App() {
  const [activeTab, setActiveTab] = useState<TabName>("submit");

  return (
    <Layout activeTab={activeTab} onTabChange={setActiveTab}>
      <MainContent tab={activeTab} />
    </Layout>
  );
}
