import { currentLocale, i18n } from "@/i18n";

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

const statusLabels: Record<string, string> = {
  available: "可用",
  unavailable: "不可用",
  pending: "等待中",
  queued: "等待",
  uploading: "上传中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  converged: "已收敛",
  not_converged: "未收敛",
  complete: "完成",
  normal: "正常",
  ok: "正常",
  error: "错误",
};

export function labelFor(value: string | null | undefined): string {
  if (!value) return i18n.global.t("common.none");
  const group = Object.hasOwn(roleLabels, value)
    ? "roles"
    : Object.hasOwn(artifactLabels, value)
    ? "artifacts"
    : Object.hasOwn(statusLabels, value)
    ? "statuses"
    : null;
  const translationKey = group ? `${group}.${value}` : null;
  if (translationKey && i18n.global.te(translationKey)) return i18n.global.t(translationKey);
  return roleLabels[value] ?? artifactLabels[value] ?? statusLabels[value] ?? value.replaceAll("_", " ");
}

export function formatEnergy(value: number | null | undefined): string {
  return formatNumber(value, 6);
}

export function formatNumber(
  value: number | null | undefined,
  digits = 4,
): string {
  if (value === null || value === undefined) return i18n.global.t("common.none");
  return new Intl.NumberFormat(currentLocale(), {
    useGrouping: false,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return i18n.global.t("common.none");
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = size >= 10 || unit === 0 ? 0 : 1;
  const formatted = new Intl.NumberFormat(currentLocale(), {
    useGrouping: false,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(size);
  return `${formatted} ${units[unit]}`;
}

export function shortId(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : i18n.global.t("common.none");
}

export function formatDateTime(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === "") return i18n.global.t("common.none");
  return new Intl.DateTimeFormat(currentLocale(), {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatDate(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === "") return i18n.global.t("common.none");
  return new Intl.DateTimeFormat(currentLocale(), { dateStyle: "medium" }).format(new Date(value));
}

export function statusTone(value: string | null | undefined): string {
  if (["converged", "complete", "available", "normal", "success", "ok"].includes(value ?? "")) {
    return "ok";
  }
  if (["failed", "error", "unavailable"].includes(value ?? "")) return "bad";
  return "neutral";
}
