const PROTOCOL = "tricycle-chemdoodle-editor/1";

type EditorMessage =
  | { protocol: typeof PROTOCOL; type: "ready" }
  | { protocol: typeof PROTOCOL; type: "change"; molfile: string; smiles: string }
  | { protocol: typeof PROTOCOL; type: "reactionChange"; rxn: string }
  | { protocol: typeof PROTOCOL; type: "layout"; height: number }
  | { protocol: typeof PROTOCOL; type: "response"; requestId: number; value: string }
  | { protocol: typeof PROTOCOL; type: "error"; message: string };

type EditorCommand =
  | { command: "loadMolfile"; molfile: string }
  | { command: "loadRxn"; rxn: string }
  | { command: "getMolfile" }
  | { command: "getSmiles" }
  | { command: "getRxn" }
  | { command: "clear" }
  | { command: "resize"; width: number; height: number };

export type EditorChangeHandler = (value: { molfile: string; smiles: string }) => void;
export type EditorReactionChangeHandler = (value: { rxn: string }) => void;
export type EditorReadyHandler = () => void;
export type EditorLayoutHandler = (value: { height: number }) => void;
export type EditorErrorHandler = (message: string) => void;

/** Typed, source-checked postMessage boundary for the isolated ChemDoodle editor. */
export class ChemDoodleEditorBridge {
  private requestId = 0;
  private latestMolfile = "";
  private latestSmiles = "";
  private latestRxn = "";
  private readonly pendingRequests = new Map<
    number,
    { command: "getMolfile" | "getSmiles" | "getRxn"; resolve: (value: string) => void; timer: number }
  >();
  private changeHandler: EditorChangeHandler | null = null;
  private reactionChangeHandler: EditorReactionChangeHandler | null = null;
  private readyHandler: EditorReadyHandler | null = null;
  private layoutHandler: EditorLayoutHandler | null = null;
  private errorHandler: EditorErrorHandler | null = null;
  private readonly onMessage = (event: MessageEvent<unknown>): void => {
    if (event.source !== this.iframe.contentWindow || !this.isEditorMessage(event.data)) return;
    const message = event.data;
    if (message.type === "change") {
      this.latestMolfile = message.molfile;
      this.latestSmiles = message.smiles;
      this.changeHandler?.({ molfile: message.molfile, smiles: message.smiles });
    } else if (message.type === "reactionChange") {
      this.latestRxn = message.rxn;
      this.reactionChangeHandler?.({ rxn: message.rxn });
    } else if (message.type === "ready") {
      this.readyHandler?.();
    } else if (message.type === "layout") {
      this.layoutHandler?.({ height: message.height });
    } else if (message.type === "response") {
      const pending = this.pendingRequests.get(message.requestId);
      if (!pending) return;
      window.clearTimeout(pending.timer);
      this.pendingRequests.delete(message.requestId);
      if (pending.command === "getMolfile") this.latestMolfile = message.value;
      else if (pending.command === "getSmiles") this.latestSmiles = message.value;
      else this.latestRxn = message.value;
      pending.resolve(message.value);
    } else if (message.type === "error") {
      this.errorHandler?.(message.message);
    }
  };

  constructor(private readonly iframe: HTMLIFrameElement) {
    window.addEventListener("message", this.onMessage);
  }

  loadMolfile(molfile: string): void {
    this.send({ command: "loadMolfile", molfile });
  }

  loadRxn(rxn: string): void {
    this.send({ command: "loadRxn", rxn });
  }

  getMolfile(): Promise<string> {
    return this.requestValue("getMolfile", this.latestMolfile);
  }

  getSmiles(): Promise<string> {
    return this.requestValue("getSmiles", this.latestSmiles);
  }

  getRxn(): Promise<string> {
    return this.requestValue("getRxn", this.latestRxn);
  }

  clear(): void {
    this.latestMolfile = "";
    this.latestSmiles = "";
    this.latestRxn = "";
    this.send({ command: "clear" });
  }

  resize(width: number, height: number): void {
    this.send({ command: "resize", width, height });
  }

  onChange(handler: EditorChangeHandler | null): void {
    this.changeHandler = handler;
  }

  onReactionChange(handler: EditorReactionChangeHandler | null): void {
    this.reactionChangeHandler = handler;
  }

  onReady(handler: EditorReadyHandler | null): void {
    this.readyHandler = handler;
  }

  onLayout(handler: EditorLayoutHandler | null): void {
    this.layoutHandler = handler;
  }

  onError(handler: EditorErrorHandler | null): void {
    this.errorHandler = handler;
  }

  destroy(): void {
    window.removeEventListener("message", this.onMessage);
    for (const pending of this.pendingRequests.values()) {
      window.clearTimeout(pending.timer);
      pending.resolve(
        pending.command === "getMolfile"
          ? this.latestMolfile
          : pending.command === "getSmiles"
            ? this.latestSmiles
            : this.latestRxn,
      );
    }
    this.pendingRequests.clear();
    this.changeHandler = null;
    this.reactionChangeHandler = null;
    this.readyHandler = null;
    this.layoutHandler = null;
    this.errorHandler = null;
  }

  private send(command: EditorCommand): void {
    if (!this.iframe.contentWindow) return;
    this.iframe.contentWindow.postMessage(
      { protocol: PROTOCOL, type: "command", requestId: ++this.requestId, ...command },
      "*",
    );
  }

  private requestValue(
    command: "getMolfile" | "getSmiles" | "getRxn",
    fallback: string,
  ): Promise<string> {
    const target = this.iframe.contentWindow;
    if (!target) return Promise.resolve(fallback);
    const requestId = ++this.requestId;
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        this.pendingRequests.delete(requestId);
        resolve(fallback);
      }, 1_000);
      this.pendingRequests.set(requestId, { command, resolve, timer });
      target.postMessage({ protocol: PROTOCOL, type: "command", requestId, command }, "*");
    });
  }

  private isEditorMessage(value: unknown): value is EditorMessage {
    if (!value || typeof value !== "object") return false;
    const candidate = value as Record<string, unknown>;
    if (candidate.protocol !== PROTOCOL || typeof candidate.type !== "string") return false;
    if (candidate.type === "ready") return true;
    if (candidate.type === "change") {
      return typeof candidate.molfile === "string" && typeof candidate.smiles === "string";
    }
    if (candidate.type === "reactionChange") {
      return typeof candidate.rxn === "string";
    }
    if (candidate.type === "layout") {
      return typeof candidate.height === "number" && Number.isFinite(candidate.height) && candidate.height > 0;
    }
    if (candidate.type === "response") {
      return typeof candidate.requestId === "number" && typeof candidate.value === "string";
    }
    return candidate.type === "error" && typeof candidate.message === "string";
  }
}
