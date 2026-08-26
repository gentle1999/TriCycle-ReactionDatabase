<script setup lang="ts">
import { ArrowUpRight, ChevronRight, Network } from "@lucide/vue";
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { formatEnergy, formatNumber, shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { GeometryDetail } from "@/types";

import ChemDoodleGeometry3D from "./ChemDoodleGeometry3D.vue";

const props = defineProps<{
  geometry: GeometryDetail;
  projectId?: string;
}>();

const emit = defineEmits<{ openFrame: [id: string] }>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));

function energySourceLabel(id: string | null): string {
  return id ? shortId(id) : "未选择";
}
</script>

<template>
  <div class="geometry-detail-content">
    <nav class="frame-resource-links" aria-label="几何构象关联资源">
      <RouterLink :to="{ name: 'topology-detail', params: { topologyId: geometry.topology_id }, query: navigationQuery }" title="查看分子拓扑" aria-label="查看分子拓扑">
        <Network :size="15" aria-hidden="true" /><span>分子拓扑</span>
      </RouterLink>
    </nav>
    <ChemDoodleGeometry3D :geometry-id="geometry.id" :project-id="projectId" :label="geometry.canonical_isomeric_smiles ?? undefined" :height="320" />
    <header class="geometry-detail-identity"><strong class="geometry-detail-smiles">{{ geometry.canonical_isomeric_smiles ?? "SMILES 不可用" }}</strong><code :title="geometry.id">{{ geometry.id }}</code></header>
    <dl class="energy-facts">
      <div><dt>电子能</dt><dd>{{ formatEnergy(geometry.energy_view.electronic_energy_hartree) }}</dd></div>
      <div><dt>焓 H</dt><dd>{{ formatEnergy(geometry.energy_view.enthalpy_hartree) }}</dd></div>
      <div><dt>Gibbs G</dt><dd>{{ formatEnergy(geometry.energy_view.gibbs_free_energy_hartree) }}</dd></div>
      <div><dt>熵 S</dt><dd>{{ formatNumber(geometry.energy_view.entropy_cal_mol_k, 3) }} cal/mol/K</dd></div>
      <div><dt>温度 / 压力</dt><dd>{{ geometry.energy_view.temperature_kelvin ?? "—" }} K · {{ geometry.energy_view.pressure_atm ?? "—" }} atm</dd></div>
    </dl>
    <p class="source-note">电子能来源 Frame <code>{{ energySourceLabel(geometry.energy_view.electronic_energy_source_frame_id) }}</code>；热化学校正来源 Frame <code>{{ energySourceLabel(geometry.energy_view.thermochemistry_source_frame_id) }}</code>。</p>
    <section class="geometry-frame-section">
      <header><div><span class="eyebrow">CalculationFrame</span><h3>来源帧</h3></div><span>{{ geometry.frames.length }} 帧</span></header>
      <div class="frame-list">
        <div v-for="item in geometry.frames" :key="item.id" class="frame-list-item">
          <button class="frame-list-row" type="button" @click="emit('openFrame', item.id)">
            <span class="frame-list-main"><strong>{{ item.original_filename }}</strong><small>frame {{ item.file_frame_index + 1 }} · {{ item.frame_role }}</small></span>
            <span class="frame-list-energy">{{ formatEnergy(item.selected_energy_hartree) }}</span><ChevronRight :size="15" aria-hidden="true" />
          </button>
          <RouterLink class="frame-list-direct-link" :to="{ name: 'calculation-detail', params: { frameId: item.id }, query: navigationQuery }" title="在独立页面打开" :aria-label="`在独立页面打开计算帧 ${item.id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink>
        </div>
      </div>
    </section>
  </div>
</template>
