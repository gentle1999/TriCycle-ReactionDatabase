<script setup lang="ts">
import { Download, RefreshCw } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import type { EChartsOption } from "echarts";
import { BarChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import { use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { computed, ref } from "vue";
import VChart from "vue-echarts";
import { useRoute, useRouter } from "vue-router";

import { api } from "@/api";
import { useProjectContext } from "@/composables/useProjectContext";
import { shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { ThermodynamicDistributionBin, ThermodynamicScatterPoint } from "@/types";

use([
  BarChart,
  ScatterChart,
  CanvasRenderer,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
]);

const projectContext = useProjectContext();
const route = useRoute();
const router = useRouter();
const currentProjectId = projectContext.currentProjectId;
const currentProject = projectContext.currentProject;
const navigationQuery = computed(() => withoutAccessState(route.query));
const downloading = ref(false);
const downloadError = ref("");

interface ScatterDatum {
  value: [number, number];
  mappedReactionId: string;
  mappedReactionSmiles: string;
  detailHref: string;
}

interface ScatterEvent {
  data?: unknown;
}

const statistics = useQuery({
  queryKey: computed(() => ["thermodynamic-statistics", { projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.mappedReactionThermodynamicStatistics(
    { projectId: currentProjectId.value ?? undefined },
    signal,
  ),
  enabled: computed(() => currentProjectId.value !== null),
  staleTime: 60_000,
});

const errorMessage = computed(() => {
  if (downloadError.value) return downloadError.value;
  return statistics.error.value instanceof Error ? statistics.error.value.message : "";
});

function histogramOption(
  title: string,
  bins: ThermodynamicDistributionBin[],
  color: string,
): EChartsOption {
  return {
    animation: false,
    grid: { top: 16, right: 18, bottom: 44, left: 48 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    xAxis: {
      type: "category",
      name: "kcal/mol",
      nameLocation: "middle",
      nameGap: 30,
      data: bins.map((bin) => `${bin.lower.toFixed(1)}–${bin.upper.toFixed(1)}`),
      axisLabel: { rotate: bins.length > 8 ? 36 : 0, fontSize: 10 },
    },
    yAxis: { type: "value", name: "profile 数", minInterval: 1 },
    series: [{
      name: title,
      type: "bar",
      barMaxWidth: 28,
      data: bins.map((bin) => bin.count),
      itemStyle: { color },
    }],
  };
}

const activationOption = computed(() => histogramOption(
  "活化自由能 ΔG‡",
  statistics.data.value?.activation_gibbs_free_energy_kcal_mol ?? [],
  "#a63e35",
));

const reactionOption = computed(() => histogramOption(
  "反应自由能 ΔG",
  statistics.data.value?.reaction_gibbs_free_energy_kcal_mol ?? [],
  "#15736c",
));

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character] ?? character);
}

function scatterDatum(point: ThermodynamicScatterPoint): ScatterDatum {
  return {
    value: [
      point.activation_gibbs_free_energy_kcal_mol,
      point.reaction_gibbs_free_energy_kcal_mol,
    ],
    mappedReactionId: point.mapped_reaction_id,
    mappedReactionSmiles: point.mapped_reaction_smiles,
    detailHref: router.resolve({
      name: "mapped-reaction-detail",
      params: { mappedReactionId: point.mapped_reaction_id },
      query: navigationQuery.value,
    }).href,
  };
}

function isScatterDatum(value: unknown): value is ScatterDatum {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ScatterDatum>;
  return Array.isArray(candidate.value)
    && candidate.value.length === 2
    && typeof candidate.mappedReactionId === "string"
    && typeof candidate.mappedReactionSmiles === "string"
    && typeof candidate.detailHref === "string";
}

function scatterTooltip(params: unknown): string {
  const candidate = Array.isArray(params) ? params[0] : params;
  if (!candidate || typeof candidate !== "object") return "";
  const data = (candidate as { data?: unknown }).data;
  if (!isScatterDatum(data)) return "";
  const [activation, reaction] = data.value;
  return [
    '<div class="thermo-point-tooltip">',
    '<span class="thermo-point-tooltip-label">Mapped reaction</span>',
    `<code>${escapeHtml(data.mappedReactionSmiles)}</code>`,
    '<dl>',
    `<div><dt>ΔG‡</dt><dd>${activation.toFixed(2)} kcal/mol</dd></div>`,
    `<div><dt>ΔG</dt><dd>${reaction.toFixed(2)} kcal/mol</dd></div>`,
    `<div><dt>ID</dt><dd>${escapeHtml(shortId(data.mappedReactionId))}</dd></div>`,
    '</dl>',
    `<a href="${escapeHtml(data.detailHref)}">映射反应详情 ↗</a>`,
    '</div>',
  ].join("");
}

function openScatterPoint(params: ScatterEvent): void {
  if (!isScatterDatum(params.data)) return;
  void router.push({
    name: "mapped-reaction-detail",
    params: { mappedReactionId: params.data.mappedReactionId },
    query: navigationQuery.value,
  });
}

const scatterOption = computed<EChartsOption>(() => {
  const points = statistics.data.value?.scatter ?? [];
  return {
    animation: false,
    grid: { top: 16, right: 18, bottom: 48, left: 56 },
    tooltip: {
      trigger: "item",
      renderMode: "html",
      enterable: true,
      confine: true,
      hideDelay: 240,
      className: "thermo-scatter-tooltip",
      formatter: scatterTooltip,
    },
    xAxis: { type: "value", name: "ΔG‡ / kcal/mol", nameLocation: "middle", nameGap: 30 },
    yAxis: { type: "value", name: "ΔG / kcal/mol", nameLocation: "middle", nameGap: 42 },
    series: [{
      name: "thermodynamic profile",
      type: "scatter",
      symbolSize: 9,
      cursor: "pointer",
      data: points.map(scatterDatum),
      itemStyle: { color: "#32658a", opacity: 0.72 },
      emphasis: {
        scale: 1.7,
        itemStyle: { color: "#a63e35", opacity: 1, borderColor: "#ffffff", borderWidth: 1 },
      },
    }],
  };
});

const levelOption = computed<EChartsOption>(() => {
  const levels = statistics.data.value?.level_of_theory ?? [];
  return {
    animation: false,
    grid: { top: 16, right: 18, bottom: 20, left: 150 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    xAxis: { type: "value", minInterval: 1 },
    yAxis: { type: "category", inverse: true, data: levels.map((item) => item.label), axisLabel: { fontSize: 10 } },
    series: [{
      type: "bar",
      data: levels.map((item) => item.count),
      barMaxWidth: 22,
      itemStyle: { color: "#946a19" },
    }],
  };
});

const temperatureOption = computed<EChartsOption>(() => {
  const temperatures = statistics.data.value?.temperature_kelvin ?? [];
  return {
    animation: false,
    grid: { top: 16, right: 18, bottom: 36, left: 48 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    xAxis: { type: "category", data: temperatures.map((item) => item.label), axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", minInterval: 1 },
    series: [{
      type: "bar",
      data: temperatures.map((item) => item.count),
      barMaxWidth: 32,
      itemStyle: { color: "#32658a" },
    }],
  };
});

function formatCoverage(value: number, denominator: number): string {
  if (!denominator) return "暂无数据";
  return `${value} / ${denominator}（${((value / denominator) * 100).toFixed(1)}%）`;
}

async function downloadExport(): Promise<void> {
  if (!currentProjectId.value || downloading.value) return;
  downloading.value = true;
  downloadError.value = "";
  try {
    const blob = await api.mappedReactionThermodynamicExport({ projectId: currentProjectId.value });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `mapped-reaction-thermodynamics-${currentProject.value?.project_slug ?? "project"}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    downloadError.value = error instanceof Error ? error.message : "导出失败";
  } finally {
    downloading.value = false;
  }
}
</script>

<template>
  <main class="analytics-page">
    <section class="analytics-heading">
      <div>
        <span class="eyebrow">REACTION PATH ANALYTICS</span>
        <h1>反应路径分布统计</h1>
        <p>{{ currentProject?.project_name ?? "当前项目" }} · 物化热力学 profile 的可见数据汇总</p>
      </div>
      <button class="command-button" type="button" :disabled="downloading || !currentProjectId" @click="downloadExport">
        <Download :size="15" aria-hidden="true" />
        <span>{{ downloading ? "正在导出" : "导出热力学 CSV" }}</span>
      </button>
    </section>

    <div v-if="errorMessage" class="notice analytics-notice" role="alert">{{ errorMessage }}</div>
    <div v-if="statistics.isLoading.value" class="analytics-state">
      <RefreshCw :size="18" class="is-spinning" aria-hidden="true" />
      <span>正在计算统计...</span>
    </div>
    <template v-else-if="statistics.data.value">
      <section class="analytics-kpis" aria-label="热力学数据覆盖概览">
        <div><span>映射反应</span><strong>{{ statistics.data.value.mapped_reaction_count }}</strong></div>
        <div><span>热力学 profile</span><strong>{{ statistics.data.value.profile_count }}</strong></div>
        <div><span>含 ΔG‡</span><strong>{{ formatCoverage(statistics.data.value.activation_profile_count, statistics.data.value.profile_count) }}</strong></div>
        <div><span>含 ΔG</span><strong>{{ formatCoverage(statistics.data.value.reaction_profile_count, statistics.data.value.profile_count) }}</strong></div>
        <div><span>ΔG‡ + ΔG</span><strong>{{ formatCoverage(statistics.data.value.complete_profile_count, statistics.data.value.profile_count) }}</strong></div>
      </section>

      <section class="analytics-grid" aria-label="热力学分布图表">
        <article class="analytics-panel">
          <div class="analytics-panel-heading"><div><span class="eyebrow">KINETIC</span><h2>活化自由能分布</h2></div><span>ΔG‡ / kcal/mol</span></div>
          <VChart class="analytics-chart" :option="activationOption" autoresize />
        </article>
        <article class="analytics-panel">
          <div class="analytics-panel-heading"><div><span class="eyebrow">THERMODYNAMIC</span><h2>反应自由能分布</h2></div><span>ΔG / kcal/mol</span></div>
          <VChart class="analytics-chart" :option="reactionOption" autoresize />
        </article>
        <article class="analytics-panel analytics-panel-wide">
          <div class="analytics-panel-heading"><div><span class="eyebrow">RELATIONSHIP</span><h2>动力学与热力学关系</h2></div><span>最多显示 1000 个 profile</span></div>
          <VChart class="analytics-chart analytics-chart-tall analytics-scatter-chart" :option="scatterOption" autoresize @click="openScatterPoint" />
        </article>
        <article class="analytics-panel">
          <div class="analytics-panel-heading"><div><span class="eyebrow">METHOD</span><h2>计算层级构成</h2></div><span>profile 数</span></div>
          <VChart class="analytics-chart analytics-chart-level" :option="levelOption" autoresize />
        </article>
        <article class="analytics-panel">
          <div class="analytics-panel-heading"><div><span class="eyebrow">CONDITION</span><h2>温度条件</h2></div><span>profile 数</span></div>
          <VChart class="analytics-chart" :option="temperatureOption" autoresize />
        </article>
      </section>
    </template>
    <div v-else class="analytics-state">当前项目暂无可统计的热力学 profile。</div>
  </main>
</template>
