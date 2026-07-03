import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Sidebar } from "./components/Shell";
import { Dashboard } from "./pages/Dashboard";
import { Catalog } from "./pages/Catalog";
import { InstallForm } from "./pages/InstallForm";
import { CustomPod } from "./pages/CustomPod";
import { Shares } from "./pages/Shares";
import { Network } from "./pages/Network";

function Layout() {
  return (
    <div className="shell">
      <Sidebar />
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/install/:name" element={<InstallForm />} />
          <Route path="/custom" element={<CustomPod />} />
          <Route path="/shares" element={<Shares />} />
          <Route path="/network" element={<Network />} />
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
