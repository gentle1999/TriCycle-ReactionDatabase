export type NexusXEndpointIcon = "graphql" | "paginated" | "mcp" | "voyager";

export interface NexusXEndpoint {
  id: string;
  name: string;
  kind: string;
  description: string;
  purpose: string;
  usage: string;
  output: string;
  mode: string;
  exampleQuery?: string;
  request: string;
  path: string;
  servicePath: string;
  url: string;
  icon: NexusXEndpointIcon;
}

function endpointUrl(environmentKey: string, proxyPath: string): string {
  const configured = import.meta.env[environmentKey]?.trim();
  if (configured) return configured;
  if (typeof window === "undefined") return proxyPath;
  return `${window.location.origin}${proxyPath}`;
}

export const nexusxEndpoints: NexusXEndpoint[] = [
  {
    id: "graphql",
    name: "Direct-list GraphQL",
    kind: "GraphQL Playground",
    description: "面向探索的只读直接列表查询，默认返回一个小型数组。",
    purpose: "先确认有哪些 Artifact、反应或计算帧，以及字段是否符合预期。",
    usage: "页面已填入可运行示例。点击 Execute Query；通过右上角 Docs 选择字段，修改 limit 后再次执行。",
    output: "JSON data/errors；GraphQLCatalogService 的 list_* 返回数组，不带 page，也不提供 mutation。",
    mode: "适合临时浏览与字段试验；不用于遍历完整目录。",
    exampleQuery: `{
  GraphQLCatalogService {
    list_artifacts(limit: 5) {
      id
      original_filename
      artifact_kind
      size_bytes
    }
  }
}`,
    request: "GET 页面 / POST 查询",
    path: "/nexusx/graphql",
    servicePath: "/graphql",
    url: endpointUrl("VITE_NEXUSX_GRAPHQL_URL", "/nexusx/graphql"),
    icon: "graphql",
  },
  {
    id: "paginated-graphql",
    name: "Paginated GraphQL",
    kind: "GraphQL",
    description: "面向应用与脚本的全量查询，列表结果始终带分页信息。",
    purpose: "按项目、状态或结构条件筛选，再稳定翻页读取 Artifact、几何、反应等资源。",
    usage: "页面已填入可运行示例。修改 limit 和 offset；同时请求 items 与 page，直到 offset 覆盖 total。",
    output: "JSON data/errors；列表返回 items + page(total、limit、offset)。",
    mode: "适合前端集成、导出和可重复脚本；支持完整的 service 查询与筛选参数。",
    exampleQuery: `{
  ArtifactQueryService {
    list_artifacts(limit: 5, offset: 0) {
      items {
        id
        original_filename
        artifact_kind
        size_bytes
      }
      page { total limit offset }
    }
  }
}`,
    request: "GET 页面 / POST 查询",
    path: "/nexusx/paginated-graphql",
    servicePath: "/graphql",
    url: endpointUrl("VITE_NEXUSX_PAGINATED_GRAPHQL_URL", "/nexusx/paginated-graphql"),
    icon: "paginated",
  },
  {
    id: "mcp",
    name: "UseCase MCP",
    kind: "MCP",
    description: "Streamable HTTP 传输与四层渐进披露工具入口。",
    purpose: "供 Claude、Cursor 等 MCP 客户端调用数据库查询工具，不是浏览器调试页面。",
    usage: "把入口 URL 配置到支持 Streamable HTTP 的 MCP 客户端；先调用 list_apps，再描述 schema/method，最后调用 compose_query。",
    output: "JSON-RPC 工具结果，以 JSON 或 text/event-stream 返回。浏览器 GET 只显示连接说明。",
    request: "POST JSON-RPC",
    path: "/nexusx/mcp/",
    servicePath: "/mcp/",
    url: endpointUrl("VITE_NEXUSX_MCP_URL", "/nexusx/mcp/"),
    icon: "mcp",
    mode: "适合由 MCP 客户端按需调用工具；不在浏览器页面中直接执行。",
  },
  {
    id: "voyager",
    name: "Voyager",
    kind: "Visualization",
    description: "UseCase 与 SQLModel 实体关系可视化。",
    purpose: "查看服务、实体、外键和查询边界之间的关系，帮助理解系统结构。",
    usage: "打开 Voyager 后切换 service/entity，点击节点查看关联；它只做可视化，不执行 CRUD。",
    output: "交互式关系图和 DOT 图数据。",
    request: "GET 可视化页面",
    path: "/nexusx/voyager/",
    servicePath: "/voyager/",
    url: endpointUrl("VITE_NEXUSX_VOYAGER_URL", "/nexusx/voyager/"),
    icon: "voyager",
    mode: "适合查看关系与服务边界；不用于查询或修改数据库。",
  },
];
