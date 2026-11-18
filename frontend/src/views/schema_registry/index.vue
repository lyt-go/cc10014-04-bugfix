<template>
  <section class="page" data-module="schema_registry">
    <header class="page-head">
      <div>
        <h2>结构注册管理</h2>
        <p class="page-desc">维护结构定义的输入、执行约束与结果记录，为结构注册提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记结构定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出结构注册清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无结构注册数据，可先登记结构定义</td>
        </tr>
      </tbody>
    </table>

    <section class="source-summary" data-testid="field-source-panel">
      <h3>目标字段当前来源核对（刷新结构注册后每个目标字段只有一个当前来源）</h3>
      <p v-if="!fieldSources.length" class="empty-state">暂无当前生效映射驱动的字段来源。</p>
      <table v-else class="data-table compact">
        <thead>
          <tr>
            <th>表名称</th>
            <th>目标字段</th>
            <th>当前来源规则</th>
            <th>源字段名</th>
            <th>来源批次</th>
            <th>规则版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in fieldSources" :key="`${item.表名称}-${item.目标字段}`" :data-testid="`source-row-${item.表名称}-${item.目标字段}`">
            <td>{{ item.表名称 }}</td>
            <td>{{ item.目标字段 }}</td>
            <td>{{ item.规则编号 }}</td>
            <td>{{ item.源字段名 }}</td>
            <td>{{ item.来源批次 || '—' }}</td>
            <td>{{ item.规则版本 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="source-summary" data-testid="transition-panel">
      <h3>结构定义过渡版本（旧字段来源说明与边界变更留痕）</h3>
      <p v-if="!transitions.length" class="empty-state">暂无过渡版本；旧映射废弃或新映射生效时在此留痕。</p>
      <ol v-else class="transition-list">
        <li v-for="item in transitions" :key="item.id" class="transition-item" :data-testid="`transition-row-${item.id}`">
          <strong>{{ item.表名称 }} · {{ item.定义版本 || '未生成结构定义' }} · {{ item.变更类型 }}</strong>
          <p class="transition-meta">
            触发规则 {{ item.触发规则 }}（{{ item.触发动作 }}） ｜
            新增：{{ (item.新增字段 || []).join('、') || '—' }} ｜
            移除：{{ (item.移除字段 || []).join('、') || '—' }}
          </p>
          <ul v-if="item.旧字段来源说明?.length" class="transition-meta">
            <li v-for="src in item.旧字段来源说明" :key="src.字段名称">
              旧来源：{{ src.字段名称 }} ← {{ src.旧来源规则 }}（{{ src.旧来源批次 || '无批次' }}，{{ src.旧结构版本 }}）
            </li>
          </ul>
        </li>
      </ol>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记结构定义</h3>
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
      <span>共 {{ total }} 条结构注册记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type FieldSource = {
  表名称: string
  目标字段: string
  规则编号: string
  源字段名: string
  来源批次: string
  规则版本: string
}
type PreviousSource = {
  字段名称: string
  旧结构版本: string
  旧来源规则: string
  旧来源批次: string
}
type Transition = {
  id: number
  表名称: string
  定义版本?: string
  变更类型: string
  触发规则: string
  触发动作: string
  新增字段?: string[]
  移除字段?: string[]
  旧字段来源说明?: PreviousSource[]
}

const ENDPOINT = '/api/schema_registry'
const columns = ["定义编号", "表名称", "字段名称", "字段类型", "可空标识", "默认值", "定义版本", "生效批次"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["结构定义总数", "待校验定义", "已生效定义"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const fieldSources = ref<FieldSource[]>([])
const transitions = ref<Transition[]>([])
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
    link.download = 'schema_registry.csv'
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
      // 透出后端单向状态机拦截原因
      throw new Error(payload?.message || '结构注册动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结构注册操作失败'
  }
}

async function loadFieldSources() {
  try {
    const response = await request(`${ENDPOINT}/field-sources`)
    if (!response.ok) throw new Error('字段来源核对数据读取失败')
    const payload = await response.json()
    fieldSources.value = (payload.items ?? []) as FieldSource[]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段来源核对数据读取失败'
  }
}

async function loadTransitions() {
  try {
    const response = await request(`${ENDPOINT}/transitions`)
    if (!response.ok) throw new Error('过渡版本读取失败')
    const payload = await response.json()
    transitions.value = (payload.items ?? []) as Transition[]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '过渡版本读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('结构定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    // 刷新结构注册后同步字段映射结果与过渡留痕
    await Promise.all([loadFieldSources(), loadTransitions()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结构注册列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.source-summary {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
}
.source-summary h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.data-table.compact th,
.data-table.compact td {
  font-size: 12px;
  padding: 6px 8px;
}
.transition-list {
  margin: 0;
  padding-left: 18px;
}
.transition-item {
  margin-bottom: 8px;
  font-size: 13px;
}
.transition-meta {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--muted);
}
</style>
