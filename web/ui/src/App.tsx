import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Sidebar } from "./components/Shell";
import { Dashboard } from "./pages/Dashboard";
import { Catalog } from "./pages/Catalog";
import { CustomPod } from "./pages/CustomPod";
import { Shares } from "./pages/Shares";
import { Network } from "./pages/Network";
import { Monitor } from "./pages/Monitor";
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
          <Route path="/custom" element={<CustomPod />} />
          <Route path="/shares" element={<Shares />} />
          <Route path="/network" element={<Network />} />
          <Route path="/users" element={<Users />} />
          <Route path="/monitor" element={<Monitor />} />
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
    </BrowserRouter>
  );
}
