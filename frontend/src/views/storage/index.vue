<template>
  <section class="page" data-module="storage">
    <header class="page-head">
      <div>
        <h2>采集存储策略管理</h2>
        <p class="page-desc">维护采集存储的路径规划、格式选择与归档策略，为采集数据持久化管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记存储配置</button>
        <button class="btn" type="button" @click="exportRows">导出采集存储策略清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集存储策略数据，可先登记存储配置</td>
        </tr>
      </tbody>
    </table>

    <section class="batch-section">
      <h3>归档批次</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in batchColumns" :key="column">{{ column }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch['批次编号'])">
            <td>{{ batch['批次编号'] }}</td>
            <td>{{ batch['配置编号'] }}</td>
            <td>{{ batch['状态'] }}</td>
            <td>{{ (batch['已处理分片'] ?? []).length }} / {{ (batch['选中分片'] ?? []).length }}</td>
            <td>{{ (batch['失败分片'] ?? []).length }}</td>
            <td>{{ batch['释放容量MB'] }}</td>
            <td>{{ batch['剩余容量MB'] ?? '—' }}</td>
            <td>{{ batch['审计记录编号'] ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="toggleBatch(batch['批次编号'])">
                {{ expandedBatch === batch['批次编号'] ? '收起' : '回看' }}
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td :colspan="batchColumns.length + 1" class="empty-state">暂无归档批次，可在已启用配置上执行归档</td>
          </tr>
        </tbody>
      </table>

      <div v-if="batchDetail" class="batch-detail">
        <h4>批次 {{ batchDetail['批次编号'] }} 分片明细（保留天数 {{ batchDetail['保留天数'] }} 天快照）</h4>
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
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集存储策略记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/storage'
const columns = ["配置编号", "存储路径", "文件格式", "分片大小", "压缩算法", "保留天数", "归档策略", "是否启用"]
const actions = ["提交校验", "启用配置", "停用配置", "执行归档"]
const batchColumns = ["批次编号", "配置编号", "状态", "已处理分片", "失败分片", "释放容量MB", "剩余容量MB", "审计记录编号"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const batches = ref<Row[]>([])
const summary = ref<Row>({ totals: {} })
const expandedBatch = ref('')
const batchDetail = ref<Row | null>(null)

const stats = computed(() => [
  { label: '存储配置总数', value: total.value },
  { label: '归档批次数', value: summary.value?.totals?.['归档批次数'] ?? 0 },
  { label: '已回收容量(MB)', value: summary.value?.totals?.['已回收容量MB'] ?? 0 },
  { label: '剩余容量(MB)', value: summary.value?.totals?.['剩余容量MB'] ?? 0 },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '存储配置登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  const isArchive = action === '执行归档'
  const url = isArchive ? `${ENDPOINT}/${row.id}/archive` : `${ENDPOINT}/${row.id}/actions`
  try {
    const response = await request(url, {
      method: 'POST',
      body: JSON.stringify(isArchive ? {} : { action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      throw new Error(payload?.detail ?? '采集存储策略动作未生效，请稍后重试')
    }
    if (payload && payload.ok === false) {
      errorMessage.value = payload.message ?? '采集存储策略动作未生效'
    } else if (payload?.message) {
      noticeMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储策略操作失败'
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
    const response = await request(`${ENDPOINT}/archives/${code}`)
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
    const [listResp, batchResp, summaryResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/archives`),
      request(`${ENDPOINT}/archives/summary`),
    ])
    if (!listResp.ok) {
      throw new Error('存储配置列表读取失败')
    }
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (batchResp.ok) {
      batches.value = (await batchResp.json()).items ?? []
    }
    if (summaryResp.ok) {
      summary.value = await summaryResp.json()
    }
    if (expandedBatch.value) {
      const detailResp = await request(`${ENDPOINT}/archives/${expandedBatch.value}`)
      batchDetail.value = detailResp.ok ? await detailResp.json() : null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储策略列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.batch-section {
  margin-top: 16px;
}
.batch-section h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
.batch-detail {
  margin-top: 12px;
}
.batch-detail h4 {
  font-size: 13px;
  margin: 0 0 8px;
  color: var(--muted);
}
.notice-text {
  color: #067647;
}
</style>
