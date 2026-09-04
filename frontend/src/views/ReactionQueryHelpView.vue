<!--
 * @Author: TMJ
 * @Date: 2026-08-18 20:09:28
 * @LastEditors: TMJ
 * @LastEditTime: 2026-08-18 20:14:28
 * @Description: 请填写简介
-->
<script setup lang="ts">
import { ArrowLeft, CircleHelp, ExternalLink } from "@lucide/vue";
import { RouterLink } from "vue-router";
</script>

<template>
  <main class="query-help-page" aria-labelledby="reaction-query-help-title">
    <header class="query-help-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'reactions' }">
          <ArrowLeft :size="15" aria-hidden="true" />返回反应路径
        </RouterLink>
        <span class="eyebrow">Reaction query reference</span>
        <h1 id="reaction-query-help-title">反应查询帮助</h1>
        <p>反应路径目录展示 LogicalReaction；其中结构检索始终先匹配由三维端点生成的 MappedReaction，再按逻辑反应归并。快速查询按相似度排序，高级查询可以组合结构、元数据、能量和时间条件。</p>
      </div>
      <div class="query-help-mark" aria-hidden="true"><CircleHelp :size="25" /></div>
    </header>

    <section class="query-help-section" aria-labelledby="query-help-quick-title">
      <header>
        <span class="eyebrow">01 · Quick query</span>
        <h2 id="query-help-quick-title">快速查询：反应 SMILES / SMARTS</h2>
      </header>
      <p>在反应路径侧栏输入映射反应的反应物和产物，用 <code>&gt;&gt;</code> 分隔，例如 <code>C=C&gt;&gt;CC</code>。后端使用 RDKit 的反应 SMARTS 解析器校验输入，因此这里也可以使用 SMARTS 原子或键条件。</p>
      <div class="query-help-example"><code>[C:1]=[C:2]>>[C:1]-[C:2]</code><span>带原子映射的反应结构模式</span></div>
        <p class="query-help-note">侧栏快速输入使用可见 MappedReaction 的反应结构指纹进行相似度排序，并显示每个 LogicalReaction 下最高的映射反应相似度；高级查询中的反应结构条件也匹配 MappedReaction.reaction 的结构包含关系，命中后只返回对应的 LogicalReaction，不要求反应字符串完全相同。</p>
    </section>

    <section class="query-help-section" aria-labelledby="query-help-rxn-title">
      <header>
        <span class="eyebrow">02 · Reaction SMARTS</span>
        <h2 id="query-help-rxn-title">RXN SMARTS 的写法</h2>
      </header>
      <dl class="query-help-list">
        <div><dt>反应物和产物</dt><dd><code>反应物&gt;&gt;产物</code>；中间有试剂时使用 <code>反应物&gt;试剂&gt;产物</code>。</dd></div>
        <div><dt>多个组分</dt><dd>同一侧用 <code>.</code> 分隔，例如 <code>CC.O&gt;&gt;CCO</code>。</dd></div>
        <div><dt>原子映射</dt><dd>用 <code>:[数字]</code> 标记对应原子，例如 <code>[C:1]</code>；需要表达键变化时建议给相关原子加映射。</dd></div>
        <div><dt>SMARTS 条件</dt><dd>可以使用 RDKit 支持的原子、键、芳香性、电荷、氢数、环和立体化学等查询属性；输入必须能被 RDKit 解析。</dd></div>
        <div><dt>限制</dt><dd>无效模板、缺少反应分隔符、超过服务端字符预算或导致候选集过大的查询会被拒绝。</dd></div>
      </dl>
    </section>

    <section class="query-help-section" aria-labelledby="query-help-fields-title">
      <header>
        <span class="eyebrow">03 · Advanced fields</span>
        <h2 id="query-help-fields-title">高级查询字段</h2>
      </header>
      <div class="query-help-table-wrap">
        <table class="query-help-table">
          <thead><tr><th>字段</th><th>输入方式</th><th>匹配含义</th></tr></thead>
          <tbody>
            <tr><th><code>reactant_smarts</code></th><td>分子 SMARTS</td><td>在 MappedReaction 的前体侧匹配，例如 <code>[C]=[C]</code>。</td></tr>
            <tr><th><code>product_smarts</code></th><td>分子 SMARTS</td><td>在 MappedReaction 的后体侧匹配，例如 <code>[C][C]</code>。</td></tr>
            <tr><th><code>rxn_smarts</code> / <code>reaction_smarts</code></th><td>完整 RXN SMARTS</td><td>在 MappedReaction 上同时约束反应物、试剂（若有）和产物；命中的映射反应再归并到 LogicalReaction。</td></tr>
            <tr><th><code>reactant_mol_block</code> / <code>product_mol_block</code></th><td>绘图编辑器或 MOL Block</td><td>先解析为分子，再作为 MappedReaction 的结构查询；它不是 SMARTS 通配查询。</td></tr>
            <tr><th><code>smarts</code></th><td>分子 SMARTS</td><td>在 MappedReaction 的前体侧或后体侧任一侧命中。</td></tr>
            <tr><th><code>reactant_product_changed</code></th><td>布尔值 <code>true</code> / <code>false</code></td><td>按前体和后体的标准分子拓扑（含顺序标准化）比较多重集合；化学计量系数也参与比较。</td></tr>
            <tr><th><code>minimum_mapped_reaction_count</code> / <code>maximum_mapped_reaction_count</code></th><td>非负整数</td><td>按逻辑反应下可见映射反应的数量设置下限或上限。</td></tr>
            <tr><th>元数据和能量</th><td>ID、名称、反应类型、自由能、创建时间</td><td>精确、范围或时间边界条件，按字段名称的“最低/最高”含义比较。</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="query-help-section" aria-labelledby="query-help-logic-title">
      <header>
        <span class="eyebrow">04 · Logic</span>
        <h2 id="query-help-logic-title">条件组合</h2>
      </header>
      <div class="query-help-logic-grid">
        <div><strong>AND</strong><p>所有条件都满足。适合把反应结构和能量范围一起筛选。</p></div>
        <div><strong>OR</strong><p>任一条件满足。适合查找多个反应类别或多个结构模式。</p></div>
        <div><strong>NOT</strong><p>勾选“排除（NOT）”后排除该条件命中的反应。</p></div>
      </div>
      <p class="query-help-note">查询帮助页说明的是当前界面能力；服务端表达式 API 还支持嵌套 <code>not</code> 节点，单个请求的嵌套深度有限制。</p>
    </section>

    <section class="query-help-section" aria-labelledby="query-help-editor-title">
      <header>
        <span class="eyebrow">05 · Structure editor</span>
        <h2 id="query-help-editor-title">绘图编辑器</h2>
      </header>
      <p>选择完整反应字段时，ChemDoodle 组件直接绘制反应并同步 RXN SMILES；选择前体或后体结构字段时，分别绘制对应一侧。完成绘制后可以继续添加其他条件，再用 AND、OR 或 NOT 组合。这里绘制的是对 MappedReaction 的查询模板，结果仍以 LogicalReaction 路径卡片呈现。</p>
      <a class="query-help-doc-link" href="https://www.rdkit.org/docs/RDKit_Book.html#smarts-support" target="_blank" rel="noreferrer">
        RDKit SMARTS 参考 <ExternalLink :size="14" aria-hidden="true" />
      </a>
    </section>
  </main>
</template>
