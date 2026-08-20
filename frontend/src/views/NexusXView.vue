<script setup lang="ts">
import { Braces, Cable, Check, Clipboard, Code2, ExternalLink, KeyRound, Waypoints } from "@lucide/vue";
import { computed, onBeforeUnmount, ref } from "vue";

import { ApiError, api, apiUrl } from "@/api";
import { mcpServerName } from "@/branding";
import { nexusxEndpoints, type NexusXEndpoint } from "@/nexusx";

const endpointIcons = {
  graphql: Braces,
  paginated: Code2,
  mcp: Cable,
  voyager: Waypoints,
};

const endpointDisplayOrder = ["graphql", "paginated-graphql", "voyager", "mcp"];
const displayedEndpoints = endpointDisplayOrder
  .map((id) => nexusxEndpoints.find((endpoint) => endpoint.id === id))
  .filter((endpoint): endpoint is NexusXEndpoint => endpoint !== undefined);

function endpointIcon(endpoint: NexusXEndpoint) {
  return endpointIcons[endpoint.icon];
}

const apiDocsUrl = apiUrl("/docs");
const mcpEndpoint = nexusxEndpoints.find((endpoint) => endpoint.id === "mcp");
if (!mcpEndpoint) throw new Error("MCP endpoint metadata is missing");

interface McpClientConfig {
  id: string;
  name: string;
  file: string;
  config: string;
  note: string;
}

const mcpUrl = mcpEndpoint.url;
const jsonConfig = (value: unknown): string => JSON.stringify(value, null, 2);
const mcpClientDefinitions: Array<Omit<McpClientConfig, "config"> & { build: (authorization: string) => unknown }> = [
  {
    id: "claude-desktop",
    name: "Claude Desktop",
    file: "claude_desktop_config.json",
    build: (authorization) => ({ mcpServers: { [mcpServerName]: { url: mcpUrl, headers: { Authorization: authorization } } } }),
    note: "将 JSON 合并到 Claude Desktop 的 MCP 配置后重启客户端。",
  },
  {
    id: "cursor",
    name: "Cursor",
    file: ".cursor/mcp.json",
    build: (authorization) => ({ mcpServers: { [mcpServerName]: { url: mcpUrl, headers: { Authorization: authorization } } } }),
    note: "保存到项目 .cursor/mcp.json，或在 Cursor Settings > MCP 中粘贴同一配置。",
  },
  {
    id: "cline",
    name: "Cline",
    file: "cline_mcp_settings.json",
    build: (authorization) => ({ mcpServers: { [mcpServerName]: { url: mcpUrl, headers: { Authorization: authorization } } } }),
    note: "在 Cline MCP Servers 的配置文件中合并 mcpServers 字段，然后重新连接。",
  },
  {
    id: "windsurf",
    name: "Windsurf",
    file: "~/.codeium/windsurf/mcp_config.json",
    build: (authorization) => ({ mcpServers: { [mcpServerName]: { serverUrl: mcpUrl, headers: { Authorization: authorization } } } }),
    note: "将配置合并到 Windsurf 的 mcp_config.json，再从 MCP 面板刷新。",
  },
  {
    id: "vscode",
    name: "VS Code / Copilot",
    file: ".vscode/mcp.json",
    build: (authorization) => ({ servers: { [mcpServerName]: { type: "http", url: mcpUrl, headers: { Authorization: authorization } } } }),
    note: "保存到工作区 .vscode/mcp.json，并从 Copilot Chat 的工具列表启动。",
  },
  {
    id: "claude-code",
    name: "Claude Code",
    file: "终端命令",
    build: (authorization) => `claude mcp add --transport http --header \"Authorization: ${authorization}\" ${mcpServerName} ${mcpUrl}`,
    note: "在终端执行命令后运行 /mcp 检查连接状态。",
  },
];
const mcpAccessToken = ref<string | null>(null);
const mcpTokenName = ref("NexusX client");
const mcpTokenError = ref<string | null>(null);
const mcpTokenBusy = ref(false);
const copiedMcpToken = ref(false);
const copiedMcpHeader = ref(false);
const mcpAuthorization = computed(() => `Bearer ${mcpAccessToken.value ?? "<token>"}`);
const mcpAuthorizationHeader = computed(() => `Authorization: ${mcpAuthorization.value}`);
const mcpClientConfigs = computed<McpClientConfig[]>(() => mcpClientDefinitions.map((client) => ({
  id: client.id,
  name: client.name,
  file: client.file,
  config: jsonConfig(client.build(mcpAuthorization.value)),
  note: client.note,
})));
const activeMcpClientId = ref(mcpClientConfigs.value[0].id);
const copiedMcpClientId = ref<string | null>(null);
const mcpCopyError = ref<string | null>(null);
const copiedGraphqlEndpointId = ref<string | null>(null);
const graphqlCopyError = ref<string | null>(null);
let mcpCopyResetTimer: number | undefined;
let graphqlCopyResetTimer: number | undefined;
const activeMcpClient = computed(
  () => mcpClientConfigs.value.find((client) => client.id === activeMcpClientId.value) ?? mcpClientConfigs.value[0],
);

function selectMcpClient(id: string): void {
  activeMcpClientId.value = id;
  copiedMcpClientId.value = null;
  mcpCopyError.value = null;
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

async function copyMcpConfig(): Promise<void> {
  const client = activeMcpClient.value;
  mcpCopyError.value = null;
  try {
    await copyText(client.config);
    copiedMcpClientId.value = client.id;
    if (mcpCopyResetTimer !== undefined) window.clearTimeout(mcpCopyResetTimer);
    mcpCopyResetTimer = window.setTimeout(() => {
      copiedMcpClientId.value = null;
    }, 1800);
  } catch {
    mcpCopyError.value = "浏览器不允许自动复制，请手动选择上方配置文本。";
  }
}

async function copyGraphqlExample(endpoint: NexusXEndpoint): Promise<void> {
  if (!endpoint.exampleQuery) return;
  graphqlCopyError.value = null;
  try {
    await copyText(endpoint.exampleQuery);
    copiedGraphqlEndpointId.value = endpoint.id;
    if (graphqlCopyResetTimer !== undefined) window.clearTimeout(graphqlCopyResetTimer);
    graphqlCopyResetTimer = window.setTimeout(() => {
      copiedGraphqlEndpointId.value = null;
    }, 1800);
  } catch {
    graphqlCopyError.value = "浏览器不允许自动复制，请手动选择查询文本。";
  }
}

async function generateMcpToken(): Promise<void> {
  mcpTokenBusy.value = true;
  mcpTokenError.value = null;
  try {
    const result = await api.createMcpToken({ name: mcpTokenName.value.trim() || "NexusX client" });
    if (!result) throw new Error("MCP token response was empty");
    mcpAccessToken.value = result.access_token;
    copiedMcpToken.value = false;
    copiedMcpHeader.value = false;
  } catch (error) {
    mcpTokenError.value = error instanceof ApiError ? error.message : "MCP token 创建失败。";
  } finally {
    mcpTokenBusy.value = false;
  }
}

async function copyMcpToken(): Promise<void> {
  if (!mcpAccessToken.value) return;
  try {
    await copyText(mcpAccessToken.value);
    copiedMcpToken.value = true;
  } catch {
    mcpTokenError.value = "浏览器不允许自动复制，请手动选择 token。";
  }
}

async function copyMcpHeader(): Promise<void> {
  try {
    await copyText(mcpAuthorizationHeader.value);
    copiedMcpHeader.value = true;
  } catch {
    mcpTokenError.value = "浏览器不允许自动复制，请手动选择 Authorization。";
  }
}

onBeforeUnmount(() => {
  if (mcpCopyResetTimer !== undefined) window.clearTimeout(mcpCopyResetTimer);
  if (graphqlCopyResetTimer !== undefined) window.clearTimeout(graphqlCopyResetTimer);
});
</script>

<template>
  <main class="nexusx-page" aria-labelledby="nexusx-page-title">
    <header class="page-heading">
      <span class="eyebrow">NexusX transports</span>
      <h1 id="nexusx-page-title">增强接口</h1>
      <p>统一入口访问 GraphQL、MCP、Voyager 等 NexusX 增强能力；项目 API 文档集中提供 REST 接口。</p>
    </header>

    <section class="nexusx-launch-band" aria-label="NexusX 服务启动提示">
      <div>
        <span class="eyebrow">Local services</span>
        <h2>开发接口入口</h2>
        <p>浏览器只访问当前前端 origin，默认由组合 API 提供全部入口；本地后端端口仅供代理连接。</p>
        <div class="nexusx-runtime-list">
          <span><strong>推荐</strong> <code>make serve</code> + <code>make serve-frontend</code></span>
          <span><strong>独立演示</strong> <code>make serve-nexusx</code>（不要与组合 API 同时占用 8000）</span>
        </div>
      </div>
      <a class="command-button nexusx-docs-link" :href="apiDocsUrl" target="_blank" rel="noreferrer">
        项目 API 文档 <ExternalLink :size="14" aria-hidden="true" />
      </a>
    </section>

    <section class="nexusx-endpoint-grid" aria-label="NexusX 服务列表">
      <article
        v-for="endpoint in displayedEndpoints"
        :key="endpoint.id"
        class="nexusx-endpoint-card"
        :class="`nexusx-endpoint-card--${endpoint.id}`"
      >
        <header class="nexusx-endpoint-heading">
          <span class="nexusx-endpoint-icon" aria-hidden="true"><component :is="endpointIcon(endpoint)" :size="19" /></span>
          <div>
            <span class="eyebrow">{{ endpoint.kind }}</span>
            <h2>{{ endpoint.name }}</h2>
          </div>
        </header>
        <p>{{ endpoint.description }}</p>
        <dl class="nexusx-endpoint-details">
          <div>
            <dt>用途</dt>
            <dd>{{ endpoint.purpose }}</dd>
          </div>
          <div>
            <dt>使用</dt>
            <dd>{{ endpoint.usage }}</dd>
          </div>
          <div>
            <dt>返回</dt>
            <dd>{{ endpoint.output }}</dd>
          </div>
          <div>
            <dt>适用</dt>
            <dd>{{ endpoint.mode }}</dd>
          </div>
        </dl>
        <section v-if="endpoint.exampleQuery" class="nexusx-graphql-start" aria-label="GraphQL 起步查询">
          <header class="nexusx-graphql-start-heading">
            <div>
              <span class="eyebrow">Start here</span>
              <h3>起步查询</h3>
            </div>
            <button class="command-button nexusx-copy-button" type="button" @click="copyGraphqlExample(endpoint)">
              <Check v-if="copiedGraphqlEndpointId === endpoint.id" :size="14" aria-hidden="true" />
              <Clipboard v-else :size="14" aria-hidden="true" />
              {{ copiedGraphqlEndpointId === endpoint.id ? "已复制" : "复制查询" }}
            </button>
          </header>
          <p>打开入口时已自动填入。点击 Execute Query 查看结果；从右上角 Docs 选择字段后再次执行。</p>
          <pre class="nexusx-graphql-snippet"><code>{{ endpoint.exampleQuery }}</code></pre>
          <p v-if="graphqlCopyError" class="nexusx-mcp-copy-error" role="alert">{{ graphqlCopyError }}</p>
        </section>
        <section v-if="endpoint.id === 'mcp'" class="nexusx-mcp-config" aria-labelledby="mcp-config-title">
          <header class="nexusx-mcp-config-heading">
            <div>
              <span class="eyebrow">Client setup</span>
              <h3 id="mcp-config-title">一键复制配置</h3>
            </div>
            <code>{{ endpoint.url }}</code>
          </header>
          <section class="nexusx-mcp-token-panel" aria-labelledby="mcp-token-title">
            <div class="nexusx-mcp-token-heading">
              <div>
                <span class="eyebrow">Authentication</span>
                <h4 id="mcp-token-title">登录后生成 MCP Token</h4>
              </div>
              <KeyRound :size="18" aria-hidden="true" />
            </div>
            <p class="nexusx-mcp-token-help">外部客户端不能使用浏览器 Cookie。生成后把配置片段中的 Bearer 值一起复制到客户端。</p>
            <div class="nexusx-mcp-token-actions">
              <label>Token 名称<input v-model="mcpTokenName" maxlength="128" /></label>
              <button class="command-button" type="button" :disabled="mcpTokenBusy" @click="generateMcpToken">
                {{ mcpTokenBusy ? "生成中…" : "生成 Token" }}
              </button>
            </div>
            <div v-if="mcpAccessToken" class="nexusx-mcp-token-result">
              <span class="eyebrow">仅显示本次生成的原文</span>
              <code class="nexusx-mcp-token-value">{{ mcpAccessToken }}</code>
              <div class="nexusx-mcp-token-copy-actions">
                <button class="command-button" type="button" @click="copyMcpToken">
                  <Check v-if="copiedMcpToken" :size="14" aria-hidden="true" />
                  <Clipboard v-else :size="14" aria-hidden="true" />
                  {{ copiedMcpToken ? "Token 已复制" : "复制 Token" }}
                </button>
                <button class="command-button" type="button" @click="copyMcpHeader">
                  <Check v-if="copiedMcpHeader" :size="14" aria-hidden="true" />
                  <Clipboard v-else :size="14" aria-hidden="true" />
                  {{ copiedMcpHeader ? "Header 已复制" : "复制 Authorization" }}
                </button>
              </div>
              <p class="nexusx-mcp-token-header"><code>{{ mcpAuthorizationHeader }}</code></p>
              <p class="nexusx-mcp-token-warning">原文不会再次从服务端返回；离开此页前请完成客户端配置。需要重新显示时请撤销后生成新 Token。</p>
            </div>
            <p v-if="mcpTokenError" class="nexusx-mcp-copy-error" role="alert">{{ mcpTokenError }}</p>
          </section>
          <div class="nexusx-mcp-client-tabs" role="tablist" aria-label="MCP 客户端">
            <button
              v-for="client in mcpClientConfigs"
              :id="`mcp-client-${client.id}`"
              :key="client.id"
              class="nexusx-mcp-client-tab"
              :class="{ 'is-active': activeMcpClient.id === client.id }"
              type="button"
              role="tab"
              :aria-selected="activeMcpClient.id === client.id"
              @click="selectMcpClient(client.id)"
            >
              {{ client.name }}
            </button>
          </div>
          <pre class="nexusx-mcp-snippet"><code>{{ activeMcpClient.config }}</code></pre>
          <div class="nexusx-mcp-config-footer">
            <span>文件：<code>{{ activeMcpClient.file }}</code></span>
            <button class="command-button nexusx-copy-button" type="button" @click="copyMcpConfig">
              <Check v-if="copiedMcpClientId === activeMcpClient.id" :size="14" aria-hidden="true" />
              <Clipboard v-else :size="14" aria-hidden="true" />
              {{ copiedMcpClientId === activeMcpClient.id ? "已复制" : "复制配置" }}
            </button>
          </div>
          <p class="nexusx-mcp-client-note">{{ activeMcpClient.note }}</p>
          <p v-if="mcpCopyError" class="nexusx-mcp-copy-error" role="alert">{{ mcpCopyError }}</p>
          <p class="nexusx-mcp-auth-note">配置片段已包含 <code>Authorization: Bearer &lt;token&gt;</code>。尖括号占位符仅在尚未生成 Token 时显示。</p>
        </section>
        <div class="nexusx-endpoint-meta">
          <span>{{ endpoint.request }}</span>
          <code>{{ endpoint.path }}</code>
        </div>
        <a class="command-button nexusx-open-link" :href="endpoint.url" target="_blank" rel="noreferrer">
          打开入口 <ExternalLink :size="14" aria-hidden="true" />
        </a>
        <code class="nexusx-endpoint-url">{{ endpoint.url }}</code>
      </article>
    </section>

    <p class="nexusx-note">接口沿用当前账户认证和服务端权限边界；MCP 地址供支持 Streamable HTTP 的客户端使用。</p>
  </main>
</template>
