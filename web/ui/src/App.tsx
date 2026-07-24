import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Sidebar } from "./components/Shell";
import { SetupOverlay } from "./components/SetupOverlay";
import { Dashboard } from "./pages/Dashboard";
import { Catalog } from "./pages/Catalog";
import { Shares } from "./pages/Shares";
import { Network } from "./pages/Network";
import { Monitor } from "./pages/Monitor";
import { Notifications } from "./pages/Notifications";
import { Stats } from "./pages/Stats";
import { Users } from "./pages/Users";
import { Settings } from "./pages/Settings";

function Layout() {
  return (
    <div className="shell">
      <Sidebar />
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/catalog" element={<Catalog />} />
          {/* deep-link: opens the catalog with the install popup for :name */}
          <Route path="/install/:name" element={<Catalog />} />
          {/* custom pods moved into the catalog (v0.16.0) */}
          <Route path="/custom" element={<Navigate to="/catalog" replace />} />
          <Route path="/shares" element={<Shares />} />
          <Route path="/network" element={<Network />} />
          <Route path="/users" element={<Users />} />
          <Route path="/monitor" element={<Monitor />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/stats" element={<Stats />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Dashboard />} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <Layout />
      <SetupOverlay />
    </BrowserRouter>
  );
}
