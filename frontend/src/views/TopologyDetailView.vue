<script setup lang="ts">
import { ArrowLeft, ArrowUpRight, Check, Clipboard, Network } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { api } from "@/api";
import ChemDoodleMolecule from "@/components/ChemDoodleMolecule.vue";
import GeometryCatalogCard from "@/components/GeometryCatalogCard.vue";
import PaginationControls from "@/components/PaginationControls.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { usePaginatedQuery } from "@/composables/usePaginatedQuery";
import { formatNumber, labelFor, shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";

const route = useRoute();
const router = useRouter();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const topologyId = computed(() => typeof route.params.topologyId === "string" ? route.params.topologyId : null);
const navigationQuery = computed(() => withoutAccessState(route.query));
const relatedPageLimit = 24;
const geometryOffset = ref(0);
const reactionOffset = ref(0);

const topologyQuery = useQuery({
  queryKey: computed(() => ["topology-detail", topologyId.value]),
  queryFn: ({ signal }) => api.topology(topologyId.value ?? "", signal),
  enabled: computed(() => topologyId.value !== null),
  staleTime: 60_000,
});

function geometryPageQueryKey(offset: number) {
  return ["topology-geometries", {
    topologyId: topologyId.value,
    projectId: currentProjectId.value,
    limit: relatedPageLimit,
    offset,
  }] as const;
}

function fetchGeometryPage(offset: number, signal: AbortSignal) {
  return api.geometries({
    projectId: currentProjectId.value ?? undefined,
    topologyId: topologyId.value ?? undefined,
    thermodynamicOnly: false,
    limit: relatedPageLimit,
    offset,
  }, signal);
}

const geometriesQuery = usePaginatedQuery({
  queryKey: computed(() => geometryPageQueryKey(geometryOffset.value)),
  enabled: computed(() => topologyId.value !== null && currentProjectId.value !== null),
  offset: geometryOffset,
  fetchPage: fetchGeometryPage,
  queryKeyForOffset: geometryPageQueryKey,
  staleTime: 60_000,
});

function reactionPageQueryKey(offset: number) {
  return ["topology-reactions", {
    topologyId: topologyId.value,
    projectId: currentProjectId.value,
    limit: relatedPageLimit,
    offset,
  }] as const;
}

function fetchReactionPage(offset: number, signal: AbortSignal) {
  return api.reactions({
    projectId: currentProjectId.value ?? undefined,
    topologyId: topologyId.value ?? undefined,
    limit: relatedPageLimit,
    offset,
  }, signal);
}

const reactionsQuery = usePaginatedQuery({
  queryKey: computed(() => reactionPageQueryKey(reactionOffset.value)),
  enabled: computed(() => topologyId.value !== null && currentProjectId.value !== null),
  offset: reactionOffset,
  fetchPage: fetchReactionPage,
  queryKeyForOffset: reactionPageQueryKey,
  staleTime: 60_000,
});

const topology = computed(() => topologyQuery.data.value ?? null);
const geometries = computed(() => geometriesQuery.data.value?.items ?? []);
const reactions = computed(() => reactionsQuery.data.value?.items ?? []);
const copiedSmiles = ref(false);
const smilesCopyError = ref("");
let smilesCopyResetTimer: number | null = null;
const geometryPage = computed(() => geometriesQuery.data.value?.page ?? {
  total: 0,
  limit: relatedPageLimit,
  offset: geometryOffset.value,
});
const reactionPage = computed(() => reactionsQuery.data.value?.page ?? {
  total: 0,
  limit: relatedPageLimit,
  offset: reactionOffset.value,
});
const detailError = computed(() => topologyQuery.error.value instanceof Error ? topologyQuery.error.value.message : "");

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

async function copyTopologySmiles(): Promise<void> {
  const smiles = topology.value?.canonical_isomeric_smiles;
  if (!smiles) return;
  smilesCopyError.value = "";
  try {
    await copyText(smiles);
    copiedSmiles.value = true;
    if (smilesCopyResetTimer !== null) window.clearTimeout(smilesCopyResetTimer);
    smilesCopyResetTimer = window.setTimeout(() => {
      copiedSmiles.value = false;
      smilesCopyResetTimer = null;
    }, 1600);
  } catch {
    copiedSmiles.value = false;
    smilesCopyError.value = "浏览器不允许自动复制，请手动选择完整 SMILES。";
  }
}

watch([currentProjectId, topologyId], () => {
  geometryOffset.value = 0;
  reactionOffset.value = 0;
});

function previousGeometryPage(): void {
  geometryOffset.value = Math.max(0, geometryOffset.value - geometryPage.value.limit);
}

function nextGeometryPage(): void {
  if (geometryOffset.value + geometryPage.value.limit < geometryPage.value.total) {
    geometryOffset.value += geometryPage.value.limit;
  }
}

function jumpGeometryPage(offset: number): void {
  geometryOffset.value = offset;
}

function previousReactionPage(): void {
  reactionOffset.value = Math.max(0, reactionOffset.value - reactionPage.value.limit);
}

function nextReactionPage(): void {
  if (reactionOffset.value + reactionPage.value.limit < reactionPage.value.total) {
    reactionOffset.value += reactionPage.value.limit;
  }
}

function jumpReactionPage(offset: number): void {
  reactionOffset.value = offset;
}

function openGeometry(id: string): void {
  void router.push({ name: "geometry-detail", params: { geometryId: id }, query: navigationQuery.value });
}

onBeforeUnmount(() => {
  if (smilesCopyResetTimer !== null) window.clearTimeout(smilesCopyResetTimer);
});
</script>

<template>
  <main class="entity-detail-page" aria-labelledby="topology-detail-title">
    <header class="entity-detail-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'geometries', query: navigationQuery }"><ArrowLeft :size="15" aria-hidden="true" />几何构象</RouterLink>
        <span class="eyebrow">MolecularTopology</span>
        <h1 id="topology-detail-title">分子拓扑</h1>
        <p>分子连接关系、描述符以及关联的构象和反应。</p>
      </div>
      <span class="entity-kind-mark" aria-hidden="true"><Network :size="22" /></span>
    </header>

    <section v-if="topologyQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="detailError" class="entity-detail-state is-error" role="alert"><strong>分子拓扑无法读取</strong><p>{{ detailError }}</p></section>
    <section v-else-if="!topology" class="entity-detail-state"><strong>分子拓扑不存在或当前项目不可见</strong></section>
    <template v-else>
      <section class="topology-overview">
        <div class="topology-structure-panel">
          <ChemDoodleMolecule :topology-id="topology.id" :label="topology.canonical_isomeric_smiles ?? undefined" :height="360" />
          <div class="topology-smiles-row">
            <code
              class="topology-smiles-value"
              :title="topology.canonical_isomeric_smiles ?? undefined"
            >{{ topology.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code>
            <button
              class="command-button command-button-muted topology-smiles-copy-button"
              type="button"
              :disabled="!topology.canonical_isomeric_smiles"
              :title="copiedSmiles ? '已复制完整 SMILES' : '复制完整 SMILES'"
              :aria-label="copiedSmiles ? '已复制完整 SMILES' : '复制完整 SMILES'"
              @click="copyTopologySmiles"
            >
              <Check v-if="copiedSmiles" :size="14" aria-hidden="true" />
              <Clipboard v-else :size="14" aria-hidden="true" />
              {{ copiedSmiles ? "已复制" : "复制" }}
            </button>
          </div>
          <p v-if="smilesCopyError" class="topology-smiles-copy-status is-error" role="alert">{{ smilesCopyError }}</p>
          <p v-else-if="copiedSmiles" class="topology-smiles-copy-status" role="status" aria-live="polite">完整 SMILES 已复制。</p>
        </div>
        <div class="topology-facts-panel">
          <header><strong>{{ topology.hill_formula }}</strong><code :title="topology.id">{{ topology.id }}</code></header>
          <dl class="topology-primary-facts">
            <div><dt>原子 / 重原子</dt><dd>{{ topology.atom_count }} / {{ topology.heavy_atom_count }}</dd></div>
            <div><dt>形式电荷</dt><dd>{{ topology.formal_charge }}</dd></div>
            <div><dt>自由基电子</dt><dd>{{ topology.radical_electron_count }}</dd></div>
            <div><dt>片段数</dt><dd>{{ topology.fragment_count }}</dd></div>
            <div><dt>立体状态</dt><dd>{{ labelFor(topology.stereo_status) }}</dd></div>
            <div><dt>净化状态</dt><dd>{{ labelFor(topology.sanitization_status) }}</dd></div>
          </dl>
          <dl class="detail-list topology-descriptors">
            <div><dt>分子量</dt><dd>{{ formatNumber(topology.molecular_weight, 4) }}</dd></div>
            <div><dt>LogP</dt><dd>{{ formatNumber(topology.logp, 4) }}</dd></div>
            <div><dt>TPSA</dt><dd>{{ formatNumber(topology.tpsa, 4) }}</dd></div>
            <div><dt>HBA / HBD</dt><dd>{{ topology.hba_count ?? "—" }} / {{ topology.hbd_count ?? "—" }}</dd></div>
            <div><dt>环数</dt><dd>{{ topology.ring_count ?? "—" }}</dd></div>
            <div><dt>骨架 SMILES</dt><dd>{{ topology.scaffold_smiles ?? "—" }}</dd></div>
            <div><dt>graph hash</dt><dd :title="topology.graph_hash">{{ shortId(topology.graph_hash) }}</dd></div>
          </dl>
        </div>
      </section>

      <section class="entity-count-band" aria-label="分子拓扑关联统计">
        <div><span>几何构象</span><strong>{{ topology.geometry_count }}</strong></div>
        <div><span>逻辑反应</span><strong>{{ topology.logical_reaction_count }}</strong></div>
        <div><span>拓扑推导</span><strong>{{ topology.derivation_count }}</strong></div>
      </section>

      <section class="entity-related-section">
        <header><div><span class="eyebrow">Geometry</span><h2>关联构象</h2></div><span>{{ geometriesQuery.data.value?.page.total ?? topology.geometry_count }} 个</span></header>
        <div v-if="geometriesQuery.isLoading.value" class="entity-related-loading"><div class="loading-block is-wide"></div></div>
        <div v-else-if="geometriesQuery.error.value" class="compact-empty">关联构象读取失败</div>
        <div v-else-if="!geometries.length" class="compact-empty">当前项目没有可见构象</div>
        <div v-else class="topology-geometry-grid">
          <GeometryCatalogCard v-for="geometry in geometries" :key="geometry.id" :geometry="geometry" :project-id="currentProjectId" :active="false" @open="openGeometry" />
        </div>
        <PaginationControls :page="geometryPage" label="关联构象分页" @previous="previousGeometryPage" @next="nextGeometryPage" @jump="jumpGeometryPage" />
      </section>

      <section class="entity-related-section">
        <header><div><span class="eyebrow">LogicalReaction</span><h2>关联反应</h2></div><span>{{ reactionsQuery.data.value?.page.total ?? topology.logical_reaction_count }} 个</span></header>
        <div v-if="reactionsQuery.isLoading.value" class="entity-related-loading"><div class="loading-block is-wide"></div></div>
        <div v-else-if="reactionsQuery.error.value" class="compact-empty">关联反应读取失败</div>
        <div v-else-if="!reactions.length" class="compact-empty">当前项目没有可见反应</div>
        <div v-else class="topology-reaction-list">
          <RouterLink v-for="reaction in reactions" :key="reaction.id" :to="{ name: 'reaction-detail', params: { logicalReactionId: reaction.id }, query: navigationQuery }">
            <span><strong>{{ reaction.label || reaction.reaction_key }}</strong><small>{{ labelFor(reaction.reaction_class) }} · {{ reaction.reactant_topology_ids.length }} → {{ reaction.product_topology_ids.length }}</small></span>
            <code>{{ shortId(reaction.id) }}</code><ArrowUpRight :size="15" aria-hidden="true" />
          </RouterLink>
        </div>
        <PaginationControls :page="reactionPage" label="关联反应分页" @previous="previousReactionPage" @next="nextReactionPage" @jump="jumpReactionPage" />
      </section>
    </template>
  </main>
</template>
