/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_API_PROXY_TARGET?: string;
  readonly VITE_APP_NAME?: string;
  readonly VITE_BRAND_NAME?: string;
  readonly VITE_APP_TAGLINE?: string;
  readonly VITE_MCP_SERVER_NAME?: string;
  readonly VITE_CSRF_COOKIE_NAME?: string;
  readonly VITE_CSRF_HEADER_NAME?: string;
  readonly VITE_NEXUSX_GRAPHQL_URL?: string;
  readonly VITE_NEXUSX_CORE_URL?: string;
  readonly VITE_NEXUSX_PAGINATED_GRAPHQL_URL?: string;
  readonly VITE_NEXUSX_MCP_URL?: string;
  readonly VITE_NEXUSX_REST_URL?: string;
  readonly VITE_NEXUSX_VOYAGER_URL?: string;
}

interface ChemDoodleViewer {
  styles: Record<string, unknown>;
  loadMolecule(molecule: unknown): void;
  repaint(): void;
  resize(width: number, height: number): void;
}

interface ChemDoodleInteractionTarget {
  prehandleEvent(event: Event): void;
  drag?(event: Event): void;
  mouseup?(event: Event): void;
  keydown?(event: Event): void;
  keypress?(event: Event): void;
  keyup?(event: Event): void;
}

interface ChemDoodleMonitor {
  CANVAS_DRAGGING: ChemDoodleInteractionTarget | undefined;
  CANVAS_OVER: ChemDoodleInteractionTarget | undefined;
  ALT: boolean;
  SHIFT: boolean;
  META: boolean;
}

interface ChemDoodleAtom {
  label: string;
  charge: number;
  implicitH: number;
  x: number;
  y: number;
  z: number;
  altLabel?: string;
}

interface ChemDoodleMoleculeModel {
  atoms: ChemDoodleAtom[];
}

interface ChemDoodleApi {
  _Canvas3D: { PRESERVE_DRAWING_BUFFER: boolean };
  monitor: ChemDoodleMonitor;
  ViewerCanvas: new (id: string, width: number, height: number) => ChemDoodleViewer;
  TransformCanvas3D: new (id: string, width: number, height: number) => ChemDoodleViewer;
  readMOL(content: string, coordinateMultiplier?: number): ChemDoodleMoleculeModel;
}

interface Window {
  ChemDoodle?: ChemDoodleApi;
}
