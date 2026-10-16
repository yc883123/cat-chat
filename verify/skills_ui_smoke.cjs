// Skills UI 改版冒烟：覆盖层 PATCH 生效、管理页详情卡、快速引用弹窗（桌面模态 + 手机抽屉）。
// 运行：NAIBA_SMOKE_BASE=http://127.0.0.1:8890 node verify/skills_ui_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8890';
const errors = [];

async function openSettings(page, tab) {
  await page.evaluate((t) => {
    const btn = document.querySelector(`[data-settings-tab="${t}"]`);
    if (btn) btn.click();
  }, tab);
  await page.waitForTimeout(300);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    // ---- 桌面 ----
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`); });
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3000);

    // 找一个已安装 Skill
    const skill = await page.evaluate(() => (window.localStorage && null) || null);
    const skillInfo = await page.evaluate(async () => {
      const token = (localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '');
      const res = await fetch('/api/skills/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: '{}',
      });
      const data = await res.json();
      const first = (data.skills || [])[0];
      return first ? { id: first.id, name: first.name, ref: first.ref } : null;
    });
    if (!skillInfo) throw new Error('环境里没有任何已安装 Skill，无法冒烟');

    // PATCH 别名 + 备注 → scan 直出
    const patched = await page.evaluate(async ({ id }) => {
      const token = (localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '');
      const res = await fetch(`/api/skills/${id}/override`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ alias: '冒烟别名', note: '冒烟备注内容' }),
      });
      return { status: res.status, body: await res.json().catch(() => ({})) };
    }, skillInfo);
    if (patched.status !== 200) throw new Error(`PATCH override 失败：${patched.status} ${JSON.stringify(patched.body)}`);
    const scanAfter = await page.evaluate(async () => {
      const token = (localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '');
      const res = await fetch('/api/bootstrap', { headers: { Authorization: `Bearer ${token}` } });
      const data = await res.json();
      return (data.skills || []).find((s) => s.alias === '冒烟别名') || null;
    });
    if (!scanAfter) throw new Error('bootstrap 未合并别名（alias 缺失）');
    if (scanAfter.note !== '冒烟备注内容') throw new Error('bootstrap 未合并备注（note 缺失）');
    if (scanAfter.name === '冒烟别名') throw new Error('别名不得覆盖原始 name（prompt 注入用）');
    if (scanAfter.ref !== skillInfo.ref) throw new Error('别名不得改变 ref');

    // 管理页：打开设置 → Skills 管理，详情卡出现
    await page.click('#openSettings');
    await page.waitForTimeout(500);
    await openSettings(page, 'skills');
    await page.waitForTimeout(500);
    const adminState = await page.evaluate(() => ({
      hasSearch: Boolean(document.querySelector('#skillAdminSearch')),
      rows: document.querySelectorAll('#installedSkillList .skill-item').length,
      aliasValue: document.querySelector('#skillDetailName')?.value || '',
      noteValue: document.querySelector('#skillDetailNote')?.value || '',
    }));
    if (!adminState.hasSearch) throw new Error('管理页缺少搜索框 #skillAdminSearch');
    if (!adminState.rows) throw new Error('管理页已安装列表为空');
    if (adminState.aliasValue !== '冒烟别名') throw new Error(`详情卡别名未回显：${adminState.aliasValue}`);
    if (adminState.noteValue !== '冒烟备注内容') throw new Error(`详情卡备注未回显：${adminState.noteValue}`);

    // 管理页搜索命中备注
    await page.fill('#skillAdminSearch', '冒烟备注内容');
    await page.waitForTimeout(300);
    const filtered = await page.evaluate(() => document.querySelectorAll('#installedSkillList .skill-item').length);
    if (filtered !== 1) throw new Error(`备注搜索未命中（rows=${filtered}）`);
    await page.fill('#skillAdminSearch', '');
    await page.waitForTimeout(200);

    // 快速引用弹窗（桌面模态）
    await page.keyboard.press('Escape');
    await page.evaluate(() => document.querySelector('#settingsDialog')?.close());
    await page.evaluate(() => document.querySelector('#openSkills')?.click());
    await page.waitForTimeout(500);
    const quick = await page.evaluate(() => ({
      open: document.querySelector('#skillsDialog')?.open || false,
      summary: document.querySelector('#skillsSummary')?.textContent || '',
      rows: document.querySelectorAll('#skillList [data-skill-insert]').length,
      hasMore: Boolean(document.querySelector('#skillList .skills-quick-more')),
      foot: document.querySelector('.skills-quick-foot')?.textContent || '',
    }));
    if (!quick.open) throw new Error('快速引用弹窗未打开');
    if (!quick.rows) throw new Error('快速引用弹窗列表为空');
    if (!quick.foot.includes('不截断')) throw new Error('弹窗页脚缺少「不截断」提示');
    if (!quick.hasMore) throw new Error('弹窗行缺少 ⋯ 菜单');
    // 别名优先显示
    const firstRowName = await page.evaluate(() => document.querySelector('#skillList [data-skill-insert] b')?.textContent || '');
    if (firstRowName !== '冒烟别名') throw new Error(`弹窗行未用别名显示：${firstRowName}`);
    await page.keyboard.press('Escape');

    // ---- 清理：清空该 skill 的覆盖层（桌面 page 仍开着，先清再关）----
    const cleaned = await page.evaluate(async ({ id }) => {
      const token = (localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '');
      const res = await fetch(`/api/skills/${id}/override`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ alias: '', note: '' }),
      });
      return res.status;
    }, skillInfo);
    if (cleaned !== 200) throw new Error('清理覆盖层失败');
    await page.close();

    // ---- 手机抽屉 ----
    const mob = await browser.newPage({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });
    mob.on('pageerror', (err) => errors.push(`[mobile] pageerror: ${err.message}`));
    mob.on('console', (msg) => { if (msg.type() === 'error') errors.push(`[mobile] console.error: ${msg.text()}`); });
    await mob.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await mob.waitForTimeout(2500);
    await mob.evaluate(() => document.querySelector('#openSkills')?.click());
    await mob.waitForTimeout(500);
    const drawer = await mob.evaluate(() => {
      const dlg = document.querySelector('#skillsDialog');
      const r = dlg?.getBoundingClientRect();
      return {
        open: dlg?.open || false,
        isDrawer: dlg?.classList.contains('is-drawer') || false,
        bottomAnchored: r ? Math.abs((r.bottom - window.innerHeight)) < 4 : false,
        height: r ? r.height : 0,
        handleVisible: (() => {
          const h = document.querySelector('.skill-drawer-handle');
          if (!h) return false;
          const cs = getComputedStyle(h);
          return cs.display !== 'none';
        })(),
      };
    });
    if (!drawer.open) throw new Error('[mobile] 弹窗未打开');
    if (!drawer.isDrawer) throw new Error('[mobile] 未进入抽屉形态（缺 .is-drawer）');
    if (!drawer.bottomAnchored) throw new Error('[mobile] 抽屉未贴底');
    if (!drawer.handleVisible) throw new Error('[mobile] 缺拖拽把手');
    await mob.close();

    if (errors.length) throw new Error(`页面错误：\n${errors.join('\n')}`);
    console.log('✅ Skills UI 冒烟通过：PATCH/scan 合并、管理页详情卡与备注搜索、桌面弹窗、手机抽屉、别名优先显示');
  } catch (err) {
    console.error('❌ Skills UI 冒烟失败：', err.message, '\n  at:', err.stack?.split('\n').slice(1, 4).join(' | '));
    if (errors.length) console.error('页面错误：\n' + errors.join('\n'));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
