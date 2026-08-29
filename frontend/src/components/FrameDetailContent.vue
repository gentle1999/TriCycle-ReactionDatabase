<script setup lang="ts">
import { ChevronDown, Download, FileText, Network, Shapes } from "@lucide/vue";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, apiUrl } from "@/api";
import { formatBytes, formatEnergy, formatNumber, labelFor, shortId, statusTone } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { CalculationFrameDetail, ScientificArrayPreview } from "@/types";

import ChemDoodleGeometry3D from "./ChemDoodleGeometry3D.vue";
import ChemDoodleTransitionStateMode3D from "./ChemDoodleTransitionStateMode3D.vue";
import TransitionStateModeDofPreview from "./TransitionStateModeDofPreview.vue";

const props = defineProps<{
  frame: CalculationFrameDetail;
  projectId?: string;
}>();

const route = useRoute();

const navigationQuery = computed(() => withoutAccessState(route.query));
const protocolEntries = computed(() => displayEntries(props.frame.protocol));
const energyEntries = computed(() => displayEntries(props.frame.energy));
const thermochemistryEntries = computed(() => displayEntries(props.frame.thermochemistry));

type OptimizationMetric = {
  label: string;
  unit: string;
  actual: number | null;
  reference: number | null;
  converged: boolean | null;
};

const optimizationMetrics = computed<OptimizationMetric[]>(() => {
  const optimization = props.frame.optimization;
  if (!optimization) return [];
  return [
    {
      label: "能量变化 ΔE",
      unit: "Eh",
      actual: optimization.energy_change_hartree,
      reference: optimization.energy_change_threshold_hartree,
      converged: optimization.energy_change_converged,
    },
    {
      label: "RMS 力",
      unit: "Eh Bohr⁻¹",
      actual: optimization.rms_force_hartree_per_bohr,
      reference: optimization.rms_force_threshold_hartree_per_bohr,
      converged: optimization.rms_force_converged,
    },
    {
      label: "最大力",
      unit: "Eh Bohr⁻¹",
      actual: optimization.max_force_hartree_per_bohr,
      reference: optimization.max_force_threshold_hartree_per_bohr,
      converged: optimization.max_force_converged,
    },
    {
      label: "RMS 位移",
      unit: "Bohr",
      actual: optimization.rms_displacement_bohr,
      reference: optimization.rms_displacement_threshold_bohr,
      converged: optimization.rms_displacement_converged,
    },
    {
      label: "最大位移",
      unit: "Bohr",
      actual: optimization.max_displacement_bohr,
      reference: optimization.max_displacement_threshold_bohr,
      converged: optimization.max_displacement_converged,
    },
  ];
});

type ArrayPreviewState = {
  loading: boolean;
  data: ScientificArrayPreview | null;
  error: string;
};

const expandedArrayId = ref<string | null>(null);
const arrayPreviewStates = ref<Record<string, ArrayPreviewState>>({});
let previewController: AbortController | null = null;

function arrayPreviewState(id: string): ArrayPreviewState | null {
  return arrayPreviewStates.value[id] ?? null;
}

function arrayPreviewData(id: string): ScientificArrayPreview | null {
  return arrayPreviewState(id)?.data ?? null;
}

function arrayDownloadUrl(id: string): string {
  return apiUrl(`/api/scientific-arrays/${encodeURIComponent(id)}.npy`);
}

function arrayDisplayLabel(array: CalculationFrameDetail["scientific_arrays"][number]): string {
  if (array.kind === "atomic_population") {
    const populationName =
      array.population_name ??
      array.source_field?.match(/populations\.([^\.]+)\.values$/)?.[1];
    if (typeof populationName === "string" && populationName.length > 0) {
      return populationName;
    }
  }
  return `${array.kind} #${array.ordinal}`;
}

function arrayDisplaySource(array: CalculationFrameDetail["scientific_arrays"][number]): string {
  if (array.kind === "atomic_population" && array.population_source_label) {
    return array.population_source_label;
  }
  return array.source_field ?? "MolOP";
}

function arrayDisplayDetails(array: CalculationFrameDetail["scientific_arrays"][number]): string {
  if (array.kind === "atomic_population") {
    const details = [array.population_scheme, array.population_quantity, array.unit].filter(
      (value): value is string => Boolean(value),
    );
    return details.length ? details.join(" · ") : array.unit;
  }
  return [array.kind, array.dtype, array.shape.join(" × "), array.unit].join(" · ");
}

function formatArrayValue(value: unknown): string {
  if (value && typeof value === "object" && "real" in value && "imag" in value) {
    const complex = value as { real: unknown; imag: unknown };
    return `${String(complex.real)} ${Number(complex.imag) >= 0 ? "+" : "-"} ${Math.abs(Number(complex.imag))}i`;
  }
  return typeof value === "string" ? value : String(value);
}

function formatArrayPreview(preview: ScientificArrayPreview | null): string {
  if (!preview) return "";
  const values = preview.values.map(formatArrayValue);
  const columns = Math.max(1, Math.min(preview.shape.at(-1) ?? values.length, 16));
  const rows: string[] = [];
  for (let index = 0; index < values.length; index += columns) {
    rows.push(values.slice(index, index + columns).join("  "));
  }
  return rows.join("\n");
}

async function toggleArray(arrayId: string): Promise<void> {
  if (expandedArrayId.value === arrayId) {
    expandedArrayId.value = null;
    previewController?.abort();
    return;
  }
  expandedArrayId.value = arrayId;
  const existing = arrayPreviewState(arrayId);
  if (existing?.data || existing?.loading) return;

  previewController?.abort();
  const controller = new AbortController();
  previewController = controller;
  arrayPreviewStates.value[arrayId] = { loading: true, data: null, error: "" };
  try {
    const data = await api.scientificArrayPreview(arrayId, { maxElements: 512 }, controller.signal);
    if (!controller.signal.aborted) {
      arrayPreviewStates.value[arrayId] = { loading: false, data, error: "" };
    }
  } catch (caught) {
    if (controller.signal.aborted) return;
    arrayPreviewStates.value[arrayId] = {
      loading: false,
      data: null,
      error: caught instanceof Error ? caught.message : "科学数组读取失败",
    };
  }
}

function displayEntries(source: Record<string, unknown> | null): Array<[string, string]> {
  if (!source) return [];
  return Object.entries(source)
    .filter(([, value]) => value !== null && value !== "" && value !== undefined)
    .map(([key, value]) => {
      const displayValue =
        typeof value === "number" && (key.endsWith("_hartree") || key === "value_hartree")
          ? formatEnergy(value)
          : Array.isArray(value)
            ? value.join(", ")
            : String(value);
      return [key.replaceAll("_", " "), displayValue];
    });
}

function formatOptimizationValue(value: number | null): string {
  if (value === null) return "—";
  return Math.abs(value) > 0 && Math.abs(value) < 0.0001
    ? value.toExponential(6)
    : formatNumber(value, 6);
}

function convergenceLabel(value: boolean | null): string {
  if (value === true) return "已满足";
  if (value === false) return "未满足";
  return "未判定";
}

function convergenceTone(value: boolean | null): string {
  if (value === true) return "ok";
  if (value === false) return "bad";
  return "neutral";
}

watch(
  () => props.frame.id,
  () => {
    expandedArrayId.value = null;
    arrayPreviewStates.value = {};
    previewController?.abort();
  },
);

onBeforeUnmount(() => previewController?.abort());
</script>

<template>
  <div class="frame-detail-content">
    <section class="drawer-identity">
      <strong>{{ frame.original_filename }}</strong>
      <span>segment {{ frame.segment_index + 1 }} · file frame {{ frame.file_frame_index + 1 }}</span>
      <code :title="frame.id">{{ frame.id }}</code>
    </section>

    <nav class="frame-resource-links" aria-label="计算帧关联资源">
      <RouterLink :to="{ name: 'artifact-detail', params: { artifactId: frame.artifact_file_id }, query: navigationQuery }" :title="`查看原始文件 ${frame.original_filename}`">
        <FileText :size="15" aria-hidden="true" />
        <span>查看原始文件</span>
      </RouterLink>
      <RouterLink :to="{ name: 'topology-detail', params: { topologyId: frame.topology_id }, query: navigationQuery }" title="查看分子拓扑" aria-label="查看分子拓扑">
        <Network :size="15" aria-hidden="true" />
        <span>分子拓扑</span>
      </RouterLink>
      <RouterLink :to="{ name: 'geometry-detail', params: { geometryId: frame.geometry_id }, query: navigationQuery }" title="查看几何构象" aria-label="查看几何构象">
        <Shapes :size="15" aria-hidden="true" />
        <span>几何构象</span>
      </RouterLink>
    </nav>

    <ChemDoodleGeometry3D
      :geometry-id="frame.geometry_id"
      :project-id="projectId"
      :label="frame.canonical_isomeric_smiles ?? undefined"
      :height="280"
    />
    <section v-if="frame.transition_state_endpoints.length === 2" class="transition-state-mode-views" aria-label="虚频模式插值视图">
      <ChemDoodleTransitionStateMode3D
        :frame-id="frame.id"
        :project-id="projectId"
        :negative-displacement-ratio="frame.transition_state_endpoints.find((item) => item.direction === 'negative')?.displacement_ratio"
        :positive-displacement-ratio="frame.transition_state_endpoints.find((item) => item.direction === 'positive')?.displacement_ratio"
        :height="340"
      />
      <TransitionStateModeDofPreview :frame-id="frame.id" :project-id="projectId" :height="340" />
    </section>
    <code class="drawer-smiles">{{ frame.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code>

    <dl class="drawer-metrics">
      <div><dt>角色</dt><dd>{{ labelFor(frame.frame_role) }}</dd></div>
      <div><dt>电荷 / 多重度</dt><dd>{{ frame.charge }} / {{ frame.multiplicity }}</dd></div>
      <div><dt>能量 / Eh</dt><dd>{{ formatEnergy(frame.selected_energy_hartree) }}</dd></div>
      <div><dt>负频数</dt><dd>{{ frame.negative_frequency_count ?? "—" }}</dd></div>
    </dl>

    <section class="drawer-section">
      <h3>计算状态</h3>
      <div class="status-row">
        <span class="status-dot" :class="statusTone(frame.scf_status)">SCF {{ frame.scf_status }}</span>
        <span class="status-dot" :class="statusTone(frame.optimization_status)">优化 {{ frame.optimization_status }}</span>
        <span class="role-pill">{{ frame.parse_completeness }}</span>
      </div>
    </section>

    <section v-if="frame.optimization" class="drawer-section">
      <header class="optimization-convergence-header">
        <h3>优化收敛性</h3>
        <span>
          几何优化：{{ frame.optimization.geometry_optimized === null ? "未判定" : frame.optimization.geometry_optimized ? "是" : "否" }}
          · 判定倍率 ×{{ formatNumber(frame.optimization.convergence_multiplier, 2) }}
        </span>
      </header>
      <div class="optimization-convergence-table" role="table" aria-label="优化收敛性指标">
        <div class="optimization-convergence-row is-header" role="row">
          <span role="columnheader">指标</span>
          <span role="columnheader">实际值</span>
          <span role="columnheader">参考值</span>
          <span role="columnheader">判定</span>
        </div>
        <div v-for="metric in optimizationMetrics" :key="metric.label" class="optimization-convergence-row" role="row">
          <span role="cell"><strong>{{ metric.label }}</strong><small>{{ metric.unit }}</small></span>
          <code role="cell">{{ formatOptimizationValue(metric.actual) }}</code>
          <code role="cell">{{ formatOptimizationValue(metric.reference) }}</code>
          <span role="cell"><span class="status-dot" :class="convergenceTone(metric.converged)">{{ convergenceLabel(metric.converged) }}</span></span>
        </div>
      </div>
    </section>

    <section v-if="protocolEntries.length" class="drawer-section">
      <h3>计算协议</h3>
      <dl class="detail-list"><div v-for="([key, value]) in protocolEntries" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div></dl>
    </section>
    <section v-if="energyEntries.length" class="drawer-section">
      <h3>能量分量</h3>
      <dl class="detail-list"><div v-for="([key, value]) in energyEntries" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div></dl>
    </section>
    <section v-if="thermochemistryEntries.length" class="drawer-section">
      <h3>热化学</h3>
      <dl class="detail-list"><div v-for="([key, value]) in thermochemistryEntries" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></div></dl>
    </section>

    <section v-if="frame.vibration" class="drawer-section">
      <h3>振动分析</h3>
      <dl class="detail-list">
        <div><dt>mode count</dt><dd>{{ frame.vibration.mode_count }}</dd></div>
        <div><dt>imaginary modes</dt><dd>{{ frame.vibration.imaginary_mode_count }}</dd></div>
        <div><dt>lowest frequency / cm-1</dt><dd>{{ frame.vibration.lowest_frequency_cm1 ?? "—" }}</dd></div>
      </dl>
    </section>

    <section class="drawer-section">
      <h3>来源与拓扑重建</h3>
      <dl class="detail-list">
        <div><dt>source lines</dt><dd>{{ frame.source_span ? `${frame.source_span.start_line ?? "?"}-${frame.source_span.end_line ?? "?"}` : "未记录" }}</dd></div>
        <div><dt>source hash</dt><dd :title="frame.source_span?.block_sha256 ?? undefined">{{ frame.source_span?.block_sha256 ? shortId(frame.source_span.block_sha256) : "未记录" }}</dd></div>
        <div><dt>method</dt><dd>{{ frame.topology_derivation.reconstruction_method }}</dd></div>
        <div><dt>version</dt><dd>{{ frame.topology_derivation.reconstruction_version }}</dd></div>
        <div><dt>provenance</dt><dd :title="frame.topology_derivation.provenance_hash">{{ shortId(frame.topology_derivation.provenance_hash) }}</dd></div>
      </dl>
    </section>

    <section v-if="frame.scientific_arrays.length" class="drawer-section">
      <h3>科学数组</h3>
      <div class="array-list">
        <article v-for="array in frame.scientific_arrays" :key="array.id" class="array-list-item">
          <div class="array-list-row">
            <button type="button" class="array-toggle" :aria-expanded="expandedArrayId === array.id" @click="void toggleArray(array.id)">
              <span><strong>{{ arrayDisplayLabel(array) }}</strong><small>{{ arrayDisplaySource(array) }} · {{ arrayDisplayDetails(array) }} · {{ formatBytes(array.array_nbytes) }}</small></span>
              <ChevronDown :size="16" :class="{ 'is-rotated': expandedArrayId === array.id }" aria-hidden="true" />
            </button>
            <a class="array-download" :href="arrayDownloadUrl(array.id)" download :title="`下载 ${array.kind} NPY`" :aria-label="`下载 ${array.kind} NPY`" @click.stop><Download :size="15" aria-hidden="true" /></a>
          </div>
          <div v-if="expandedArrayId === array.id" class="array-preview">
            <div v-if="arrayPreviewState(array.id)?.loading" class="array-preview-state">正在读取数组预览</div>
            <div v-else-if="arrayPreviewState(array.id)?.error" class="array-preview-state is-error">{{ arrayPreviewState(array.id)?.error }}</div>
            <template v-else-if="arrayPreviewData(array.id)">
              <pre class="array-preview-values"><code>{{ formatArrayPreview(arrayPreviewData(array.id)) }}</code></pre>
              <p v-if="arrayPreviewData(array.id)?.truncated" class="array-preview-note">当前显示前 {{ arrayPreviewData(array.id)?.values.length }} / {{ arrayPreviewData(array.id)?.total_elements }} 个元素，下载 NPY 可查看完整数组。</p>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
