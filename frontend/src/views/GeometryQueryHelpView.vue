<script setup lang="ts">
import { ArrowLeft, CircleHelp, ExternalLink } from "@lucide/vue";
import { RouterLink } from "vue-router";
</script>

<template>
  <main class="query-help-page" aria-labelledby="geometry-query-help-title">
    <header class="query-help-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'geometries' }"><ArrowLeft :size="15" aria-hidden="true" />返回几何构象</RouterLink>
        <span class="eyebrow">Geometry query reference</span>
        <h1 id="geometry-query-help-title">几何构象查询帮助</h1>
        <p>快速查询适合单个拓扑 SMILES；高级查询可以组合拓扑、来源、角色和计算属性。</p>
      </div>
      <div class="query-help-mark" aria-hidden="true"><CircleHelp :size="25" /></div>
    </header>
    <section class="query-help-section">
      <header><span class="eyebrow">01 · Quick query</span><h2>拓扑 SMILES</h2></header>
      <p>在侧栏输入分子 SMILES，例如 <code>CCO</code>。输入框右侧图标表示 RDKit 格式校验状态；构象查询只会在提交有效结构后执行。</p>
      <div class="query-help-example"><code>c1ccccc1</code><span>苯的拓扑 SMILES</span></div>
    </section>
    <section class="query-help-section">
      <header><span class="eyebrow">02 · Structure fields</span><h2>结构条件</h2></header>
      <div class="query-help-table-wrap"><table class="query-help-table"><thead><tr><th>字段</th><th>输入方式</th><th>匹配含义</th></tr></thead><tbody>
        <tr><th><code>topology_smiles</code></th><td>ChemDoodle 绘图或 SMILES</td><td>按拓扑结构匹配。</td></tr>
        <tr><th><code>topology_smarts</code></th><td>SMARTS 文本</td><td>按 RDKit SMARTS 子结构匹配。</td></tr>
        <tr><th><code>topology_mol_block</code></th><td>MOL Block 文本</td><td>解析 MOL Block 后按结构匹配。</td></tr>
      </tbody></table></div>
    </section>
    <section class="query-help-section">
      <header><span class="eyebrow">03 · Metadata</span><h2>元数据与逻辑</h2></header>
      <p>高级查询还支持拓扑 ID、几何 hash、坐标 hash、规范化版本、派生 ID、反应节点角色、热力学属性、虚频状态和原子数范围。条件可用 AND、OR 组合，并可单独勾选 NOT。</p>
      <a class="query-help-doc-link" href="https://www.rdkit.org/docs/RDKit_Book.html#smarts-support" target="_blank" rel="noreferrer">RDKit SMARTS 参考 <ExternalLink :size="14" aria-hidden="true" /></a>
    </section>
  </main>
</template>
