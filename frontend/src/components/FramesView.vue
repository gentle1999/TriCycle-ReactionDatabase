<script setup lang="ts">
import { ChevronRight, Search } from "@lucide/vue";
import { computed, ref } from "vue";

import { formatDurationSeconds, formatEnergy, labelFor, statusTone } from "@/format";
import type { CalculationFrameSummary } from "@/types";

const props = defineProps<{
  frames: CalculationFrameSummary[];
  loading: boolean;
}>();

const emit = defineEmits<{ openFrame: [id: string] }>();

const search = ref("");
const role = ref("");

const roles = computed(() =>
  [...new Set(props.frames.map((frame) => frame.frame_role))].sort(),
);

const filteredFrames = computed(() => {
  const query = search.value.trim().toLowerCase();
  return props.frames.filter((frame) => {
    const matchesRole = !role.value || frame.frame_role === role.value;
    const matchesQuery =
      !query ||
      [frame.original_filename, frame.canonical_isomeric_smiles, frame.file_frame_index]
        .some((value) => String(value).toLowerCase().includes(query));
    return matchesRole && matchesQuery;
  });
});
</script>

<template>
  <section class="table-view" aria-labelledby="frame-view-title">
    <header class="table-toolbar">
      <div>
        <span class="eyebrow">CalculationFrame</span>
        <h1 id="frame-view-title">计算帧</h1>
      </div>
      <div class="filter-row">
        <label class="search-field is-wide">
          <Search :size="15" aria-hidden="true" />
          <span class="sr-only">筛选计算帧</span>
          <input v-model="search" type="search" placeholder="文件、SMILES 或帧号">
        </label>
        <label class="select-field">
          <span class="sr-only">帧角色</span>
          <select v-model="role">
            <option value="">全部角色</option>
            <option v-for="item in roles" :key="item" :value="item">{{ labelFor(item) }}</option>
          </select>
        </label>
      </div>
    </header>

    <div class="data-table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>文件 / 帧</th>
            <th>角色</th>
            <th>分子拓扑</th>
            <th>能量 / Hartree</th>
            <th>逐帧用时</th>
            <th>SCF</th>
            <th>优化</th>
            <th><span class="sr-only">详情</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !frames.length">
            <td colspan="8"><div class="table-loading">正在加载计算帧</div></td>
          </tr>
          <tr v-else-if="!filteredFrames.length">
            <td colspan="8"><div class="compact-empty">没有匹配的计算帧</div></td>
          </tr>
          <tr
            v-for="frame in filteredFrames"
            v-else
            :key="frame.id"
            class="clickable-row"
            tabindex="0"
            @click="emit('openFrame', frame.id)"
            @keydown.enter="emit('openFrame', frame.id)"
          >
            <td>
              <strong>{{ frame.original_filename }}</strong>
              <span>segment {{ frame.segment_index }} · frame {{ frame.file_frame_index }}</span>
            </td>
            <td><span class="role-pill">{{ labelFor(frame.frame_role) }}</span></td>
            <td><code>{{ frame.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code></td>
            <td class="number-cell">{{ formatEnergy(frame.selected_energy_hartree) }}</td>
            <td class="number-cell">{{ formatDurationSeconds(frame.running_time_seconds) }}</td>
            <td><span class="status-dot" :class="statusTone(frame.scf_status)">{{ labelFor(frame.scf_status) }}</span></td>
            <td><span class="status-dot" :class="statusTone(frame.optimization_status)">{{ labelFor(frame.optimization_status) }}</span></td>
            <td><ChevronRight :size="16" aria-hidden="true" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="table-summary">显示 {{ filteredFrames.length }} / {{ frames.length }} 个计算帧</p>
  </section>
</template>
