import { expect, test } from '@playwright/test'

/**
 * 字段映射状态流转 Bugfix 验收：
 * - 待校验不能直接跳已废弃、已生效不能回退（单向状态机，错误原因透出到页脚）；
 * - 字段映射版本表只增不倒序，保留当前生效版本数据结果；
 * - 旧映射废弃 + 新映射生效后，结构注册面板每个目标字段只有一个当前来源，
 *   过渡版本保留旧字段来源说明，历史版本不丢失。
 *
 * 依赖后端在 baseURL 的 /api 代理上运行（与 functionality.spec.ts 同一套环境）。
 */

const CM_ENDPOINT = '/api/column_mapping'

async function createRule(page: import('@playwright/test').Page, ruleNo: string, targetFields: string) {
  await page.getByTestId('open-create').click()
  await expect(page.getByTestId('create-modal')).toBeVisible()
  await page.getByTestId('create-规则编号').fill(ruleNo)
  await page.getByTestId('create-源字段名').fill('src_a,src_b,src_c')
  await page.getByTestId('create-目标字段名').fill(targetFields)
  await page.getByTestId('create-数据类型').fill('STRING,STRING,STRING')
  await page.getByTestId('create-默认值').fill(',,')
  await page.getByTestId('create-转换函数').fill('trim,trim,trim')
  await page.getByTestId('create-规则版本').fill('v1')
  await page.getByTestId('create-生效日期').fill('2026-11-18')
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)
}

test('字段映射：单向状态机拦截非法跳跃并透出原因', async ({ page }) => {
  await page.goto('/column_mapping')
  await page.waitForTimeout(2000)

  const ruleNo = 'MAP-E2E-FLOW-01'
  await createRule(page, ruleNo, 'flow_a,flow_b,flow_c')
  const row = page.locator('tbody tr').filter({ hasText: ruleNo }).first()
  await expect(row).toHaveAttribute('data-status', '草稿')

  // 草稿直接废弃：必须被拦，状态保持草稿，页脚展示后端的单向流转原因
  await row.getByRole('button', { name: '标记废弃', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '草稿')
  await expect(page.locator('.error-text')).toContainText('单向顺序')

  // 草稿直接生效同样被拦
  await row.getByRole('button', { name: '标记生效', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '草稿')
  await expect(page.locator('.error-text')).toContainText('单向顺序')

  // 合法推进到待校验
  await row.getByRole('button', { name: '提交校验', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '待校验')

  // 原始 Bug 场景：待校验直接跳已废弃必须被拦
  await row.getByRole('button', { name: '标记废弃', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '待校验')
  await expect(page.locator('.error-text')).toContainText('单向顺序')

  // 继续单向推进到已生效
  await row.getByRole('button', { name: '标记生效', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '已生效')
  await expect(row.getByTestId('effective-tag')).toBeVisible()

  // 已生效不能改回旧状态
  await row.getByRole('button', { name: '提交校验', exact: true }).click()
  await expect(row).toHaveAttribute('data-status', '已生效')
  await expect(page.locator('.error-text')).toContainText('单向顺序')

  // 版本表时间线：只包含待校验、已生效两条，顺序不倒置
  await row.getByRole('button', { name: /查看版本/ }).click()
  const items = page.getByTestId(/^timeline-item-/)
  await expect(items).toHaveCount(2)
  await expect(items.nth(0)).toContainText('待校验')
  await expect(items.nth(1)).toContainText('已生效')
  await expect(items.nth(1)).toContainText('当前生效版本')

  // 当前生效版本数据结果面板包含该规则
  await expect(page.getByTestId('effective-panel')).toContainText(ruleNo)
})

test('字段映射→结构注册：废弃旧规则再生效新规则后来源唯一且留痕', async ({ page, request }) => {
  // 直接通过 API 准备一条已生效规则（避免与上一个用例的 UI 时序耦合）
  const ruleA = 'MAP-E2E-SWITCH-A'
  const ruleB = 'MAP-E2E-SWITCH-B'
  const targets = 'switch_x,switch_y'

  async function postJson(path: string, body: unknown) {
    const response = await request.post(path, { data: body })
    return response.json() as Promise<{ ok: boolean; message: string; entry?: { id: number } }>
  }

  let res = await postJson(CM_ENDPOINT, {
    规则编号: ruleA, 源字段名: 'a1,a2', 目标字段名: targets,
    数据类型: 'STRING,STRING', 规则版本: 'v1', 来源批次: 'RAW-20261101',
  })
  expect(res.ok).toBeTruthy()
  const idA = res.entry!.id
  await postJson(`${CM_ENDPOINT}/${idA}/actions`, { action: '提交校验' })
  await postJson(`${CM_ENDPOINT}/${idA}/actions`, { action: '标记生效' })

  // 新规则未废弃旧规则直接生效 -> 冲突拦截
  res = await postJson(CM_ENDPOINT, {
    规则编号: ruleB, 源字段名: 'b1,b2', 目标字段名: targets,
    数据类型: 'STRING,STRING', 规则版本: 'v2', 来源批次: 'RAW-20261101',
  })
  const idB = res.entry!.id
  await postJson(`${CM_ENDPOINT}/${idB}/actions`, { action: '提交校验' })
  const blocked = await postJson(`${CM_ENDPOINT}/${idB}/actions`, { action: '标记生效' })
  expect(blocked.ok).toBeFalsy()
  expect(blocked.message).toContain('冲突')
  expect(blocked.message).toContain('优先顺序尚未确定')

  // 旧废弃 -> 新生效
  await postJson(`${CM_ENDPOINT}/${idA}/actions`, { action: '标记废弃' })
  const activated = await postJson(`${CM_ENDPOINT}/${idB}/actions`, { action: '标记生效' })
  expect(activated.ok).toBeTruthy()

  // 结构注册面板：刷新后每个目标字段只有一个当前来源，且指向新规则
  await page.goto('/schema_registry')
  await page.waitForTimeout(2000)
  const sourceRows = page.getByTestId('field-source-panel').locator('tbody tr')
  const switchRows = sourceRows.filter({ hasText: 'switch_' })
  await expect(switchRows).toHaveCount(2)
  for (const line of await switchRows.allInnerTexts()) {
    expect(line).toContain(ruleB)
    expect(line).not.toContain(ruleA)
  }

  // 过渡版本面板保留旧字段来源说明
  await expect(page.getByTestId('transition-panel')).toContainText('边界缩窄')
  await expect(page.getByTestId('transition-panel')).toContainText(ruleA)

  // 回到字段映射：历史版本不丢，旧规则时间线为 待校验→已生效→已废弃
  await page.goto('/column_mapping')
  await page.waitForTimeout(2000)
  const oldRow = page.locator('tbody tr').filter({ hasText: ruleA }).first()
  await oldRow.getByRole('button', { name: /查看版本/ }).click()
  const oldItems = page.getByTestId(/^timeline-item-/)
  await expect(oldItems).toHaveCount(3)
  await expect(oldItems.nth(0)).toContainText('待校验')
  await expect(oldItems.nth(1)).toContainText('已生效')
  await expect(oldItems.nth(2)).toContainText('已废弃')
})
