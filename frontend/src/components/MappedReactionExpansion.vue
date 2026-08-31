<script setup lang="ts">
import { ArrowRight, ArrowUpRight, ChevronRight } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, defineAsyncComponent, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import { formatDurationSeconds, formatEnergy, formatNumber, labelFor, shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type {
  LogicalReactionDetail,
  MappedReactionDetail,
  MappedReactionNode,
  MappedReactionNodeGeometry,
} from "@/types";

import ChemDoodleGeometry3D from "./ChemDoodleGeometry3D.vue";
import ChemDoodleMolecule from "./ChemDoodleMolecule.vue";
import type { ReactionPotentialEnergyProfile } from "./ReactionPotentialEnergyDiagram.vue";

const ReactionPotentialEnergyDiagram = defineAsyncComponent(() => import("./ReactionPotentialEnergyDiagram.vue"));

const props = defineProps<{
  reaction: LogicalReactionDetail;
  mappedReaction: MappedReactionDetail | null;
  selectedMappedId: string | null;
  mappedLoading: boolean;
  projectId: string | null;
}>();

const emit = defineEmits<{
  selectMapped: [id: string];
  openFrame: [id: string];
}>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));

const nodeRoleOrder: Record<string, number> = {
  reactant: 0,
  reactant_complex: 1,
  transition_state: 2,
  intermediate: 3,
  product_complex: 4,
  product: 5,
  other: 6,
};

const activeNodeId = ref<string | null>(null);
const logicalParticipantsById = computed(() => new Map(
  props.reaction.participants.map((participant) => [participant.id, participant]),
));
const mappedReactants = computed(() => [...(props.mappedReaction?.participants ?? [])]
  .filter((participant) => participant.side === "reactant")
  .sort((left, right) => left.template_index - right.template_index));
const mappedProducts = computed(() => [...(props.mappedReaction?.participants ?? [])]
  .filter((participant) => participant.side === "product")
  .sort((left, right) => left.template_index - right.template_index));
const orderedNodes = computed(() => [...(props.mappedReaction?.nodes ?? [])].sort((left, right) => {
  const roleDifference = (nodeRoleOrder[left.role] ?? nodeRoleOrder.other) - (nodeRoleOrder[right.role] ?? nodeRoleOrder.other);
  return roleDifference || left.node_index - right.node_index;
}));
const selectedNode = computed<MappedReactionNode | null>(() => {
  const nodes = orderedNodes.value;
  return nodes.find((node) => node.id === activeNodeId.value) ?? nodes[0] ?? null;
});
const selectedGeometryRows = computed(() => {
  const rows = new Map<string, {
    key: string;
    componentIndex: number;
    componentKey: string;
    participantRole: string | null;
    geometries: MappedReactionNodeGeometry[];
  }>();
  for (const geometry of selectedNode.value?.geometries ?? []) {
    const key = `${geometry.component_index}:${geometry.component_key}`;
    const row = rows.get(key) ?? {
      key,
      componentIndex: geometry.component_index,
      componentKey: geometry.component_key,
      participantRole: geometry.participant_role,
      geometries: [],
    };
    row.geometries.push(geometry);
    rows.set(key, row);
  }
  return [...rows.values()]
    .sort((left, right) => left.componentIndex - right.componentIndex || left.componentKey.localeCompare(right.componentKey))
    .map((row) => ({
      ...row,
      geometries: [...row.geometries].sort((left, right) => left.coordinate_index - right.coordinate_index),
    }));
});
const thermodynamics = useQuery({
  queryKey: computed(() => ["catalog", "mapped-reaction-thermodynamics", { id: props.mappedReaction?.id, projectId: props.projectId }]),
  queryFn: ({ signal }) => api.mappedReactionThermodynamics(props.mappedReaction?.id ?? "", { projectId: props.projectId ?? undefined }, signal),
  enabled: computed(() => Boolean(props.projectId && props.mappedReaction?.id)),
  staleTime: 120_000,
});
const profiles = computed(() => thermodynamics.data.value?.profiles ?? []);

const HARTREE_TO_KCAL_MOL = 627.5094740631;

function finiteNumber(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value);
}

const thermodynamicPotentialProfile = computed<ReactionPotentialEnergyProfile | null>(() => {
  const profile = profiles.value.find((candidate) =>
    candidate.transition_state !== null
    && candidate.products !== null
    && finiteNumber(candidate.reactants.gibbs_free_energy_hartree)
    && finiteNumber(candidate.transition_state.gibbs_free_energy_hartree)
    && finiteNumber(candidate.products.gibbs_free_energy_hartree),
  );
  if (!profile || !profile.transition_state || !profile.products) return null;
  const referenceEnergy = profile.reactants.gibbs_free_energy_hartree;
  const relativeEnergy = (value: number): number =>
    (value - referenceEnergy) * HARTREE_TO_KCAL_MOL;
  return {
    energyKind: "gibbs_free_energy_hartree",
    levelOfTheory: profile.level_of_theory,
    temperatureKelvin: profile.temperature_kelvin,
    stages: [
      {
        id: `${profile.mapped_reaction_id}-reactants`,
        label: "前体",
        role: "reactant",
        energyHartree: profile.reactants.gibbs_free_energy_hartree,
        relativeEnergy: 0,
      },
      {
        id: `${profile.mapped_reaction_id}-transition-state`,
        label: "TS",
        role: "transition_state",
        energyHartree: profile.transition_state.gibbs_free_energy_hartree,
        relativeEnergy: relativeEnergy(profile.transition_state.gibbs_free_energy_hartree),
      },
      {
        id: `${profile.mapped_reaction_id}-products`,
        label: "后体",
        role: "product",
        energyHartree: profile.products.gibbs_free_energy_hartree,
        relativeEnergy: relativeEnergy(profile.products.gibbs_free_energy_hartree),
      },
    ],
    reactionEnergyKcalMol: profile.reaction?.gibbs_free_energy_kcal_mol ?? null,
    forwardBarrierKcalMol: profile.activation?.gibbs_free_energy_kcal_mol ?? null,
    reverseBarrierKcalMol: profile.activation && profile.reaction
      ? profile.activation.gibbs_free_energy_kcal_mol - profile.reaction.gibbs_free_energy_kcal_mol
      : null,
  };
});

const energyProfile = useQuery({
  queryKey: computed(() => ["catalog", "mapped-reaction-energy-profile", { id: props.mappedReaction?.id, projectId: props.projectId }]),
  queryFn: ({ signal }) => api.reactionEnergyProfile(
    props.mappedReaction?.id ?? "",
    { projectId: props.projectId ?? undefined, energyKind: "gibbs_free_energy_hartree" },
    signal,
  ),
  enabled: computed(() => Boolean(props.projectId && props.mappedReaction?.id)),
  staleTime: 120_000,
});
const energyPotentialProfile = computed<ReactionPotentialEnergyProfile | null>(() => {
  const profile = energyProfile.data.value;
  if (!profile) return null;
  const points = new Map(profile.points.map((point) => [point.node_id, point]));
  const edge = profile.edges.find((candidate) => {
    const transitionStateId = candidate.transition_state_node_id;
    if (!transitionStateId) return false;
    return [candidate.source_node_id, transitionStateId, candidate.target_node_id]
      .every((id) => {
        const point = points.get(id);
        return finiteNumber(point?.energy_hartree) && finiteNumber(point.relative_energy_kcal_mol);
      });
  });
  if (!edge || !edge.transition_state_node_id) return null;
  const source = points.get(edge.source_node_id);
  const transitionState = points.get(edge.transition_state_node_id);
  const target = points.get(edge.target_node_id);
  if (!source || !transitionState || !target) return null;
  return {
    energyKind: profile.energy_kind,
    levelOfTheory: null,
    temperatureKelvin: null,
    stages: [
      { id: source.node_id, label: "前体", role: source.role, energyHartree: source.energy_hartree!, relativeEnergy: source.relative_energy_kcal_mol! },
      { id: transitionState.node_id, label: "TS", role: transitionState.role, energyHartree: transitionState.energy_hartree!, relativeEnergy: transitionState.relative_energy_kcal_mol! },
      { id: target.node_id, label: "后体", role: target.role, energyHartree: target.energy_hartree!, relativeEnergy: target.relative_energy_kcal_mol! },
    ],
    reactionEnergyKcalMol: edge.reaction_energy_kcal_mol,
    forwardBarrierKcalMol: edge.forward_barrier_kcal_mol,
    reverseBarrierKcalMol: edge.reverse_barrier_kcal_mol,
  };
});
// Prefer materialized endpoint thermochemistry; fall back to complete node composites for older paths.
const potentialEnergyProfile = computed(() => thermodynamicPotentialProfile.value ?? energyPotentialProfile.value);

function mappedParticipantRole(logicalParticipantId: string, side: string): string {
  return logicalParticipantsById.value.get(logicalParticipantId)?.role ?? side;
}

watch(orderedNodes, (nodes) => { activeNodeId.value = nodes[0]?.id ?? null; }, { immediate: true });
</script>

<template>
  <section class="mapped-reaction-expansion" :aria-labelledby="`mapped-reaction-title-${reaction.id}`">
    <header class="workspace-header compact-workspace-header">
      <div>
        <span class="eyebrow">MappedReaction</span>
        <h2 :id="`mapped-reaction-title-${reaction.id}`">{{ reaction.label || reaction.reaction_key }} 的映射路径</h2>
        <code class="mapped-reaction-smiles">{{ mappedReaction?.mapped_reaction_smiles ?? "正在读取映射反应…" }}</code>
      </div>
      <dl class="header-facts"><div><dt>映射方案</dt><dd>{{ reaction.mapped_reactions.length }}</dd></div><div><dt>参与物</dt><dd>{{ reaction.participants.length }}</dd></div></dl>
    </header>

    <section v-if="profiles.length" class="reaction-thermodynamics" aria-label="反应热力学">
      <header class="thermo-section-header"><div><span class="eyebrow">Thermodynamics</span><h3>活化和反应能差</h3></div><span>{{ profiles.length }} 个计算 profile</span></header>
      <div class="thermo-profile-list">
        <article v-for="(profile, profileIndex) in profiles" :key="`${profile.level_of_theory}-${profile.temperature_kelvin}-${profile.pressure_atm}-${profileIndex}`" class="thermo-profile">
          <header class="thermo-profile-header"><div><span class="eyebrow">Profile {{ String(profileIndex + 1).padStart(2, "0") }}</span><code class="thermo-level">{{ profile.level_of_theory }}</code></div><span>{{ profile.temperature_kelvin }} K · {{ profile.pressure_atm }} atm</span></header>
          <div class="thermo-metrics">
            <template v-if="profile.activation">
              <div><span>ΔH‡</span><strong>{{ profile.activation.enthalpy_kcal_mol.toFixed(2) }}</strong><small>kcal/mol</small></div>
              <div><span>ΔG‡</span><strong>{{ profile.activation.gibbs_free_energy_kcal_mol.toFixed(2) }}</strong><small>kcal/mol</small></div>
              <div><span>ΔS‡</span><strong>{{ profile.activation.entropy_cal_mol_k.toFixed(2) }}</strong><small>cal/mol/K</small></div>
            </template>
            <template v-if="profile.reaction">
              <div><span>ΔH 反应</span><strong>{{ profile.reaction.enthalpy_kcal_mol.toFixed(2) }}</strong><small>kcal/mol</small></div>
              <div><span>ΔG 反应</span><strong>{{ profile.reaction.gibbs_free_energy_kcal_mol.toFixed(2) }}</strong><small>kcal/mol</small></div>
              <div><span>ΔS 反应</span><strong>{{ profile.reaction.entropy_cal_mol_k.toFixed(2) }}</strong><small>cal/mol/K</small></div>
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

    <ReactionPotentialEnergyDiagram v-if="potentialEnergyProfile" :profile="potentialEnergyProfile" />

    <section v-if="mappedReaction" class="reaction-equation compact-equation" aria-label="已映射的底物和产物">
      <div class="equation-side">
        <article v-for="participant in mappedReactants" :key="participant.id" class="molecule-item">
          <div class="molecule-caption"><span><strong>{{ labelFor(mappedParticipantRole(participant.logical_reaction_participant_id, participant.side)) }}</strong><small>{{ participant.mapped_smiles }}</small></span><RouterLink :to="{ name: 'topology-detail', params: { topologyId: participant.topology_id }, query: navigationQuery }" title="查看分子拓扑" :aria-label="`查看分子拓扑 ${participant.topology_id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></div>
          <ChemDoodleMolecule :topology-id="participant.topology_id" :atom-map-numbers="participant.atom_map_numbers" :height="170" :label="participant.mapped_smiles" />
        </article>
      </div>
      <div class="equation-arrow"><ArrowRight :size="24" aria-hidden="true" /></div>
      <div class="equation-side is-product">
        <article v-for="participant in mappedProducts" :key="participant.id" class="molecule-item">
          <div class="molecule-caption"><span><strong>{{ labelFor(mappedParticipantRole(participant.logical_reaction_participant_id, participant.side)) }}</strong><small>{{ participant.mapped_smiles }}</small></span><RouterLink :to="{ name: 'topology-detail', params: { topologyId: participant.topology_id }, query: navigationQuery }" title="查看分子拓扑" :aria-label="`查看分子拓扑 ${participant.topology_id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></div>
          <ChemDoodleMolecule :topology-id="participant.topology_id" :atom-map-numbers="participant.atom_map_numbers" :height="170" :label="participant.mapped_smiles" />
        </article>
      </div>
    </section>
    <div v-else class="loading-block is-wide mapped-equation-loading"></div>

    <section class="mapped-section">
      <div class="section-heading mapped-heading">
        <div><span class="eyebrow">MappedReaction</span><h3>节点和几何构象</h3></div>
        <label v-if="reaction.mapped_reactions.length" class="select-field"><span class="sr-only">选择映射反应</span><select :value="selectedMappedId ?? ''" @change="emit('selectMapped', ($event.target as HTMLSelectElement).value)"><option v-for="mapped in reaction.mapped_reactions" :key="mapped.id" :value="mapped.id">{{ mapped.label || mapped.mapped_reaction_key }}</option></select></label>
      </div>
      <div v-if="mappedLoading" class="loading-block is-wide"></div>
      <div v-else-if="!mappedReaction" class="compact-empty">该反应没有映射路径</div>
      <template v-else>
        <div class="mapped-meta"><span>{{ mappedReaction.mapped_reaction_kind }}</span><code>{{ mappedReaction.mapped_reaction_key }}</code><code :title="mappedReaction.mapping_hash">hash {{ shortId(mappedReaction.mapping_hash) }}</code></div>
        <nav class="node-rail" aria-label="反应节点">
          <template v-for="(node, index) in orderedNodes" :key="node.id">
            <button type="button" class="node-step" :class="{ 'is-active': selectedNode?.id === node.id }" @click="activeNodeId = node.id"><span>{{ String(index + 1).padStart(2, "0") }}</span><strong>{{ labelFor(node.role) }}</strong><small>{{ node.geometries.length }} 个构型</small></button>
            <ChevronRight v-if="index < orderedNodes.length - 1" :size="18" aria-hidden="true" />
          </template>
        </nav>
        <section v-if="selectedNode" class="node-detail">
          <header class="node-detail-header"><div><span class="eyebrow">Node / {{ selectedNode.node_key }}</span><h3>{{ labelFor(selectedNode.role) }}</h3></div><dl v-if="selectedNode.additive_properties" class="node-energy-summary"><div><dt>电子能</dt><dd>{{ formatEnergy(selectedNode.additive_properties.electronic_energy_hartree) }}</dd></div><div><dt>Gibbs</dt><dd>{{ formatEnergy(selectedNode.additive_properties.gibbs_free_energy_hartree) }}</dd></div></dl></header>
          <div class="geometry-component-list">
            <section v-for="row in selectedGeometryRows" :key="row.key" class="geometry-component-row">
              <header class="geometry-component-row-header"><div><span class="eyebrow">Component {{ row.componentIndex + 1 }}</span><h4>{{ row.participantRole ? labelFor(row.participantRole) : row.componentKey }}</h4></div><code>{{ row.componentKey }}</code></header>
              <div class="geometry-grid"><article v-for="(geometry, geometryIndex) in row.geometries" :key="geometry.id" class="geometry-item"><header><div><strong>{{ geometry.participant_role ? labelFor(geometry.participant_role) : geometry.component_key }}</strong><span>坐标 {{ geometryIndex + 1 }}</span></div><span v-if="geometry.is_primary" class="primary-tag">主构型</span><RouterLink class="geometry-direct-link" :to="{ name: 'geometry-detail', params: { geometryId: geometry.geometry_id }, query: navigationQuery }" title="查看几何构象" :aria-label="`查看几何构象 ${geometry.geometry_id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></header><ChemDoodleGeometry3D :geometry-id="geometry.geometry_id" :project-id="projectId ?? undefined" :label="geometry.canonical_isomeric_smiles ?? undefined" :height="220" /><code class="smiles-line">{{ geometry.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code><dl class="property-grid"><div><dt>电子能 / Eh</dt><dd>{{ formatEnergy(geometry.energy_view.electronic_energy_hartree) }}</dd></div><div><dt>Gibbs / Eh</dt><dd>{{ formatEnergy(geometry.energy_view.gibbs_free_energy_hartree) }}</dd></div><div><dt>熵 / cal mol⁻¹ K⁻¹</dt><dd>{{ formatNumber(geometry.energy_view.entropy_cal_mol_k, 3) }}</dd></div></dl><div class="frame-links"><div v-for="calculation in geometry.calculations" :key="calculation.id" class="frame-link-item"><button type="button" @click="emit('openFrame', calculation.id)"><span>{{ labelFor(calculation.frame_role) }}</span><code>{{ formatEnergy(calculation.selected_energy_hartree) }}</code><code class="frame-link-runtime" :title="`逐帧计算用时：${formatDurationSeconds(calculation.running_time_seconds)}`">{{ formatDurationSeconds(calculation.running_time_seconds) }}</code><ChevronRight :size="15" aria-hidden="true" /></button><RouterLink :to="{ name: 'calculation-detail', params: { frameId: calculation.id }, query: navigationQuery }" title="在独立页面打开" :aria-label="`在独立页面打开计算帧 ${calculation.id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></div></div></article></div>
            </section>
          </div>
        </section>
      </template>
    </section>

  </section>
</template>
