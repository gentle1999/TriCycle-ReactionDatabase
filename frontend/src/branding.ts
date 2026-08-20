const configured = (value: string | undefined, fallback: string): string => value?.trim() || fallback;

export const frontendAppName = configured(import.meta.env.VITE_APP_NAME, "Example Chemistry Database");
export const frontendBrandName = configured(import.meta.env.VITE_BRAND_NAME, "Example Research Platform");
export const frontendTagline = configured(import.meta.env.VITE_APP_TAGLINE, "计算数据浏览器");
export const mcpServerName = configured(
  import.meta.env.VITE_MCP_SERVER_NAME,
  "example-chemistry-database",
);
