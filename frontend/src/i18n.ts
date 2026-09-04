import { createI18n } from "vue-i18n";

export const supportedLocales = ["zh-CN", "en-US"] as const;
export type SupportedLocale = (typeof supportedLocales)[number];

const LOCALE_STORAGE_KEY = "tricycle.locale";

const messages = {
  "zh-CN": {
    app: {
      navigation: {
        reactions: "反应路径",
        geometry: "几何构象",
        artifacts: "原始文件",
        statistics: "分布统计",
        organizations: "组织",
        projects: "项目",
        nexusx: "增强接口",
        account: "账户",
        login: "登录",
        logout: "退出登录",
        refresh: "刷新数据",
        apiDocs: "API 文档",
        currentProject: "当前项目",
        anonymous: "匿名访问",
        databaseUnavailable: "数据库不可用",
        connecting: "正在连接",
        accountNav: "账户导航",
        dataViews: "数据视图",
      },
      locale: {
        label: "界面语言",
        zhCN: "中文",
        enUS: "English",
      },
    },
    common: {
      none: "—",
      first: "首页",
      last: "末页",
      previous: "上一页",
      next: "下一页",
      close: "关闭",
      page: "页码",
      jump: "跳转",
      loading: "正在加载",
      status: "状态",
    },
    pagination: {
      range: "{start}-{end} / {total}",
      empty: "0 / 0",
      cursorPage: "第 {page} 页",
      pageInput: "页码",
      jump: "跳转",
    },
    auth: {
      eyebrow: "Authentication",
      title: "登录 {app}",
      description: "使用组织身份提供方登录或创建账户。首次登录后可创建组织和项目，也可以接受已有项目的邀请。",
      loginFailed: "登录失败：{error}",
      continue: "继续登录",
      publicBrowse: "返回公开文件浏览",
    },
    state: {
      workspace: "Workspace",
      authentication: "Authentication",
      notFound: "页面不存在",
      page: "页面",
      authRequired: "请先完成身份认证后访问此页面。",
      pending: "此资源页面正在接入项目访问上下文和服务端查询。",
      login: "前往登录",
      backToWorkspace: "返回反应工作区",
    },
    upload: {
      diagnostics: "Upload diagnostics",
      errorTitle: "上传错误",
      status: "状态",
      attempts: "尝试次数",
      message: "错误信息",
      previousBatch: "上一页",
      nextBatch: "下一页",
      refreshBatch: "刷新批次",
    },
    roles: {
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
    },
    artifacts: {
      calculation_output: "计算输出",
      input: "输入文件",
      workflow_manifest: "工作流清单",
      auxiliary: "辅助文件",
    },
    statuses: {
      available: "可用",
      unavailable: "不可用",
      pending: "等待中",
      queued: "等待",
      uploading: "上传中",
      parsing: "正在解析",
      succeeded: "成功",
      failed: "失败",
      partial: "部分成功",
      filtered: "已过滤（无计算帧）",
      cancelled: "已取消",
      converged: "已收敛",
      not_converged: "未收敛",
      complete: "完成",
      normal: "正常",
      ok: "正常",
      error: "错误",
    },
  },
  "en-US": {
    app: {
      navigation: {
        reactions: "Reaction paths",
        geometry: "Geometries",
        artifacts: "Raw files",
        statistics: "Statistics",
        organizations: "Organizations",
        projects: "Projects",
        nexusx: "NexusX",
        account: "Account",
        login: "Sign in",
        logout: "Sign out",
        refresh: "Refresh data",
        apiDocs: "API docs",
        currentProject: "Current project",
        anonymous: "Anonymous",
        databaseUnavailable: "Database unavailable",
        connecting: "Connecting",
        accountNav: "Account navigation",
        dataViews: "Data views",
      },
      locale: {
        label: "Language",
        zhCN: "中文",
        enUS: "English",
      },
    },
    common: {
      none: "—",
      first: "First page",
      last: "Last page",
      previous: "Previous page",
      next: "Next page",
      close: "Close",
      page: "Page",
      jump: "Go",
      loading: "Loading",
      status: "Status",
    },
    pagination: {
      range: "{start}-{end} / {total}",
      empty: "0 / 0",
      cursorPage: "Page {page}",
      pageInput: "Page",
      jump: "Go",
    },
    auth: {
      eyebrow: "Authentication",
      title: "Sign in to {app}",
      description: "Sign in or create an account with your organization's identity provider. After the first sign-in you can create organizations and projects, or accept an invitation to an existing project.",
      loginFailed: "Sign-in failed: {error}",
      continue: "Continue",
      publicBrowse: "Back to public files",
    },
    state: {
      workspace: "Workspace",
      authentication: "Authentication",
      notFound: "Page not found",
      page: "Page",
      authRequired: "Complete authentication before accessing this page.",
      pending: "This resource page is being connected to project access context and server-side queries.",
      login: "Sign in",
      backToWorkspace: "Back to reaction workspace",
    },
    upload: {
      diagnostics: "Upload diagnostics",
      errorTitle: "Upload error",
      status: "Status",
      attempts: "Attempts",
      message: "Error message",
      previousBatch: "Previous page",
      nextBatch: "Next page",
      refreshBatch: "Refresh batch",
    },
    roles: {
      initial: "Initial frame",
      intermediate: "Optimization intermediate",
      terminal: "Terminal frame",
      reactant: "Reactant",
      reactant_complex: "Reactant complex",
      transition_state: "Transition state",
      product_complex: "Product complex",
      product: "Product",
      supporting: "Supporting evidence",
      geometry_authority: "Geometry authority",
      thermochemistry_source: "Thermochemistry source",
      single_point_energy: "Single-point energy source",
      dienophile: "Dienophile",
      diene: "Diene",
    },
    artifacts: {
      calculation_output: "Calculation output",
      input: "Input file",
      workflow_manifest: "Workflow manifest",
      auxiliary: "Auxiliary file",
    },
    statuses: {
      available: "Available",
      unavailable: "Unavailable",
      pending: "Pending",
      queued: "Queued",
      uploading: "Uploading",
      parsing: "Parsing",
      succeeded: "Succeeded",
      failed: "Failed",
      partial: "Partially succeeded",
      filtered: "Filtered (no calculation frames)",
      cancelled: "Cancelled",
      converged: "Converged",
      not_converged: "Not converged",
      complete: "Complete",
      normal: "Normal",
      ok: "OK",
      error: "Error",
    },
  },
} as const;

function isSupportedLocale(value: string | null | undefined): value is SupportedLocale {
  return value !== undefined && value !== null && supportedLocales.includes(value as SupportedLocale);
}

function preferredLocale(): SupportedLocale {
  const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
  if (isSupportedLocale(stored)) return stored;
  // Keep the existing Chinese product experience as the stable default. A
  // user can opt into another locale through the app-level selector.
  return "zh-CN";
}

export const i18n = createI18n({
  legacy: false,
  locale: preferredLocale(),
  fallbackLocale: "zh-CN",
  messages,
  globalInjection: true,
  missingWarn: false,
  fallbackWarn: false,
});

export function setLocale(value: SupportedLocale): void {
  i18n.global.locale.value = value;
  window.localStorage.setItem(LOCALE_STORAGE_KEY, value);
  document.documentElement.lang = value;
}

export function currentLocale(): SupportedLocale {
  const value = i18n.global.locale.value;
  return isSupportedLocale(value) ? value : "zh-CN";
}

setLocale(currentLocale());
