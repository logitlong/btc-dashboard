/* 三个页面共用：页面切换、手机点按反馈、浅色 / 深色切换、设置下拉、第一次打开先选账户、切换账户后提醒同步、检查云端令牌 */
"use strict";
const UI = (() => {
  const page = document.body.dataset.page;
  const NAMES = { main: "陈总", zhang: "张主任" };
  const ls = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* 存不了 */ } }, del: k => { try { localStorage.removeItem(k); } catch (e) { /* 无所谓 */ } } };
  const PAGES = [["panel", "./", "作战面板", "面板"], ["sim", "sim.html", "模拟盘", "模拟"], ["review", "review.html", "复盘", "复盘"]];
  document.querySelectorAll(".pnav").forEach(n => {
    n.setAttribute("aria-label", "页面");
    n.innerHTML = PAGES.map(([k, href, l, s]) => `<a href="${href}"${k === page ? ' aria-current="page"' : ""}><span class="l">${l}</span><span class="s">${s}</span></a>`).join("");
  });
  // ---------- 浅色 / 深色背景：顶栏按钮一键切换，存在本机（三个页面共用）；没选过就跟随系统 ----------
  const root = document.documentElement, sysDark = matchMedia("(prefers-color-scheme: dark)"), themeFns = [];
  const isDark = () => root.dataset.theme ? root.dataset.theme === "dark" : sysDark.matches;
  const SUN = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
  const thBtns = [];
  const paintTheme = () => thBtns.forEach(b => {
    const d = isDark();
    b.innerHTML = (d ? SUN : MOON) + `<span class="tl">${d ? "浅色" : "深色"}</span>`;
    b.setAttribute("aria-label", d ? "切换到浅色背景" : "切换到深色背景"); b.title = b.getAttribute("aria-label");
  });
  const themeChanged = () => { paintTheme(); themeFns.forEach(f => { try { f(isDark()); } catch (e) { /* 某个图表重画失败不影响其他 */ } }); };
  function setTheme(t) { root.dataset.theme = t; ls.set("btc_theme", t); themeChanged(); }
  function onTheme(fn) { themeFns.push(fn); }
  { const t = ls.get("btc_theme"); if (t === "light" || t === "dark") root.dataset.theme = t; }
  sysDark.addEventListener("change", () => { if (!root.dataset.theme) themeChanged(); });
  addEventListener("storage", e => { if (e.key === "btc_theme" && (e.newValue === "light" || e.newValue === "dark")) { root.dataset.theme = e.newValue; themeChanged(); } });
  document.querySelectorAll(".pnav").forEach(n => {
    const b = document.createElement("button"); b.type = "button"; b.className = "thbtn";
    b.onclick = () => setTheme(isDark() ? "light" : "dark");
    // 和页面切换放在一组（手机上同在第一行），不挤第二行的工具和行情数字
    const g = document.createElement("div"); g.className = "navgrp";
    n.before(g); g.append(b, n);
    thBtns.push(b);
  });
  paintTheme();

  const acct = ls.get("btc_account") === "zhang" ? "zhang" : "main";
  document.querySelectorAll(".prefs .who").forEach(el => { if (!el.textContent) el.textContent = NAMES[acct]; });
  document.addEventListener("click", e => document.querySelectorAll("details.prefs[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; }));
  document.addEventListener("keydown", e => { if (e.key === "Escape") document.querySelectorAll("details.prefs[open]").forEach(d => { d.open = false; }); });

  // ---------- 手机点按：按下立刻变色 + 震动，松手直接触发 ----------
  // 页面一直在刷新（价格、订单簿），iPhone 的 Safari 会把这种时候的点按当成「悬停」吞掉，要点好几下才有反应；
  // 所以按钮、链接不等 Safari 的 click，松手时自己触发。手指滑动了算滚动，不触发；输入框、勾选框照常交给系统
  const TAP = 'button,a[href],summary,[role="button"],.brow';
  const NOFAST = 'input,textarea,select,label,[contenteditable]';
  let tp = null, scrollAt = 0, hx = null;
  addEventListener("scroll", () => { scrollAt = Date.now(); }, { capture: true, passive: true });
  function buzz() {
    if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) { /* 不支持 */ } return; }
    // iPhone 不支持 vibrate：点一下隐藏的系统开关（iOS 18 起会有一下轻震）
    if (!hx) {
      hx = document.createElement("label"); hx.setAttribute("aria-hidden", "true");
      hx.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none";
      hx.innerHTML = '<input type="checkbox" switch tabindex="-1">';
      hx.addEventListener("click", e => e.stopPropagation());
      hx.firstChild.addEventListener("change", e => e.stopPropagation());
      document.body.appendChild(hx);
    }
    try { hx.click(); } catch (e) { /* 没震也不影响 */ }
  }
  const unpress = el => setTimeout(() => el.classList.remove("uipress"), 120);
  // 松手、滑动的事件挂在按下的那个元素上：它被重画掉以后，事件到不了 document，只有挂在它自己身上才收得到
  function track(tgt) {
    const off = () => { tgt.removeEventListener("touchmove", onMove); tgt.removeEventListener("touchend", onEnd); tgt.removeEventListener("touchcancel", onCancel); };
    const onMove = e => {
      if (!tp) return off();
      const t = e.touches[0];
      if (Math.abs(t.clientX - tp.x) > 10 || Math.abs(t.clientY - tp.y) > 10) { tp.el.classList.remove("uipress"); tp = null; off(); }
    };
    const onCancel = () => { if (tp) tp.el.classList.remove("uipress"); tp = null; off(); };
    const onEnd = e => { off(); fire(e); };
    tgt.addEventListener("touchmove", onMove, { passive: true });
    tgt.addEventListener("touchend", onEnd, { passive: false });
    tgt.addEventListener("touchcancel", onCancel);
  }
  document.addEventListener("touchstart", e => {
    tp = null;
    if (e.touches.length !== 1 || Date.now() - scrollAt < 150) return;
    const el = e.target.closest && e.target.closest(TAP);
    if (!el || el.closest(NOFAST) || el.disabled) return;
    const t = e.touches[0];
    tp = { el, x: t.clientX, y: t.clientY, at: Date.now() };
    el.classList.add("uipress");
    track(e.target);
  }, { capture: true, passive: true });
  function fire(e) {
    const p = tp; tp = null;
    if (!p) return;
    unpress(p.el);
    if (Date.now() - p.at > 700) return;   // 长按交给系统
    // 按下后这一块被重画了（订单簿、仓位卡片），就点手指下面现在那个
    let el = p.el;
    if (!el.isConnected) { const u = document.elementFromPoint(p.x, p.y); el = u && u.closest(TAP); }
    if (!el || el.disabled) return;
    e.preventDefault();                    // 不要 Safari 自己再补一个 click
    const ae = document.activeElement;
    if (ae && ae !== el && ae.matches && ae.matches("input,textarea")) ae.blur();
    buzz();
    el.click();
  }

  // 换页：点了马上标出来（页面还在加载时不会以为没点到）
  document.addEventListener("click", e => {
    const a = e.target.closest(".pnav a");
    if (!a || a.getAttribute("aria-current") || e.metaKey || e.ctrlKey) return;
    a.classList.add("uigo"); document.documentElement.classList.add("uiload");
  });
  addEventListener("pageshow", () => {
    document.documentElement.classList.remove("uiload");
    document.querySelectorAll(".pnav a.uigo").forEach(a => a.classList.remove("uigo"));
  });

  // ---------- 弹窗 ----------
  function modal(html) {
    const el = document.createElement("div"); el.className = "uimodal"; el.innerHTML = `<div role="dialog" aria-modal="true">${html}</div>`;
    document.body.appendChild(el); return el;
  }

  // ---------- 第一次在这台设备打开：先选是谁（不再默认陈总，免得把别人的单记到陈总名下） ----------
  if (!ls.get("btc_account")) {
    const m = modal(`<h3>这台设备是谁在用？</h3><p>喊单、交易日志、模拟盘按人分开存。选错了会把单记到别人名下，之后可以在右上角「设置」里切换。</p>
      <div class="uibig">${Object.entries(NAMES).map(([k, n]) => `<button type="button" data-a="${k}">${n}</button>`).join("")}</div>`);
    m.addEventListener("click", e => { const b = e.target.closest("[data-a]"); if (!b) return; ls.set("btc_account", b.dataset.a); markSwitched(b.dataset.a); location.reload(); });
  }

  // ---------- 切换账户后：提醒先同步一次云端 ----------
  let syncFn = null, waiters = [];
  function onSync(fn) { syncFn = fn; waiters.forEach(w => w(fn)); waiters = []; }
  const getSync = () => syncFn ? Promise.resolve(syncFn) : new Promise(r => waiters.push(r));
  function markSwitched(to) { ls.set("btc_acct_switch", JSON.stringify({ to, t: Date.now() })); }
  let sw = null; try { sw = JSON.parse(ls.get("btc_acct_switch")); } catch (e) { /* 没有 */ }
  if (sw && Date.now() - sw.t < 120e3 && ls.get("btc_account")) {
    ls.del("btc_acct_switch");
    const m = modal(`<h3>已切换到 ${NAMES[sw.to] || sw.to}</h3>
      <p>换了账户，建议马上同步一次云端：先把云端最新的记录拉下来，再接着操作，免得看到的是这台设备上的旧数据。</p>
      <p class="uist" aria-live="polite"></p>
      <div class="uirow"><button type="button" data-x>稍后</button><button type="button" class="pri" data-go>立即同步</button></div>`);
    const st = m.querySelector(".uist"), go = m.querySelector("[data-go]");
    m.querySelector("[data-x]").onclick = () => m.remove();
    go.onclick = async () => {
      if (go.dataset.done) return m.remove();
      go.disabled = true; st.className = "uist"; st.textContent = "同步中…";
      try { const r = await (await getSync())(); st.className = "uist " + (r.ok ? "ok" : "no"); st.textContent = (r.ok ? "✓ 已同步：" : "✗ 没同步成功：") + r.msg; }
      catch (e) { st.className = "uist no"; st.textContent = "✗ 没同步成功：" + (e.message || e); }
      go.disabled = false; go.dataset.done = "1"; go.textContent = "好的";
    };
  }

  // ---------- 云端令牌：先确认能打开私有仓库，打不开就说清楚原因（以前会被当成「云端还没有文件」） ----------
  let tokOk = { tok: "", at: 0 };
  async function checkToken(token) {
    if (!token || (tokOk.tok === token && Date.now() - tokOk.at < 10 * 60e3)) return;
    let r; try { r = await fetch("https://api.github.com/repos/logitlong/btc-records", { headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" }, cache: "no-store" }); }
    catch (e) { return; }   // 断网之类，交给后面的同步自己报错
    if (r.status === 401) throw new Error("令牌无效或已过期，请重新生成");
    if (r.status === 403 || r.status === 404) throw new Error("这个令牌打不开私有仓库 btc-records：要在 logitlong 账号下生成细粒度令牌，只勾 btc-records，Contents 选 Read and write");
    if (r.ok) tokOk = { tok: token, at: Date.now() };
  }
  return { onSync, markSwitched, checkToken, NAMES, isDark, onTheme, buzz };
})();
