export const roleLabels: Record<string, string> = {
  initial: "初始帧",
  intermediate: "优化中间帧",
  terminal: "终止帧",
  reactant: "反应物",
  reactant_complex: "反应物复合物",
  transition_state: "过渡态",
  product_complex: "产物复合物",
  product: "产物",
  supporting: "辅助证据",
  geometry_authority: "几何权威",
  thermochemistry_source: "热化学来源",
  single_point_energy: "单点能来源",
  dienophile: "亲双烯体",
  diene: "双烯体",
};

export const artifactLabels: Record<string, string> = {
  calculation_output: "计算输出",
  input: "输入文件",
  workflow_manifest: "工作流清单",
  auxiliary: "辅助文件",
};

export function labelFor(value: string | null | undefined): string {
  if (!value) return "—";
  return roleLabels[value] ?? value.replaceAll("_", " ");
}

export function formatEnergy(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(6);
}

export function formatNumber(
  value: number | null | undefined,
  digits = 4,
): string {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

export function shortId(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : "—";
}

export function statusTone(value: string | null | undefined): string {
  if (["converged", "complete", "available", "normal", "success", "ok"].includes(value ?? "")) {
    return "ok";
  }
  if (["failed", "error", "unavailable"].includes(value ?? "")) return "bad";
  return "neutral";
}
