<template>
  <section class="page" data-module="column_mapping">
    <header class="page-head">
      <div>
        <h2>字段映射管理</h2>
        <p class="page-desc">维护字段映射规则的输入、执行约束与结果记录，为字段映射提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记映射规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出字段映射清单</button>
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
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
          <th>版本时间线</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
          <tr :data-testid="`row-${row.id}`" :data-status="String(row.status)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              <span class="status-pill" :data-testid="`status-${row.id}`">{{ row.status }}</span>
              <em v-if="row.当前生效版本" class="effective-tag" data-testid="effective-tag">当前生效</em>
            </td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`action-${row.id}-${action}`"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`versions-${row.id}`"
                @click="toggleVersions(String(row.id))"
              >
                {{ expandedRow === String(row.id) ? '收起版本' : `查看版本（${versionCounts[String(row.id)] ?? 0}）` }}
              </button>
            </td>
          </tr>
          <tr v-show="expandedRow === String(row.id)" class="timeline-detail-row">
            <td :colspan="columns.length + 3">
              <div class="timeline-panel" :data-testid="`timeline-${row.id}`">
                <h4>字段映射版本表 · {{ row.规则编号 }}</h4>
                <p v-if="!versionRows[String(row.id)]?.length" class="empty-state">
                  该规则尚未发生状态推进；版本表只记录单向状态流转，不产生草稿残留。
                </p>
                <ol v-else class="timeline-list">
                  <li
                    v-for="version in versionRows[String(row.id)]"
                    :key="String(version.id)"
                    class="timeline-item"
                    :data-testid="`timeline-item-${version.id}`"
                  >
                    <div class="timeline-head">
                      <strong>v{{ version.版本序号 }} · {{ version.动作 }}</strong>
                      <span>{{ version.前置状态 }} → {{ version.状态 }}</span>
                      <em v-if="version.是否当前生效版本" class="effective-tag">当前生效版本</em>
                    </div>
                    <p class="timeline-meta">
                      源 {{ version.源字段名 }} → 目标 {{ version.目标字段名 }} ｜ 批次 {{ version.来源批次 || '—' }}
                    </p>
                    <p class="timeline-meta">{{ version.留痕说明 }} ｜ {{ version.记录时间 }}</p>
                  </li>
                </ol>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无字段映射数据，可先登记映射规则</td>
        </tr>
      </tbody>
    </table>

    <section class="version-summary" data-testid="effective-panel">
      <h3>当前生效版本数据结果</h3>
      <p v-if="!effectiveVersions.length" class="empty-state">暂无当前生效的字段映射版本。</p>
      <table v-else class="data-table compact">
        <thead>
          <tr>
            <th>规则编号</th>
            <th>目标字段</th>
            <th>源字段</th>
            <th>来源批次</th>
            <th>版本</th>
            <th>生效时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in effectiveVersions" :key="String(item.id)" :data-testid="`effective-row-${item.id}`">
            <td>{{ item.规则编号 }}</td>
            <td>{{ item.目标字段名 }}</td>
            <td>{{ item.源字段名 }}</td>
            <td>{{ item.来源批次 || '—' }}</td>
            <td>{{ item.规则版本 }}</td>
            <td>{{ item.记录时间 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记映射规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条字段映射记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/column_mapping'
const columns = ["规则编号", "源字段名", "目标字段名", "数据类型", "默认值", "转换函数", "规则版本", "生效日期"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["映射规则总数", "待校验规则", "已生效规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const expandedRow = ref<string>('')
const versionRows = ref<Record<string, Row[]>>({})
const versionCounts = ref<Record<string, number>>({})
const effectiveVersions = ref<Row[]>([])
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 2]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'column_mapping.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      // 后端会返回具体的单向流转/冲突原因，必须透出而不是吞掉
      throw new Error(payload?.message || '字段映射动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段映射操作失败'
  }
}

async function toggleVersions(rowId: string) {
  if (expandedRow.value === rowId) {
    expandedRow.value = ''
    return
  }
  expandedRow.value = rowId
  if (!versionRows.value[rowId]) {
    await loadVersions(rowId)
  }
}

async function loadVersions(rowId: string) {
  try {
    const response = await request(`${ENDPOINT}/${rowId}/versions`)
    if (!response.ok) throw new Error('字段映射版本表读取失败')
    const payload = await response.json()
    versionRows.value = { ...versionRows.value, [rowId]: payload.items ?? [] }
    versionCounts.value = { ...versionCounts.value, [rowId]: payload.total ?? 0 }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段映射版本表读取失败'
  }
}

async function loadEffectiveVersions() {
  try {
    const response = await request(`${ENDPOINT}/versions/effective`)
    if (!response.ok) throw new Error('当前生效版本读取失败')
    const payload = await response.json()
    effectiveVersions.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '当前生效版本读取失败'
  }
}

async function loadVersionCounts() {
  try {
    const response = await request(`${ENDPOINT}/versions/table`)
    if (!response.ok) return
    const payload = await response.json()
    const counts: Record<string, number> = {}
    for (const item of (payload.items ?? []) as Row[]) {
      const key = String(item.映射规则id)
      counts[key] = (counts[key] ?? 0) + 1
    }
    versionCounts.value = counts
  } catch {
    // 计数失败不阻塞主列表
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('映射规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    // 重新打开列表后同步版本计数与当前生效版本，保证与结构注册面板口径一致
    versionRows.value = {}
    expandedRow.value = ''
    await Promise.all([loadVersionCounts(), loadEffectiveVersions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段映射列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.status-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #eef2ff;
  color: #3730a3;
  white-space: nowrap;
}
.effective-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  font-style: normal;
  font-size: 11px;
  background: #dcfce7;
  color: #166534;
}
.timeline-detail-row td {
  padding: 0;
  background: #f8fafc;
}
.timeline-panel {
  padding: 12px 16px;
}
.timeline-panel h4 {
  margin: 0 0 8px;
  font-size: 13px;
}
.timeline-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.timeline-item {
  position: relative;
  padding: 8px 10px 8px 18px;
  border-left: 2px solid var(--brand);
  margin-left: 6px;
  background: #fff;
  border: 1px solid var(--border);
  border-left-width: 3px;
  border-radius: 6px;
  margin-bottom: 6px;
}
.timeline-head {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 13px;
}
.timeline-meta {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--muted);
}
.version-summary {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.version-summary h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.data-table.compact th,
.data-table.compact td {
  font-size: 12px;
  padding: 6px 8px;
}
</style>
