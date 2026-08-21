/**
 * UUID helper that also works outside secure contexts.
 *
 * `crypto.randomUUID()` is only exposed in secure contexts (HTTPS or
 * localhost).  When the frontend is served over plain HTTP on a LAN host,
 * for example http://192.168.50.65:5173, the global is missing and views
 * that generate client ids at setup time fail to mount.
 */
export function randomUUID(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}
