// Shared conversions between the API's key/value maps (ports, environment,
// volumes) and the one-per-line textareas the forms edit them in.

export function parsePairs(text: string, sep: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(sep);
    if (i === -1) continue;
    const k = line.slice(0, i).trim();
    const v = line.slice(i + sep.length).trim();
    if (k) out[k] = v;
  }
  return out;
}

export function parseVolumes(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [cpath, hpath] of Object.entries(parsePairs(text, "="))) {
    if (cpath.startsWith("/") && hpath.startsWith("/")) out[cpath] = hpath;
  }
  return out;
}

export function pairsToText(map: Record<string, string>, sep: string): string {
  return Object.entries(map)
    .map(([k, v]) => `${k}${sep}${v}`)
    .join("\n");
}
