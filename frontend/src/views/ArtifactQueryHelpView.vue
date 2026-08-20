<script setup lang="ts">
import { ArrowLeft, CircleHelp } from "@lucide/vue";
import { RouterLink } from "vue-router";
</script>

<template>
  <main class="query-help-page" aria-labelledby="artifact-query-help-title">
    <header class="query-help-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'artifacts' }"><ArrowLeft :size="15" aria-hidden="true" />返回原始文件</RouterLink>
        <span class="eyebrow">Artifact query reference</span>
        <h1 id="artifact-query-help-title">原始文件查询帮助</h1>
        <p>快速查询用于文件 ID、SHA-256 或文件名；高级筛选组合后端 Artifact API 支持的字段。</p>
      </div>
      <div class="query-help-mark" aria-hidden="true"><CircleHelp :size="25" /></div>
    </header>
    <section class="query-help-section">
      <header><span class="eyebrow">01 · Quick query</span><h2>文件标识或名称</h2></header>
      <p>输入完整 UUID 时使用精确的文件 ID 查询；输入 64 位十六进制字符串时使用精确 SHA-256 查询；其他文本按原始文件名包含匹配。普通文件名不会因为不是 UUID 或 SHA-256 而被判为错误。</p>
      <div class="query-help-example"><code>output.log</code><span>文件名包含匹配</span></div>
    </section>
    <section class="query-help-section">
      <header><span class="eyebrow">02 · Advanced fields</span><h2>高级筛选字段</h2></header>
      <div class="query-help-table-wrap"><table class="query-help-table"><thead><tr><th>字段</th><th>匹配方式</th></tr></thead><tbody>
        <tr><th><code>artifact_id</code></th><td>UUID 精确匹配。</td></tr>
        <tr><th><code>content_sha256</code></th><td>64 位十六进制 SHA-256 精确匹配。</td></tr>
        <tr><th><code>original_filename_contains</code></th><td>文件名包含匹配。</td></tr>
        <tr><th><code>artifact_kind</code> / <code>storage_status</code></th><td>枚举精确匹配。</td></tr>
      </tbody></table></div>
      <p class="query-help-note">高级筛选中的多个字段按后端 API 的 AND 语义同时满足；当前接口不提供 OR/NOT 文件表达式。</p>
    </section>
  </main>
</template>
