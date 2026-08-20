import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const frontendAppName = env.VITE_APP_NAME?.trim() || "Example Chemistry Database";
  const escapedFrontendAppName = frontendAppName.replace(
    /[&<>"']/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character] ?? character,
  );
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";
  const graphqlProxyTarget = env.NEXUSX_GRAPHQL_PROXY_TARGET || apiProxyTarget;
  // The combined API hosts the direct-list playground separately so its schema
  // cannot collapse into the paginated GraphQL endpoint. A dedicated NexusX
  // demo service retains its native /graphql path.
  const graphqlUpstreamPrefix = env.NEXUSX_GRAPHQL_PROXY_TARGET ? "/graphql" : "/graphql-playground";
  const nexusxProxyTarget = (name: string, fallback: string): string =>
    env[`NEXUSX_${name}_PROXY_TARGET`] || fallback;
  const stripPrefix = (publicPrefix: string, upstreamPrefix: string) => (path: string): string => {
    const suffix = path.slice(publicPrefix.length);
    if (!suffix) return upstreamPrefix;
    if (suffix === "/") return `${upstreamPrefix.replace(/\/$/, "")}/`;
    return `${upstreamPrefix.replace(/\/$/, "")}${suffix}`;
  };
  const nexusxProxy = (
    publicPrefix: string,
    target: string,
    upstreamPrefix: string,
    forwardedPrefix = publicPrefix,
    extraHeaders: Record<string, string> = {},
  ) => ({
    target,
    changeOrigin: true,
    headers: {
      ...(forwardedPrefix ? { "X-Forwarded-Prefix": forwardedPrefix } : {}),
      ...extraHeaders,
    },
    rewrite: stripPrefix(publicPrefix, upstreamPrefix),
  });
  const proxy = {
    "/api": apiProxyTarget,
    "/health": apiProxyTarget,
    "/docs": apiProxyTarget,
    "/openapi.json": apiProxyTarget,
    // Keep the GraphiQL fetcher working when it is served through /nexusx/graphql.
    "/graphql": apiProxyTarget,
    "/nexusx/graphql": nexusxProxy(
      "/nexusx/graphql",
      graphqlProxyTarget,
      graphqlUpstreamPrefix,
      "/nexusx",
    ),
    "/nexusx/core": nexusxProxy(
      "/nexusx/core",
      nexusxProxyTarget("CORE", apiProxyTarget),
      "/",
    ),
    "/nexusx/paginated-graphql": nexusxProxy(
      "/nexusx/paginated-graphql",
      nexusxProxyTarget("PAGINATED_GRAPHQL", apiProxyTarget),
      "/graphql",
      "/nexusx",
      { "X-NexusX-GraphiQL-Prefix": "/paginated-graphql" },
    ),
    "/nexusx/mcp": nexusxProxy(
      "/nexusx/mcp",
      nexusxProxyTarget("MCP", apiProxyTarget),
      "/mcp",
      "",
    ),
    "/nexusx/rest": nexusxProxy(
      "/nexusx/rest",
      nexusxProxyTarget("REST", apiProxyTarget),
      "/",
    ),
    "/nexusx/voyager": nexusxProxy(
      "/nexusx/voyager",
      nexusxProxyTarget("VOYAGER", apiProxyTarget),
      "/voyager",
      "",
    ),
  };

  return {
    plugins: [
      vue(),
      {
        name: "deployment-branding",
        transformIndexHtml: (html: string) =>
          html.replace("%DEPLOYMENT_APP_NAME%", escapedFrontendAppName),
      },
    ],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    build: { sourcemap: true },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy,
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
      proxy,
    },
  };
});
