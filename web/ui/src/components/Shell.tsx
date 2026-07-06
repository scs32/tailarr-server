import { NavLink } from "react-router-dom";
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
      <a className="nav-item" aria-disabled="true">
        <GearIcon className="nav-icon" />
        Settings
      </a>
    </aside>
  );
}
