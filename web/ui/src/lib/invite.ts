// Suite-invite link — the SAME payload the Tailarr app's key sheet
// generates, so a family member can scan this QR with the app to enroll
// AND self-configure in one step. The invite rides the URL *fragment* of
// https://tailarr.com/import, so it never reaches tailarr.com's server;
// the app decodes it on-device. Must stay in sync with the app's
// SharedModuleConfiguration.invite() (module key "tailarr_server", v1).
const IMPORT_URL = "https://tailarr.com/import";
const MODULE_TAILARR_SERVER = "tailarr_server";
const INVITE_VERSION = 1;

// base64url of the UTF-8 JSON, padding stripped — matches Dart's
// base64Url.encode(utf8.encode(...)).replaceAll('=', '').
function base64url(json: string): string {
  const utf8 = new TextEncoder().encode(json);
  let bin = "";
  for (const b of utf8) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// A suite invite for the controller reached at `host` (the origin the
// admin is viewing — the address a freshly enrolled device will use)
// carrying the person's single-use enrollment key.
export function inviteLink(host: string, enrollKey: string): string {
  const payload = {
    v: INVITE_VERSION,
    module: MODULE_TAILARR_SERVER,
    host,
    enroll: { key: enrollKey },
  };
  return `${IMPORT_URL}#${base64url(JSON.stringify(payload))}`;
}
