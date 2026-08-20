const CORE_SCRIPT = "/vendor/chemdoodle/ChemDoodleWeb.js";
const GLOBAL_BRIDGE_SCRIPT = "/vendor/ChemDoodleWeb-global.js";
const CORE_STYLESHEET = "/vendor/chemdoodle/ChemDoodleWeb.css";

let rendererPromise: Promise<ChemDoodleApi> | null = null;
let monitorBridgeInstalled = false;

/** ChemDoodle's dynamically loaded monitor misses DOMContentLoaded on SPA startup. */
function ensureDesktopMonitorBridge(ChemDoodle: ChemDoodleApi): void {
  if (monitorBridgeInstalled || document.readyState === "loading") return;
  const monitor = ChemDoodle.monitor;
  if (!monitor) return;

  document.addEventListener("mousemove", (event) => {
    const target = monitor.CANVAS_DRAGGING;
    if (!target?.drag) return;
    target.prehandleEvent(event);
    target.drag(event);
  });
  document.addEventListener("mouseup", (event) => {
    const target = monitor.CANVAS_DRAGGING;
    if (target && target !== monitor.CANVAS_OVER && target.mouseup) {
      target.prehandleEvent(event);
      target.mouseup(event);
    }
    monitor.CANVAS_DRAGGING = undefined;
  });
  document.addEventListener("keydown", (event) => {
    monitor.SHIFT = event.shiftKey;
    monitor.ALT = event.altKey;
    monitor.META = event.metaKey || event.ctrlKey;
    const target = monitor.CANVAS_DRAGGING ?? monitor.CANVAS_OVER;
    if (target?.keydown) {
      target.prehandleEvent(event);
      target.keydown(event);
    }
  });
  document.addEventListener("keypress", (event) => {
    const target = monitor.CANVAS_DRAGGING ?? monitor.CANVAS_OVER;
    if (target?.keypress) {
      target.prehandleEvent(event);
      target.keypress(event);
    }
  });
  document.addEventListener("keyup", (event) => {
    monitor.SHIFT = event.shiftKey;
    monitor.ALT = event.altKey;
    monitor.META = event.metaKey || event.ctrlKey;
    const target = monitor.CANVAS_DRAGGING ?? monitor.CANVAS_OVER;
    if (target?.keyup) {
      target.prehandleEvent(event);
      target.keyup(event);
    }
  });
  monitorBridgeInstalled = true;
}

function ensureStylesheet(): void {
  if (document.querySelector(`link[href="${CORE_STYLESHEET}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = CORE_STYLESHEET;
  link.dataset.chemRenderer = "core-style";
  const applicationStyle = document.head.querySelector("link[rel='stylesheet'], style");
  document.head.insertBefore(link, applicationStyle);
}

function loadScript(source: string, marker: string): Promise<void> {
  const selector = `script[data-chem-renderer="${marker}"]`;
  const existing = document.querySelector<HTMLScriptElement>(selector);
  if (existing?.dataset.loaded === "true") return Promise.resolve();

  return new Promise((resolve, reject) => {
    const script = existing ?? document.createElement("script");
    const loaded = (): void => {
      script.dataset.loaded = "true";
      resolve();
    };
    const failed = (): void => {
      script.remove();
      reject(new Error(`ChemDoodle resource failed to load: ${source}`));
    };
    script.addEventListener("load", loaded, { once: true });
    script.addEventListener("error", failed, { once: true });
    if (!existing) {
      script.src = source;
      script.dataset.chemRenderer = marker;
      document.head.append(script);
    }
  });
}

/** Load the read-only ChemDoodle runtime only when a renderer is mounted. */
export function loadChemRenderer(): Promise<ChemDoodleApi> {
  if (window.ChemDoodle) {
    ensureDesktopMonitorBridge(window.ChemDoodle);
    return Promise.resolve(window.ChemDoodle);
  }
  if (rendererPromise) return rendererPromise;
  ensureStylesheet();
  rendererPromise = loadScript(CORE_SCRIPT, "core")
    .then(() => loadScript(GLOBAL_BRIDGE_SCRIPT, "global-bridge"))
    .then(() => {
      if (!window.ChemDoodle) throw new Error("ChemDoodle global bridge did not initialize");
      ensureDesktopMonitorBridge(window.ChemDoodle);
      return window.ChemDoodle;
    })
    .catch((error: unknown) => {
      rendererPromise = null;
      throw error;
    });
  return rendererPromise;
}
