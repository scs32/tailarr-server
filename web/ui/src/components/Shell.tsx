import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { api } from "../api";
import {
  GridIcon,
  StoreIcon,
  PlusIcon,
  ShareIcon,
  NetworkIcon,
  PulseIcon,
  UsersIcon,
  GearIcon,
  TailarrMark,
} from "./Icons";

const NAV = [
  { to: "/", label: "Dashboard", icon: GridIcon, end: true },
  { to: "/catalog", label: "Catalog", icon: StoreIcon },
  { to: "/custom", label: "Custom pod", icon: PlusIcon },
  { to: "/shares", label: "Shares", icon: ShareIcon },
  { to: "/network", label: "Network", icon: NetworkIcon },
  { to: "/users", label: "Users", icon: UsersIcon },
  { to: "/monitor", label: "Monitor", icon: PulseIcon },
];

export function Sidebar() {
  const [version, setVersion] = useState("");
  const [upgrade, setUpgrade] = useState(false);
  useEffect(() => {
    api
      .info()
      .then((i) => {
        setVersion(i.version);
        setUpgrade(i.upgrade_available);
      })
      .catch(() => {});
  }, []);
  return (
    <aside className="sidebar">
      <div className="brand">
        <TailarrMark className="brand__mark" />
        <div className="brand__name">
          Tail<span>arr</span>
        </div>
      </div>
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            "nav-item" + (isActive ? " nav-item--active" : "")
          }
        >
          <Icon className="nav-icon" />
          {label}
        </NavLink>
      ))}
      <div className="spacer" />
      <NavLink
        to="/settings"
        className={({ isActive }) =>
          "nav-item" + (isActive ? " nav-item--active" : "")
        }
      >
        <GearIcon className="nav-icon" />
        Settings
      </NavLink>
      {version && (
        <div
          style={{
            padding: "var(--sp-2) var(--sp-4)",
            color: "var(--muted)",
            fontSize: "0.75rem",
          }}
        >
          Tailarr v{version}
          {upgrade && (
            <>
              {" · "}
              <NavLink to="/settings" style={{ color: "var(--accent)" }}>
                update available
              </NavLink>
            </>
          )}
        </div>
      )}
    </aside>
  );
}
