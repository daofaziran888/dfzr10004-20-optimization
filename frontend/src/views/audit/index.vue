<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>采集审计日志管理</h2>
        <p class="page-desc">维护采集审计的操作类型、执行记录与复核状态，为采集合规审计提供可追溯的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记审计记录</button>
        <button class="btn" type="button" @click="exportRows">导出采集审计日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>归档批次</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['归档批次'] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="row['归档批次']"
              class="link"
              type="button"
              @click="toggleBatch(row['归档批次'])"
            >
              {{ expandedBatch === row['归档批次'] ? '收起批次' : '回看批次' }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集审计日志数据，可先登记审计记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="batchDetail" class="batch-detail">
      <h4>
        归档批次 {{ batchDetail['批次编号'] }}（{{ batchDetail['状态'] }}）：回收容量
        {{ batchDetail['释放容量MB'] }}MB，剩余容量 {{ batchDetail['剩余容量MB'] ?? '—' }}MB
      </h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>分片编号</th>
            <th>大小MB</th>
            <th>批次内状态</th>
            <th>归档前校验</th>
            <th>源分片状态</th>
            <th>失败文件</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="shard in batchDetail['分片明细']" :key="shard['分片编号']">
            <td>{{ shard['分片编号'] }}</td>
            <td>{{ shard['大小MB'] }}</td>
            <td>{{ shard['批次内状态'] }}</td>
            <td>{{ shard['校验结果'] }}</td>
            <td>{{ shard['源分片状态'] }}</td>
            <td>{{ shard['失败文件']?.length ? shard['失败文件'].join('、') : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <section class="retry-section">
      <h3>归档待重试清单</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>归档批次</th>
            <th>配置编号</th>
            <th>分片编号</th>
            <th>原因</th>
            <th>审计记录编号</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in retries" :key="`${item['归档批次']}-${item['分片编号']}`">
            <td>{{ item['归档批次'] }}</td>
            <td>{{ item['配置编号'] }}</td>
            <td>{{ item['分片编号'] }}</td>
            <td>{{ item['原因'] }}</td>
            <td>{{ item['审计记录编号'] ?? '—' }}</td>
            <td>{{ item['状态'] }}</td>
          </tr>
          <tr v-if="!retries.length">
            <td colspan="6" class="empty-state">暂无待重试分片</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集审计日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]
const actions = ["标记复核", "归档记录"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const retries = ref<Row[]>([])
const summary = ref<Row>({ totals: {} })
const expandedBatch = ref('')
const batchDetail = ref<Row | null>(null)

const stats = computed(() => [
  { label: '审计记录总数', value: total.value },
  { label: '归档批次数', value: summary.value?.totals?.['归档批次数'] ?? 0 },
  { label: '已回收容量(MB)', value: summary.value?.totals?.['已回收容量MB'] ?? 0 },
  { label: '待重试分片', value: retries.value.length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '审计记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集审计日志动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志操作失败'
  }
}

async function toggleBatch(code: string) {
  errorMessage.value = ''
  if (expandedBatch.value === code) {
    expandedBatch.value = ''
    batchDetail.value = null
    return
  }
  try {
    const response = await request(`/api/storage/archives/${code}`)
    if (!response.ok) {
      throw new Error('归档批次详情读取失败')
    }
    batchDetail.value = await response.json()
    expandedBatch.value = code
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档批次详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, retryResp, summaryResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/pending-retries`),
      request('/api/storage/archives/summary'),
    ])
    if (!listResp.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (retryResp.ok) {
      retries.value = (await retryResp.json()).items ?? []
    }
    if (summaryResp.ok) {
      summary.value = await summaryResp.json()
    }
    if (expandedBatch.value) {
      const detailResp = await request(`/api/storage/archives/${expandedBatch.value}`)
      batchDetail.value = detailResp.ok ? await detailResp.json() : null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.batch-detail {
  margin-top: 12px;
}
.batch-detail h4 {
  font-size: 13px;
  margin: 0 0 8px;
  color: var(--muted);
}
.retry-section {
  margin-top: 16px;
}
.retry-section h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
</style>
