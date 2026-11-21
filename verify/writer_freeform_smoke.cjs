// Writer freeform outputs regression on real markup/CSS/module with the isolated fixture.
// Run: NAIBA_SMOKE_BASE=http://127.0.0.1:<fixture-port> node verify/writer_freeform_smoke.cjs
// Covers the 自由文本输出 exit block (view / copy / adopt-to-form) seeded on book 0
// and the hidden empty state on book 1. No provider, no pipeline execution.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const base = process.env.NAIBA_SMOKE_BASE;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(base)) throw new Error('Set NAIBA_SMOKE_BASE to the fixture URL (verify/_serve_writer_draft_ui.py)');
const url = base.replace(/\/$/, '');
const viewports = [{ width: 1280, height: 800 }, { width: 375, height: 812 }];

async function checkWriter(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const mobile = viewport.width === 375;
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('unhandledrejection', (reason) => errors.push(String(reason)));
  const $ = (selector) => page.locator(selector);
  const visible = (selector) => $(selector).waitFor({ state: 'visible' });
  const stage = (name) => $(`[data-writer-stage="${name}"]`).click();
  try {
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#writerPageState').textContent.includes('离线正文验收'));
    await stage('setup');

    // 区块只在有存档的作品显示：第一本 5 行，最新在前。
    await visible('#writerFreeformGroup');
    const rows = $('#writerFreeformList .writer-freeform-row');
    assert.equal(await rows.count(), 5);
    const labelOf = async (index) => (await rows.nth(index).locator('summary b').innerText()).trim();
    assert.equal(await labelOf(0), '拆章');
    assert.equal(await labelOf(4), '世界观');

    // 展开可见全文；拆章行只有「复制」，没有「填入表单」。
    const splitRow = rows.nth(0);
    await splitRow.locator('summary').click();
    await splitRow.locator('.writer-freeform-text').waitFor({ state: 'visible' });
    assert.match(await splitRow.locator('.writer-freeform-text').innerText(), /第1章 敲门/);
    assert.equal(await splitRow.getByRole('button', { name: '复制' }).count(), 1);
    assert.equal(await splitRow.locator('button', { hasText: '填入' }).count(), 0);
    await splitRow.getByRole('button', { name: '复制' }).click();
    await page.waitForFunction(() => document.querySelector('#fixtureMessage').textContent.includes('已复制'));

    // 题材行同样只复制；世界观/书名/简介有「填入表单」且填后要手动保存。
    const worldRow = rows.nth(4);
    const worldText = '旧港常年起雾，码头尽头的灯塔二十年前就熄了。';
    await worldRow.locator('summary').click();
    await worldRow.getByRole('button', { name: '填入世界观表单' }).click();
    await page.waitForFunction(() => document.querySelector('#fixtureMessage').textContent.includes('世界观已填入表单'));
    assert.equal(await $('#writerWorldview').inputValue(), worldText);

    // 字段已有内容时先确认：拒绝则不动，确认才替换。
    await $('#writerWorldview').fill('手写的世界观');
    const refuse = page.once('dialog', (dialog) => dialog.dismiss());
    await worldRow.getByRole('button', { name: '填入世界观表单' }).click();
    await refuse;
    await page.waitForTimeout(100);
    assert.equal(await $('#writerWorldview').inputValue(), '手写的世界观');
    const accept = page.once('dialog', (dialog) => dialog.accept());
    await worldRow.getByRole('button', { name: '填入世界观表单' }).click();
    await accept;
    await page.waitForTimeout(100);
    assert.equal(await $('#writerWorldview').inputValue(), worldText);

    const titleRow = rows.nth(3);
    await titleRow.locator('summary').click();
    // 书名/简介表单已有作品自带内容：走确认替换。
    const acceptTitle = page.once('dialog', (dialog) => dialog.accept());
    await titleRow.getByRole('button', { name: '填入书名表单' }).click();
    await acceptTitle;
    await page.waitForTimeout(100);
    assert.equal(await $('#writerTitle').inputValue(), '雾港来信');
    const blurbRow = rows.nth(2);
    await blurbRow.locator('summary').click();
    const acceptBlurb = page.once('dialog', (dialog) => dialog.accept());
    await blurbRow.getByRole('button', { name: '填入简介表单' }).click();
    await acceptBlurb;
    await page.waitForTimeout(100);
    assert.match(await $('#writerBlurb').inputValue(), /一封从雾里寄来的信/);

    // 手机上折叠行不撑破容器宽度。
    if (mobile) {
      const overflow = await page.evaluate(() => {
        const group = document.querySelector('#writerFreeformGroup');
        return group.scrollWidth - group.clientWidth;
      });
      assert.ok(overflow <= 1, `freeform group overflows by ${overflow}px`);
    }

    // 没有存档的作品整块隐藏。
    const guarded = page.once('dialog', (dialog) => dialog.accept());
    await $('#fixtureOtherBook').click();
    await page.waitForFunction(() => document.querySelector('#writerPageState').textContent.includes('第二本隔离作品'));
    await guarded;
    await stage('setup');
    await page.waitForTimeout(200);
    assert.equal(await $('#writerFreeformGroup').isVisible(), false);

    assert.deepEqual(errors, []);
    console.log(`freeform smoke OK (${viewport.width}x${viewport.height})`);
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    for (const viewport of viewports) await checkWriter(browser, viewport);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
