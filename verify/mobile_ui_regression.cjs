// 移动端两处回归验证（静态 public/ 服务，端口 8799）：
//   ① 顶栏「下沉箭头」收起后必须**只剩它自己**（API / Agent 两个下拉、操作区、侧栏按钮全部让位），
//      并亮出「展开顶栏」文字；点一次全部恢复。
//   ② 输入框粘贴：手机经局域网 http:// 打开时是**非安全上下文**，网页读不到剪贴板。
//      正确形态是「触摸长按不拦截 → 交给系统菜单」，桌面鼠标右键仍走自定义菜单；
//      两条程序化通道都不可用时，提示必须给可行动作（Ctrl+V / 长按系统菜单）。
// 用法：$env:NODE_PATH="<node_modules>"; node verify\mobile_ui_regression.cjs
//
// 说明：静态服务下没有后端，这里只考「前端形态与事件决策」。Android 系统长按菜单本身
// 无法在无头浏览器里断言，因此②用事件层的真值判定（contextmenu 有没有被 preventDefault）。
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8799;
const BASE = `http://127.0.0.1:${PORT}`;
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');

const failures = [];
function fail(label, detail = '') {
  console.log(`FAIL  ${label}${detail ? ` -> ${detail}` : ''}`);
  failures.push(label);
}
function pass(label) { console.log(`PASS  ${label}`); }

function waitForServer(deadline = 15) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tryOnce = () => {
      fetch(`${BASE}/index.html`).then((r) => {
        if (r.ok) resolve();
        else throw new Error('not ok');
      }).catch(() => {
        if (Date.now() - start > deadline * 1000) reject(new Error('server timeout'));
        else setTimeout(tryOnce, 300);
      });
    };
    tryOnce();
  });
}

(async () => {
  const server = spawn(PY, ['-m', 'http.server', String(PORT), '--directory', path.join(ROOT, 'public')], {
    cwd: ROOT,
    windowsHide: true,
  });
  server.stderr.on('data', (d) => console.error(String(d).trim()));
  let code = 1;
  try {
    await waitForServer();
    const browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 360, height: 800 } });
    const pageErrors = [];
    // 静态服务缺少 /api/bootstrap 与 favicon.ico，产生的 404 不是本次回归要测的问题。
    page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error' && !msg.text().includes('404 (File not found)')) {
        pageErrors.push(`console.error: ${msg.text()}`);
      }
    });

    await page.goto(`${BASE}/index.html`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(800);

    // 静态服务没有 /api/bootstrap，前端会弹出认证 dialog；关闭它以便测量顶栏。
    await page.evaluate(() => {
      document.querySelector('#authDialog')?.close();
      const ms = document.querySelector('#modelSelect');
      const as = document.querySelector('#agentSelect');
      if (ms) { ms.innerHTML = '<option>deepseek-v4.1-flash</option>'; ms.hidden = false; }
      if (as) { as.innerHTML = '<option>nsfagent</option>'; as.hidden = false; }
    });

    // ---- ① 顶栏折叠 ----
    // 展开态截图（此时是「API 栏 + Agent 栏 + 操作区 + 折叠条」的正常形态）
    const expandedPath = path.join(ROOT, 'verify', 'topbar_mobile_expanded.png');
    await page.screenshot({ path: expandedPath, fullPage: false });

    const expandedLayout = await page.evaluate(() => {
      const vis = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const st = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      };
      const topbar = document.querySelector('.topbar');
      const rect = topbar.getBoundingClientRect();
      return { height: Math.round(rect.height), model: vis('.model-control'), agent: vis('.agent-control'), actions: vis('.topbar-actions') };
    });
    if (expandedLayout.model && expandedLayout.agent && expandedLayout.actions) {
      pass(`展开态：API / Agent / 操作区都在（顶栏高 ${expandedLayout.height}px）`);
    } else {
      fail('展开态基线', JSON.stringify(expandedLayout));
    }

    // 通过真实点击折叠条切到收起态（按钮事件绑定也在验证范围内）。
    await page.click('#toggleTopbarCompact');
    await page.waitForTimeout(500);

    // 收起态截图
    const collapsedPath = path.join(ROOT, 'verify', 'topbar_mobile_collapsed.png');
    await page.screenshot({ path: collapsedPath, fullPage: false });

    // 布局断言：收起后只留下拉箭头，API / Agent / 操作区 / 侧栏按钮全部隐藏。
    const layout = await page.evaluate(() => {
      const topbar = document.querySelector('.topbar');
      const vis = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return 'missing';
        const st = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return (st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0);
      };
      const visibleChildren = [...topbar.children]
        .filter((el) => {
          const st = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
        })
        .map((el) => el.id || el.className);
      return {
        compact: topbar?.classList.contains('compact'),
        actions: vis('.topbar-actions'),
        model: vis('.model-control'),
        agent: vis('.agent-control'),
        openSidebar: vis('#openSidebar'),
        toggle: vis('#toggleTopbarCompact'),
        visibleChildren,
        topbarHeight: Math.round(topbar.getBoundingClientRect().height),
        toggleLabel: (() => {
          const l = document.querySelector('#toggleTopbarCompact .topbar-toggle-label');
          if (!l) return 'missing';
          const st = getComputedStyle(l);
          return st.display === 'none' ? 'hidden' : `visible:${l.textContent.trim()}`;
        })(),
        toggleRect: (() => {
          const t = document.querySelector('#toggleTopbarCompact');
          if (!t) return null;
          const r = t.getBoundingClientRect();
          return { top: Math.round(r.top), height: Math.round(r.height), width: Math.round(r.width) };
        })(),
        ariaExpanded: document.querySelector('#toggleTopbarCompact')?.getAttribute('aria-expanded'),
      };
    });
    console.log('layout snapshot:', JSON.stringify(layout, null, 2));

    if (layout.compact) pass('顶栏已切换为 compact 态');
    else fail('顶栏 compact 态');

    for (const [key, label] of [['actions', '操作区'], ['model', 'API 下拉'], ['agent', 'Agent 下拉'], ['openSidebar', '侧栏按钮']]) {
      if (layout[key] === false) pass(`收起后${label}已隐藏`);
      else fail(`收起后${label}隐藏`, String(layout[key]));
    }
    if (layout.visibleChildren.length === 1 && layout.visibleChildren[0] === 'toggleTopbarCompact') {
      pass('收起后顶栏只剩折叠条一个可见子元素');
    } else {
      fail('收起后只应剩折叠条', layout.visibleChildren.join(' | '));
    }
    if (layout.topbarHeight <= 64) {
      pass(`收起后顶栏只剩一条（高 ${layout.topbarHeight}px，展开态 ${expandedLayout.height}px）`);
    } else {
      fail('收起后顶栏仍然太高', `${layout.topbarHeight}px`);
    }
    if (layout.toggle === true && layout.toggleRect && layout.toggleRect.height >= 24) {
      pass(`收起后只剩下拉箭头条（高度 ${layout.toggleRect.height}px）`);
    } else {
      fail('收起后下拉箭头可见', JSON.stringify(layout.toggleRect));
    }
    if (String(layout.toggleLabel).startsWith('visible:')) {
      pass(`收起条带「展开顶栏」文字提示（${layout.toggleLabel.split(':')[1]}）`);
    } else {
      fail('收起条带文字提示可见', String(layout.toggleLabel));
    }
    if (layout.ariaExpanded === 'false') pass('收起后 aria-expanded=false');
    else fail('收起后 aria-expanded', String(layout.ariaExpanded));

    // 再点一次箭头，展开后 API / Agent / 操作区应全部恢复。
    await page.click('#toggleTopbarCompact');
    await page.waitForTimeout(500);
    const restored = await page.evaluate(() => {
      const vis = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const st = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      };
      return {
        compact: document.querySelector('.topbar')?.classList.contains('compact'),
        actions: vis('.topbar-actions'),
        model: vis('.model-control'),
        agent: vis('.agent-control'),
        ariaExpanded: document.querySelector('#toggleTopbarCompact')?.getAttribute('aria-expanded'),
      };
    });
    if (!restored.compact && restored.actions && restored.model && restored.agent && restored.ariaExpanded === 'true') {
      pass('再点箭头后 API / Agent / 操作区全部恢复');
    } else {
      fail('展开恢复', JSON.stringify(restored));
    }

    // ---- ② 输入框粘贴 ----
    const input = await page.$('#messageInput');
    if (!input) {
      fail('找不到 #messageInput');
    } else {
      // 触摸长按：事件链必须**不拦截** contextmenu（拦截就等于把系统粘贴菜单关掉了）。
      const touch = await input.evaluate((el) => {
        el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', button: 0 }));
        const ev = new PointerEvent('contextmenu', { bubbles: true, cancelable: true, pointerType: 'touch' });
        el.dispatchEvent(ev);
        const menu = document.querySelector('#textContextMenu');
        return {
          prevented: ev.defaultPrevented,
          menuHidden: !menu || menu.hidden,
        };
      });
      if (!touch.prevented) pass('触摸长按未拦截 contextmenu（系统粘贴菜单可用）');
      else fail('触摸长按被 preventDefault', '系统菜单会被一起关掉');
      if (touch.menuHidden) pass('触摸长按不再弹自定义菜单（避免死按钮）');
      else fail('触摸长按仍弹自定义菜单');

      // 鼠标右键：自定义编辑菜单必须照旧（桌面体验不受影响，七项齐全）。
      await page.click('#messageInput', { button: 'right' });
      await page.waitForTimeout(300);
      const mouseMenu = await page.evaluate(() => {
        const menu = document.querySelector('#textContextMenu');
        if (!menu || menu.hidden) return null;
        return [...menu.querySelectorAll('[data-context-action]')].map((b) => b.dataset.contextAction);
      });
      if (mouseMenu && mouseMenu.length === 7 && mouseMenu.includes('paste')) {
        pass(`鼠标右键仍是自定义菜单（${mouseMenu.join('/')}）`);
      } else {
        fail('鼠标右键自定义菜单', JSON.stringify(mouseMenu));
      }

      // 局域网 http 场景：`navigator.clipboard` 根本不存在（非安全上下文）。
      // 旧实现在这里弹「粘贴失败：浏览器未授权」——用户没有任何下一步可走。
      await page.evaluate(() => {
        Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
      });
      const pasteBtn = await page.$('#textContextMenu button[data-context-action="paste"]');
      if (pasteBtn) {
        await pasteBtn.click();
        await page.waitForTimeout(400);
        const toastText = await page.evaluate(() => {
          const el = document.querySelector('#toast');
          return el ? el.textContent.trim() : '';
        });
        if (toastText.includes('未授权')) fail('粘贴仍提示「浏览器未授权」', toastText);
        else if (toastText.includes('Ctrl+V') && toastText.includes('长按')) {
          pass(`粘贴不可用时给出可行动作（${toastText}）`);
        } else {
          fail('粘贴提示不给下一步动作', toastText || '(无提示)');
        }
      } else {
        fail('右键菜单里没有「粘贴」项');
      }
      await page.evaluate(() => document.querySelector('#textContextMenu')?.setAttribute('hidden', ''));
    }

    if (pageErrors.length) fail('零 pageerror / console.error', pageErrors.slice(0, 3).join(' | '));
    else pass('零 pageerror / console.error');

    await browser.close();
    code = failures.length ? 1 : 0;
    console.log();
    console.log(`移动端回归结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  } catch (error) {
    console.error(error);
    code = 1;
  } finally {
    server.kill();
  }
  process.exit(code);
})();
