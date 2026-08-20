<script setup lang="ts">
import type { EChartsOption } from "echarts";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { computed } from "vue";
import VChart from "vue-echarts";

import { formatNumber } from "@/format";

use([LineChart, CanvasRenderer, GridComponent, TooltipComponent]);

export interface ReactionPotentialEnergyStageInput {
  id: string;
  label: string;
  role: string;
  energyHartree: number;
  relativeEnergy: number;
}

export interface ReactionPotentialEnergyProfile {
  energyKind: string;
  levelOfTheory: string | null;
  temperatureKelvin: number | null;
  stages: ReactionPotentialEnergyStageInput[];
  reactionEnergyKcalMol: number | null;
  forwardBarrierKcalMol: number | null;
  reverseBarrierKcalMol: number | null;
}

interface EnergyDatum {
  value: number;
  stageId: string;
  stageLabel: string;
  energyHartree: number;
}

const props = defineProps<{
  profile: ReactionPotentialEnergyProfile;
}>();

const activeEdgeMetrics = computed(() => ({
  reaction: props.profile.reactionEnergyKcalMol,
  forwardBarrier: props.profile.forwardBarrierKcalMol,
  reverseBarrier: props.profile.reverseBarrierKcalMol,
}));

function signed(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function isEnergyDatum(value: unknown): value is EnergyDatum {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<EnergyDatum>;
  return typeof candidate.value === "number"
    && typeof candidate.stageId === "string"
    && typeof candidate.stageLabel === "string"
    && typeof candidate.energyHartree === "number";
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character] ?? character);
}

function datumFromParams(params: unknown): EnergyDatum | null {
  const candidate = Array.isArray(params) ? params[0] : params;
  if (!candidate || typeof candidate !== "object") return null;
  const data = (candidate as { data?: unknown }).data;
  return isEnergyDatum(data) ? data : null;
}

function tooltipFormatter(params: unknown): string {
  const datum = datumFromParams(params);
  if (!datum) return "";
  return [
    '<div class="potential-energy-tooltip">',
    `<strong>${escapeHtml(datum.stageLabel)}</strong>`,
    `<span>ΔG ${signed(datum.value)} kcal/mol</span>`,
    `<span>G ${formatNumber(datum.energyHartree, 6)} Eh</span>`,
    "</div>",
  ].join("");
}

function labelFormatter(params: unknown): string {
  const datum = datumFromParams(params);
  if (!datum) return "";
  return `{relative|${signed(datum.value)} kcal/mol}\n{absolute|G ${formatNumber(datum.energyHartree, 4)} Eh}`;
}

const energyData = computed<EnergyDatum[]>(() => props.profile.stages.map((stage) => ({
  value: stage.relativeEnergy,
  stageId: stage.id,
  stageLabel: stage.label,
  energyHartree: stage.energyHartree,
})));

const energyScale = computed(() => {
  const values = energyData.value.map((datum) => datum.value);
  const minimumValue = Math.min(0, ...values);
  const maximumValue = Math.max(0, ...values);
  const span = Math.max(maximumValue - minimumValue, 8);
  const padding = Math.max(span * 0.2, 4);
  return {
    minimum: Math.floor((minimumValue - padding) / 5) * 5,
    maximum: Math.ceil((maximumValue + padding) / 5) * 5,
  };
});

const chartOption = computed<EChartsOption>(() => ({
  animation: false,
  aria: {
    enabled: true,
    label: {
      description: `Gibbs 自由能势能面：${props.profile.stages.map((stage) => `${stage.label} ${signed(stage.relativeEnergy)} kcal/mol`).join("，")}`,
    },
  },
  grid: { top: 92, right: 34, bottom: 44, left: 68, containLabel: true },
  tooltip: {
    trigger: "item",
    confine: true,
    renderMode: "html",
    className: "potential-energy-tooltip-shell",
    formatter: tooltipFormatter,
  },
  xAxis: {
    type: "category",
    boundaryGap: true,
    data: props.profile.stages.map((stage) => stage.label),
    axisLine: { lineStyle: { color: "#aeb8b1" } },
    axisTick: { show: false },
    axisLabel: { color: "#18201c", fontSize: 12, fontWeight: 700, margin: 14 },
  },
  yAxis: {
    type: "value",
    name: "ΔG / kcal/mol",
    nameLocation: "middle",
    nameGap: 48,
    min: energyScale.value.minimum,
    max: energyScale.value.maximum,
    splitNumber: 5,
    nameTextStyle: { color: "#5b6861", fontSize: 11 },
    axisLabel: {
      color: "#5b6861",
      fontSize: 10,
      formatter: (value: number) => signed(value),
    },
    axisLine: { show: true, lineStyle: { color: "#aeb8b1" } },
    splitLine: { lineStyle: { color: "#d3d9d4", type: "dashed" } },
  },
  series: [{
    name: "Gibbs 自由能",
    type: "line",
    data: energyData.value,
    smooth: 0.46,
    smoothMonotone: "x",
    symbol: "circle",
    symbolSize: 11,
    showSymbol: true,
    lineStyle: { color: "#a63e35", width: 3 },
    itemStyle: { color: "#ffffff", borderColor: "#a63e35", borderWidth: 3 },
    emphasis: {
      scale: 1.25,
      itemStyle: { color: "#f3ead5", borderColor: "#946a19", borderWidth: 3 },
    },
    label: {
      show: true,
      position: "top",
      distance: 14,
      formatter: labelFormatter,
      backgroundColor: "rgba(255, 255, 255, 0.96)",
      borderColor: "#d3d9d4",
      borderWidth: 1,
      borderRadius: 3,
      padding: [6, 8],
      rich: {
        relative: { color: "#a63e35", fontSize: 11, fontWeight: 700, lineHeight: 16 },
        absolute: { color: "#5b6861", fontSize: 10, fontFamily: "monospace", lineHeight: 15 },
      },
    },
    labelLayout: { hideOverlap: true, moveOverlap: "shiftY" },
  }],
}));
</script>

<template>
  <section v-if="profile.stages.length === 3" class="reaction-potential-energy" aria-labelledby="reaction-potential-energy-title">
    <header class="reaction-potential-energy-header">
      <div>
        <span class="eyebrow">Potential energy surface</span>
        <h3 id="reaction-potential-energy-title">Gibbs 自由能势能面</h3>
      </div>
      <span class="reaction-potential-energy-context">
        {{ profile.levelOfTheory || "Gibbs free energy" }}<template v-if="profile.temperatureKelvin !== null"> · {{ profile.temperatureKelvin }} K</template> · 相对前体
      </span>
    </header>
    <div class="reaction-potential-energy-chart" data-renderer="echarts-potential-energy">
      <VChart class="reaction-potential-energy-echart" :option="chartOption" autoresize />
    </div>
    <div class="sr-only potential-stage-values" aria-label="势能面节点能量">
      <div v-for="stage in profile.stages" :key="stage.id">
        <span class="potential-stage-label">{{ stage.label }}</span>
        <span class="potential-relative-label">{{ signed(stage.relativeEnergy) }} kcal/mol</span>
        <span class="potential-absolute-label">G {{ formatNumber(stage.energyHartree, 4) }} Eh</span>
      </div>
    </div>
    <dl class="reaction-potential-energy-metrics">
      <div><dt>正向活化自由能</dt><dd>{{ signed(activeEdgeMetrics.forwardBarrier) }} <small>kcal/mol</small></dd></div>
      <div><dt>反向活化自由能</dt><dd>{{ signed(activeEdgeMetrics.reverseBarrier) }} <small>kcal/mol</small></dd></div>
      <div><dt>反应自由能</dt><dd>{{ signed(activeEdgeMetrics.reaction) }} <small>kcal/mol</small></dd></div>
    </dl>
    <p class="reaction-potential-energy-caption">Gibbs 自由能取自热力学 profile；绝对值以 Eh 展示，平滑曲线按前体归一化。</p>
  </section>
</template>
