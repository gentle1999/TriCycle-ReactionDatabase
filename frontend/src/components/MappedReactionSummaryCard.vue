<script setup lang="ts">
import { ArrowUpRight } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, reactionDepictionUrl } from "@/api";
import { formatDurationSeconds, formatNumber, shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { MappedReactionSummary } from "@/types";

const props = defineProps<{
  mapped: MappedReactionSummary;
  index: number;
  projectId: string | null;
}>();

const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));
const isOpen = ref(false);
const depictionError = ref(false);

const depictionUrl = computed(() => reactionDepictionUrl(props.mapped.mapped_reaction_smiles));
const thermodynamics = useQuery({
  queryKey: computed(() => [
    "logical-reaction-mapped-summary-thermodynamics",
    { id: props.mapped.id, projectId: props.projectId },
  ]),
  queryFn: ({ signal }) => api.mappedReactionThermodynamics(
    props.mapped.id,
    { projectId: props.projectId ?? undefined },
    signal,
  ),
  enabled: computed(() => isOpen.value && Boolean(props.projectId)),
  staleTime: 120_000,
});
const profiles = computed(() => thermodynamics.data.value?.profiles ?? []);
const thermodynamicsError = computed(() => (
  thermodynamics.error.value instanceof Error
    ? thermodynamics.error.value.message
    : ""
));

function onToggle(event: Event): void {
  isOpen.value = (event.currentTarget as HTMLDetailsElement).open;
}

function energyRange(minimum: number | null, maximum: number | null): string {
  if (minimum === null) return "暂无";
  const upper = maximum ?? minimum;
  return `${formatNumber(minimum, 2)}–${formatNumber(upper, 2)} kcal/mol`;
}
</script>

<template>
  <details class="mapped-summary-card" @toggle="onToggle">
    <summary>
      <span class="mapped-summary-index">Mapping {{ String(index + 1).padStart(2, "0") }}</span>
      <strong>{{ mapped.label || mapped.mapped_reaction_key }}</strong>
      <code :title="mapped.mapping_hash">{{ shortId(mapped.mapping_hash) }}</code>
    </summary>
    <div class="mapped-summary-content">
      <div class="mapped-summary-meta">
        <span>{{ mapped.mapped_reaction_kind }}</span>
        <code :title="mapped.id">{{ mapped.id }}</code>
        <span>ΔG‡ {{ energyRange(mapped.minimum_activation_gibbs_free_energy_kcal_mol, mapped.maximum_activation_gibbs_free_energy_kcal_mol) }}</span>
        <span>ΔG {{ energyRange(mapped.minimum_reaction_gibbs_free_energy_kcal_mol, mapped.maximum_reaction_gibbs_free_energy_kcal_mol) }}</span>
      </div>

      <figure v-if="!depictionError" class="mapped-summary-representation">
        <img
          :src="depictionUrl"
          :alt="`映射反应 ${mapped.label || mapped.mapped_reaction_key} 的反应表示图`"
          loading="lazy"
          @error="depictionError = true"
        >
        <figcaption><span>反应表示图</span><code>RDKit · 保留原子映射和立体标记</code></figcaption>
      </figure>
      <div v-else class="mapped-summary-representation-state is-error" role="img" :aria-label="`${mapped.mapped_reaction_key} 的反应表示图不可用`">
        反应表示图不可用；下方保留原始映射反应式。
      </div>
      <code class="mapped-summary-smiles">{{ mapped.mapped_reaction_smiles }}</code>

      <section class="reaction-thermodynamics mapped-summary-thermodynamics" aria-label="该映射反应的热力学">
        <header class="thermo-section-header">
          <div><span class="eyebrow">Thermodynamics</span><h3>该映射反应的热力学</h3></div>
          <span>{{ profiles.length ? `${profiles.length} 个计算 profile` : "按需加载" }}</span>
        </header>
        <div v-if="thermodynamics.isPending.value || thermodynamics.isFetching.value" class="loading-block is-wide"></div>
        <div v-else-if="thermodynamicsError" class="compact-empty is-error" role="alert">热力学读取失败：{{ thermodynamicsError }}</div>
        <div v-else-if="!profiles.length" class="compact-empty">当前映射反应没有完整热力学 profile</div>
        <div v-else class="thermo-profile-list">
          <article v-for="(profile, profileIndex) in profiles" :key="`${profile.level_of_theory}-${profile.temperature_kelvin}-${profile.pressure_atm}-${profileIndex}`" class="thermo-profile">
            <header class="thermo-profile-header"><div><span class="eyebrow">Profile {{ String(profileIndex + 1).padStart(2, "0") }}</span><code class="thermo-level">{{ profile.level_of_theory }}</code></div><span>{{ profile.temperature_kelvin }} K · {{ profile.pressure_atm }} atm</span></header>
            <div class="thermo-metrics">
              <template v-if="profile.activation">
                <div><span>ΔH‡</span><strong>{{ formatNumber(profile.activation.enthalpy_kcal_mol, 2) }}</strong><small>kcal/mol</small></div>
                <div><span>ΔG‡</span><strong>{{ formatNumber(profile.activation.gibbs_free_energy_kcal_mol, 2) }}</strong><small>kcal/mol</small></div>
                <div><span>ΔS‡</span><strong>{{ formatNumber(profile.activation.entropy_cal_mol_k, 2) }}</strong><small>cal/mol/K</small></div>
              </template>
              <template v-if="profile.reaction">
                <div><span>ΔH 反应</span><strong>{{ formatNumber(profile.reaction.enthalpy_kcal_mol, 2) }}</strong><small>kcal/mol</small></div>
                <div><span>ΔG 反应</span><strong>{{ formatNumber(profile.reaction.gibbs_free_energy_kcal_mol, 2) }}</strong><small>kcal/mol</small></div>
                <div><span>ΔS 反应</span><strong>{{ formatNumber(profile.reaction.entropy_cal_mol_k, 2) }}</strong><small>cal/mol/K</small></div>
              </template>
            </div>
            <dl class="thermo-runtime-facts" aria-label="反应路径文件计算用时">
              <div><dt>前体文件用时</dt><dd>{{ formatDurationSeconds(profile.reactants_running_time_seconds) }}</dd></div>
              <div><dt>过渡态文件用时</dt><dd>{{ formatDurationSeconds(profile.transition_state_running_time_seconds) }}</dd></div>
              <div><dt>后体文件用时</dt><dd>{{ formatDurationSeconds(profile.products_running_time_seconds) }}</dd></div>
              <div><dt>文件总用时</dt><dd>{{ formatDurationSeconds(profile.total_running_time_seconds) }}</dd></div>
            </dl>
          </article>
        </div>
      </section>

      <RouterLink class="command-button is-quiet" :to="{ name: 'mapped-reaction-detail', params: { mappedReactionId: mapped.id }, query: navigationQuery }"><ArrowUpRight :size="15" aria-hidden="true" />打开映射反应详情</RouterLink>
    </div>
  </details>
</template>
