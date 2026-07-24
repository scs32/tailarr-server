// The Magic Stack card's signature: the stack's services as logo tiles
// physically joined by a cyan wire — a stack is a wired pipeline, not a
// bundle, and the rail draws that truth. Tile order follows the stack's
// service order (managers → downloader). Logos are bundled (offline
// installs must render them); anything without one gets a monogram tile.
const icons: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob("../assets/stack-icons/*.svg", {
      eager: true,
      query: "?url",
      import: "default",
    }),
  ).map(([path, url]) => [
    path.replace(/^.*\//, "").replace(/\.svg$/, ""),
    url as string,
  ]),
);

export function StackRail({ services }: { services: string[] }) {
  return (
    <div className="stack-rail">
      {services.map((svc, i) => (
        <span key={svc} style={{ display: "contents" }}>
          {i > 0 && <span className="stack-rail__wire" aria-hidden="true" />}
          <span className="stack-rail__tile" title={svc} aria-label={svc}>
            {icons[svc] ? (
              <img src={icons[svc]} alt="" />
            ) : (
              <span className="stack-rail__mono">
                {svc.charAt(0).toUpperCase()}
              </span>
            )}
          </span>
        </span>
      ))}
    </div>
  );
}
