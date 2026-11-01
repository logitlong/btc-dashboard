/* 复盘共用：下单 / 平仓那一刻的盘面快照、开单理由核对、自动标签、总结文字、平仓后走势。模拟盘和复盘页面都用这一份 */
"use strict";
const RV = (() => {
  const FAPI = "https://fapi.binance.com", REC_REPO = "logitlong/btc-records", DATA_REPO = "logitlong/btc-dashboard";
  const jget = u => fetch(u, { cache: "no-store" }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

  // ---------------- 选项（定下来以后别常改，否则前后统计接不上） ----------------
  const TAGS = [
    ["指标", [["kdj_gc", "KDJ 金叉"], ["kdj_dc", "KDJ 死叉"], ["rsi_os", "RSI 超卖"], ["rsi_ob", "RSI 超买"], ["macd_gc", "MACD 金叉"], ["macd_dc", "MACD 死叉"], ["macd_div", "MACD 背离"], ["vol_up", "放量"]]],
    ["点位", [["sr", "支撑 / 阻力位"], ["brk", "突破"], ["retest", "回踩确认"], ["tl", "趋势线"], ["fib", "斐波那契"], ["boll", "布林带触轨"]]],
    ["均线", [["ma_sr", "均线支撑 / 压力"], ["ma_bull", "均线多头排列"], ["ma_bear", "均线空头排列"]]],
    ["情绪 / 消息", [["news", "新闻"], ["fund", "资金费率极端"], ["ls", "多空比"]]],
    ["跟随", [["panel", "作战面板此刻结论"], ["zh_mtf", "张主任多周期信号"], ["peer", "对方喊单"]]],
    ["其他", [["gut", "直觉 / 感觉要涨或要跌"]]],
  ];
  const TAG_NAME = Object.fromEntries(TAGS.flatMap(g => g[1]));
  const CONF = [["low", "低"], ["mid", "中"], ["high", "高"]], CONF_NAME = Object.fromEntries(CONF);
  const CLOSE_WHY = [["target", "到目标了"], ["fear", "怕了"], ["wrong", "看反了"], ["bored", "不想拿了"], ["other", "其他"]];
  const CLOSE_NAME = Object.fromEntries(CLOSE_WHY);

  // ---------------- 格式 ----------------
  const p1 = x => Number.isFinite(x) ? x.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—";
  const sg = (x, d = 2) => !Number.isFinite(x) ? "—" : (x > 0 ? "+" : x < 0 ? "-" : "") + Math.abs(x).toFixed(d);
  const pc = (x, d = 2) => Number.isFinite(x) ? sg(x * 100, d) + "%" : "—";
  const dur = ms => { if (!Number.isFinite(ms) || ms < 0) return "—"; if (ms < 6e4) return `${Math.round(ms / 1e3)} 秒`; const m = Math.round(ms / 6e4); return m < 60 ? `${m} 分` : m < 1440 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${Math.floor(m / 1440)} 天 ${Math.floor(m % 1440 / 60)} 小时`; };
  const TFN = { "1m": "1分", "5m": "5分", "15m": "15分", "1h": "1小时", "4h": "4小时", "1d": "1日" };
  const TFMS = { "1m": 6e4, "5m": 3e5, "15m": 9e5, "1h": 36e5, "4h": 144e5, "1d": 864e5 };

  // ---------------- 指标（和模拟盘图上的算法一致） ----------------
  const nan = n => new Array(n).fill(NaN);
  function sma(a, n) { const o = nan(a.length); let s = 0, k = 0; for (let i = 0; i < a.length; i++) { s += a[i]; k++; if (k > n) { s -= a[i - n]; k = n; } if (k === n) o[i] = s / n; } return o; }
  function ema(a, n) { const o = nan(a.length), al = 2 / (n + 1); let p = NaN; for (let i = 0; i < a.length; i++) { p = Number.isFinite(p) ? al * a[i] + (1 - al) * p : a[i]; o[i] = p; } return o; }
  function rma(a, n) { const o = nan(a.length); let p = NaN, s = 0, k = 0; for (let i = 0; i < a.length; i++) { const v = a[i]; if (!Number.isFinite(v)) continue; if (!Number.isFinite(p)) { s += v; k++; if (k === n) { p = s / n; o[i] = p; } } else { p = (p * (n - 1) + v) / n; o[i] = p; } } return o; }
  function rsi(c, n) { const up = [NaN], dn = [NaN]; for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; up.push(Math.max(d, 0)); dn.push(Math.max(-d, 0)); } const u = rma(up, n), d = rma(dn, n); return u.map((x, i) => !Number.isFinite(x) ? NaN : d[i] === 0 ? 100 : 100 - 100 / (1 + x / d[i])); }
  const trueRange = (h, l, c) => h.map((x, i) => i ? Math.max(x - l[i], Math.abs(x - c[i - 1]), Math.abs(l[i] - c[i - 1])) : x - l[i]);
  function kdj(h, l, c) { const n = c.length, K = nan(n), D = nan(n), J = nan(n); let k = 50, d = 50;
    for (let i = 8; i < n; i++) { let hh = -Infinity, ll = Infinity; for (let j = i - 8; j <= i; j++) { hh = Math.max(hh, h[j]); ll = Math.min(ll, l[j]); }
      const rsv = hh === ll ? 50 : (c[i] - ll) / (hh - ll) * 100; k = 2 / 3 * k + rsv / 3; d = 2 / 3 * d + k / 3; K[i] = k; D[i] = d; J[i] = 3 * k - 2 * d; } return { K, D, J }; }
  function boll(c) { const m = sma(c, 20), sd = m.map((x, i) => { if (!Number.isFinite(x)) return NaN; let s = 0; for (let j = i - 19; j <= i; j++) s += (c[j] - x) ** 2; return Math.sqrt(s / 20); });
    return { M: m, U: m.map((x, i) => x + 2 * sd[i]), D: m.map((x, i) => x - 2 * sd[i]) }; }
  function macd(c) { const e12 = ema(c, 12), e26 = ema(c, 26), dif = e12.map((x, i) => x - e26[i]), dea = ema(dif, 9); return { DIF: dif, DEA: dea, H: dif.map((x, i) => x - dea[i]) }; }
  // 最近 3 根里有没有上穿 / 下穿
  const crossOf = (a, b) => { const n = a.length; for (let i = n - 1; i >= Math.max(1, n - 3); i--) { if (a[i - 1] <= b[i - 1] && a[i] > b[i]) return "金叉"; if (a[i - 1] >= b[i - 1] && a[i] < b[i]) return "死叉"; } return null; };
  const r2 = x => Number.isFinite(x) ? Math.round(x * 100) / 100 : null;

  // 一个周期的读数（最后一根是当时还没走完的那根，和屏幕上看到的一样）
  function readings(bars) {
    if (!bars || bars.length < 30) return null;
    const h = bars.map(b => b.h), l = bars.map(b => b.l), c = bars.map(b => b.c), v = bars.map(b => b.v), n = c.length, i = n - 1, x = c[i];
    const m7 = sma(c, 7)[i], m25 = sma(c, 25)[i], m99 = sma(c, 99)[i];
    const order = !Number.isFinite(m99) ? "—" : m7 > m25 && m25 > m99 ? "多头排列" : m7 < m25 && m25 < m99 ? "空头排列" : "交织";
    const bo = boll(c), bu = bo.U[i], bd = bo.D[i], pb = bu > bd ? (x - bd) / (bu - bd) : NaN;
    const kd = kdj(h, l, c), md = macd(c), atr = rma(trueRange(h, l, c), 14)[i];
    const vma = sma(v.slice(0, -1), 20).at(-1), vr = vma ? v[i - 1] / vma : NaN;   // 上一根走完的量 / 之前 20 根均量
    const trend = !Number.isFinite(m99) ? "—" : order === "多头排列" && x > m99 ? "上涨" : order === "空头排列" && x < m99 ? "下跌" : "震荡";
    return { c: x, ma7: r2(m7), ma25: r2(m25), ma99: r2(m99), order, trend, bu: r2(bu), bm: r2(bo.M[i]), bd: r2(bd), pb: r2(pb),
      rsi6: r2(rsi(c, 6)[i]), rsi12: r2(rsi(c, 12)[i]), rsi24: r2(rsi(c, 24)[i]), k: r2(kd.K[i]), d: r2(kd.D[i]), j: r2(kd.J[i]), kdjX: crossOf(kd.K, kd.D),
      dif: r2(md.DIF[i]), dea: r2(md.DEA[i]), hist: r2(md.H[i]), macdX: crossOf(md.DIF, md.DEA), atr: r2(atr), atrPct: r2(atr / x * 100), volR: r2(vr) };
  }

  // ---------------- 时段 ----------------
  function nyClock(ms) {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date(ms)).map(x => [x.type, x.value]));
    return { wd: p.weekday, min: (+p.hour % 24) * 60 + +p.minute };
  }
  function session(ms) {
    const c = nyClock(ms), wk = !["Sat", "Sun"].includes(c.wd);
    const name = !wk ? "周末" : c.min >= 570 && c.min < 960 ? "美盘" : c.min >= 180 && c.min < 570 ? "欧盘" : "亚盘";
    let toOpen = null;   // 到下一个美股开盘（9:30 美东）的分钟数，不算节假日
    for (let d = 0; d < 5; d++) { if (["Sat", "Sun"].includes(nyClock(ms + d * 864e5).wd)) continue; const m = d * 1440 + 570 - c.min; if (m > 0) { toOpen = m; break; } }
    return { name, usIn: name === "美盘" ? 0 : toOpen, usMin: name === "美盘" ? c.min - 570 : null };
  }

  // ---------------- 拉数据 ----------------
  const SNAP_TF = { "1m": 180, "5m": 300, "15m": 192, "1h": 168, "4h": 180 };
  const toBar = x => ({ t: +x[0], o: +x[1], h: +x[2], l: +x[3], c: +x[4], v: +x[5] });
  const kl = (tf, end, limit) => jget(`${FAPI}/fapi/v1/klines?symbol=BTCUSDT&interval=${tf}&limit=${limit}&endTime=${end}`).then(a => a.map(toBar));
  async function klRange(tf, from, to, maxReq = 8) {
    const out = []; let s = from;
    for (let i = 0; i < maxReq && s < to; i++) {
      const a = await jget(`${FAPI}/fapi/v1/klines?symbol=BTCUSDT&interval=${tf}&limit=1500&startTime=${s}&endTime=${to}`).catch(() => []);
      if (!a.length) break; out.push(...a.map(toBar)); s = +a.at(-1)[0] + TFMS[tf];
    }
    return out;
  }
  const fx = (path, end, limit) => jget(`${FAPI}/futures/data/${path}?symbol=BTCUSDT&period=5m&limit=${limit}&endTime=${end}`);
  let dataBase = null;
  async function dataUrl(token) {
    if (dataBase && Date.now() - dataBase.at < 60e3) return dataBase.url;
    let url = `https://raw.githubusercontent.com/${DATA_REPO}/data/`;
    try { const r = await fetch(`https://api.github.com/repos/${DATA_REPO}/git/ref/heads/data`, { cache: "no-cache", ...(token ? { headers: { Authorization: `Bearer ${token}` } } : {}) });
      if (r.ok) url = `https://raw.githubusercontent.com/${DATA_REPO}/${(await r.json()).object.sha}/`; } catch (e) { /* 按分支名读，最多晚 5 分钟 */ }
    dataBase = { url, at: Date.now() };
    return url;
  }
  const pack = bars => bars.map(b => [b.t, b.o, b.h, b.l, b.c, Math.round(b.v * 1000) / 1000]);
  const unpack = a => (a || []).map(x => ({ t: x[0], o: x[1], h: x[2], l: x[3], c: x[4], v: x[5] }));

  // 拍快照。ctx 是页面当场同步取好的东西（盘口、标记价、你看的周期和指标、画线、截图），这里补上要联网拉的部分
  async function capture(ctx) {
    const t = ctx.t, live = Date.now() - t < 120e3, end = live ? Date.now() : t;
    const tfs = Object.keys(SNAP_TF);
    const [kls, oi, top, glob, tak, st, pa] = await Promise.all([
      Promise.all(tfs.map(tf => kl(tf, end, SNAP_TF[tf]).catch(() => []))),
      fx("openInterestHist", end, 49).catch(() => null), fx("topLongShortAccountRatio", end, 1).catch(() => null),
      fx("globalLongShortAccountRatio", end, 1).catch(() => null), fx("takerlongshortRatio", end, 12).catch(() => null),
      live ? dataUrl(ctx.token).then(u => jget(u + "status.json")).catch(() => null) : null,
      live ? dataUrl(ctx.token).then(u => jget(u + "paper.json")).catch(() => null) : null]);
    const K = {}, RD = {};
    tfs.forEach((tf, i) => { K[tf] = pack(kls[i]); RD[tf] = readings(kls[i]); });
    const b1 = kls[0], b5 = kls[1], px = ctx.px;
    const ago = (bars, ms) => { const x = bars.filter(b => b.t <= t - ms).at(-1); return x ? px / x.c - 1 : NaN; };
    const oiN = oi && oi.length ? +oi.at(-1).sumOpenInterest : NaN, oiAt = k => oi && oi.length > k ? oiN / +oi.at(-1 - k).sumOpenInterest - 1 : NaN;
    const tk = tak && tak.length ? tak.reduce((s, x) => s + +x.buyVol, 0) / Math.max(1e-9, tak.reduce((s, x) => s + +x.sellVol, 0)) : NaN;
    const mkt = { mark: ctx.mark, fund: ctx.fund, oi: oiN, oi1h: oiAt(12), oi4h: oiAt(48), topLS: top && top[0] ? +top[0].longShortRatio : NaN, retailLS: glob && glob[0] ? +glob[0].longShortRatio : NaN, taker1h: tk };
    let news = null;
    if (st) {
      const recent = a => (a || []).filter(x => { const tt = Date.parse(x.time); return Number.isFinite(tt) && t - tt < 2 * 36e5 && tt <= t + 6e4; });
      news = { alerts: recent(st.alerts).filter(a => a.level === "red" || a.level === "yellow").slice(0, 8).map(a => ({ time: a.time, level: a.level, kind: a.kind, title: a.title })),
        upcoming: (st.upcoming || []).slice(0, 4).map(u => ({ title: u.title, at: u.at_ms, in: Math.round((u.at_ms - t) / 6e4) })), ruler: st.ruler || null };
    }
    const model = pa && pa.rules ? Object.fromEntries(Object.entries(pa.rules).map(([k, r]) => [k, { mode: r.mode, side: r.position && r.position.side || null }])) : null;
    return { v: 1, id: ctx.id, acct: ctx.acct, kind: ctx.kind, t, live, side: ctx.side, px, qty: ctx.qty, lev: ctx.lev, mode: ctx.mode,
      book: live ? ctx.book : null, view: ctx.view, mkt, chg: { m5: ago(b1, 3e5), h1: ago(b5, 36e5), h4: ago(b5, 4 * 36e5), h24: ago(b5, 24 * 36e5) },
      sess: session(t), news, model, rd: RD, kl: K, shot: ctx.shot || null };
  }

  // ---------------- 开单理由自动核对：拿你选的理由去对当时的真实读数 ----------------
  function lineAt(d, t) {   // 画线在时间 t 的价格（趋势线 / 射线按两点连线延长）
    if (d.k === "hline") return d.a.p;
    if (!d.b || d.b.t === d.a.t) return NaN;
    const u = (t - d.a.t) / (d.b.t - d.a.t);
    if (d.k === "trend" && (u < -0.05 || u > 1.05)) return NaN;
    if (d.k === "ray" && u < -0.05) return NaN;
    return d.a.p + (d.b.p - d.a.p) * u;
  }
  function verify(tags, snap, tr) {
    const order = [...new Set([snap.view && snap.view.tf, "1m", "5m", "15m", "1h", "4h"].filter(tf => tf && snap.rd[tf]))];
    const first = f => { for (const tf of order) { const r = snap.rd[tf], m = f(r); if (m) return `${TFN[tf]} ${m}`; } return null; };
    const near = (a, b, tol) => Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) / b <= tol;
    const px = snap.px, draws = (snap.view && snap.view.draws) || [];
    const res = (ok, txt) => ({ ok, txt });
    const rd5 = snap.rd["5m"] || {};
    const C = {
      rsi_os: () => { const m = first(r => r.rsi6 <= 30 && `RSI6=${r.rsi6}`); return m ? res(true, m) : res(false, `没到超卖（5分 RSI6=${rd5.rsi6 ?? "—"}，一般 ≤30 算超卖）`); },
      rsi_ob: () => { const m = first(r => r.rsi6 >= 70 && `RSI6=${r.rsi6}`); return m ? res(true, m) : res(false, `没到超买（5分 RSI6=${rd5.rsi6 ?? "—"}，一般 ≥70 算超买）`); },
      kdj_gc: () => { const m = first(r => r.kdjX === "金叉" && `K=${r.k} D=${r.d}`); return m ? res(true, m + " 最近 3 根内金叉") : res(false, "各周期最近 3 根都没有 KDJ 金叉"); },
      kdj_dc: () => { const m = first(r => r.kdjX === "死叉" && `K=${r.k} D=${r.d}`); return m ? res(true, m + " 最近 3 根内死叉") : res(false, "各周期最近 3 根都没有 KDJ 死叉"); },
      macd_gc: () => { const m = first(r => r.macdX === "金叉" && `DIF=${r.dif}`); return m ? res(true, m + " 最近 3 根内金叉") : res(false, "各周期最近 3 根都没有 MACD 金叉"); },
      macd_dc: () => { const m = first(r => r.macdX === "死叉" && `DIF=${r.dif}`); return m ? res(true, m + " 最近 3 根内死叉") : res(false, "各周期最近 3 根都没有 MACD 死叉"); },
      vol_up: () => { const m = first(r => r.volR >= 1.5 && `量是均量的 ${r.volR} 倍`); return m ? res(true, m) : res(false, `没明显放量（5分 上一根是均量的 ${rd5.volR ?? "—"} 倍，≥1.5 倍算放量）`); },
      boll: () => { const m = first(r => (r.pb <= 0.05 || r.pb >= 0.95) && `在布林带${r.pb >= 0.95 ? "上" : "下"}轨附近`); return m ? res(true, m) : res(false, "各周期价格都不在布林带上下轨附近"); },
      ma_sr: () => { const m = first(r => near(px, r.ma25, 0.003) ? `贴近 MA25 ${p1(r.ma25)}` : near(px, r.ma99, 0.003) ? `贴近 MA99 ${p1(r.ma99)}` : null); return m ? res(true, m) : res(false, "价格离各周期 MA25 / MA99 都超过 0.3%"); },
      ma_bull: () => { const m = first(r => r.order === "多头排列" && "MA7>MA25>MA99"); return m ? res(true, m) : res(false, "各周期都不是多头排列"); },
      ma_bear: () => { const m = first(r => r.order === "空头排列" && "MA7<MA25<MA99"); return m ? res(true, m) : res(false, "各周期都不是空头排列"); },
      fund: () => { const f = snap.mkt.fund; return !Number.isFinite(f) ? res(null, "没拿到资金费率") : Math.abs(f) >= 0.0003 ? res(true, `资金费率 ${pc(f, 4)}`) : res(false, `资金费率 ${pc(f, 4)}，不算极端（一般 ±0.03% 以上才算）`); },
      news: () => { if (!snap.news) return res(null, "当时没拿到新闻源"); const a = snap.news.alerts; return a.length ? res(true, `前 2 小时有 ${a.length} 条红 / 黄新闻：${a[0].title.slice(0, 40)}`) : res(false, "前 2 小时没有红色或黄色新闻"); },
      ls: () => res(null, `大户多空比 ${Number.isFinite(snap.mkt.topLS) ? snap.mkt.topLS.toFixed(2) : "—"}，散户 ${Number.isFinite(snap.mkt.retailLS) ? snap.mkt.retailLS.toFixed(2) : "—"}`),
      sr: () => { const d = draws.find(d => (d.k === "hline" && near(d.a.p, px, 0.005)) || (d.k === "rect" && d.b && px >= Math.min(d.a.p, d.b.p) * 0.997 && px <= Math.max(d.a.p, d.b.p) * 1.003));
        return d ? res(true, `图上 ${d.k === "hline" ? "水平线 " + p1(d.a.p) : "矩形区域"} 就在价格附近`) : res(null, "图上价格附近没有画水平线或矩形，系统没法核对"); },
      tl: () => { const d = draws.find(d => (d.k === "trend" || d.k === "ray") && near(lineAt(d, snap.t), px, 0.005)); return d ? res(true, `图上趋势线此处约 ${p1(lineAt(d, snap.t))}`) : res(null, "图上价格附近没有画趋势线，系统没法核对"); },
      fib: () => { const d = draws.find(d => d.k === "fib" && d.b && [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].some(f => near(d.b.p + (d.a.p - d.b.p) * f, px, 0.003)));
        return d ? res(true, "价格贴近图上斐波那契的一档") : res(null, "价格附近没有画斐波那契，系统没法核对"); },
      peer: () => tr && tr.from ? res(true, `从 ${tr.from.who} 的喊单带过来`) : res(null, "不是从喊单一键带过来的，系统没法核对"),
      panel: () => { const m = snap.model; if (!m) return res(null, "当时没拿到模型状态"); const on = Object.entries(m).filter(([, r]) => r.mode && r.mode !== "flat"); return res(null, on.length ? `模型规则 ${on.map(([k, r]) => `${k}：${r.mode}${r.side ? (r.side === "long" ? "多" : "空") : ""}`).join("，")}` : "模型规则 A / B 当时都空仓"); },
    };
    return tags.map(k => { const f = C[k], r = f ? f() : res(null, "这一项系统没法自动核对"); return [k, r.ok, r.txt]; });
  }

  // ---------------- 自动标签 ----------------
  const dirOf = side => side === "long" ? 1 : -1;
  const planOf = tr => tr.orig ? { tp: tr.orig.tp, sl: tr.orig.sl } : tr.mods && tr.mods.length ? { tp: tr.mods[0].from.tp, sl: tr.mods[0].from.sl } : { tp: tr.tp, sl: tr.sl };
  // trades：同一账户的全部仓位（新的在前），用来判断报复单、当天第几单
  function openFlags(tr, snap, trades, wallet, tzOff = 540) {
    const f = [], d = dirOf(tr.side), r5 = snap.rd["5m"], r1h = snap.rd["1h"];
    if (Number.isFinite(snap.chg.m5) && r5 && r5.atr) { const mv = d * snap.chg.m5 * snap.px / r5.atr; if (mv >= 1) f.push(["chase", `追单：开仓前 5 分钟已朝开仓方向走了 ${mv.toFixed(1)} 个 ATR`]); }
    if (r1h && ((d > 0 && r1h.trend === "下跌") || (d < 0 && r1h.trend === "上涨"))) f.push(["counter", `逆 1 小时趋势（1 小时是${r1h.trend}）`]);
    const prev = trades.filter(x => x.id !== tr.id && x.exit && x.exit.t <= tr.t).sort((a, b) => b.exit.t - a.exit.t)[0];
    if (prev && tr.t - prev.exit.t < 15 * 6e4 && (prev.real || 0) - (prev.fee || 0) < 0 && tr.qty * tr.entry > (prev.cq || prev.qty) * prev.entry * 1.0001)
      f.push(["revenge", `报复单：上一单亏了，${Math.round((tr.t - prev.exit.t) / 6e4)} 分钟后又开，而且仓位更大`]);
    const day = t => Math.floor((t + tzOff * 6e4) / 864e5), n = trades.filter(x => day(x.t) === day(tr.t) && x.t <= tr.t).length;
    if (n >= 4) f.push(["many", `今天第 ${n} 单`]);
    if (wallet > 0 && tr.qty * tr.entry / tr.lev > wallet * 0.3) f.push(["heavy", `重仓：保证金约占余额的 ${Math.round(tr.qty * tr.entry / tr.lev / wallet * 100)}%`]);
    const ev = snap.news && snap.news.upcoming.find(u => u.in >= -5 && u.in <= 30);
    if (ev) f.push(["event", `${ev.title}${ev.in >= 0 ? ` 还有 ${ev.in} 分钟` : " 刚开始"}`]);
    if (snap.news && snap.news.alerts.some(a => a.level === "red" && snap.t - Date.parse(a.time) < 30 * 6e4)) f.push(["rednews", "开仓前 30 分钟内有红色新闻"]);
    return f;
  }
  function closeFlags(tr) {
    const f = [], d = dirOf(tr.side), e = tr.entry, x = tr.exit, plan = planOf(tr);
    const mfe = Number.isFinite(tr.hi) ? (d > 0 ? tr.hi / e - 1 : 1 - tr.lo / e) : NaN, mae = Number.isFinite(tr.lo) ? (d > 0 ? tr.lo / e - 1 : 1 - tr.hi / e) : NaN;
    const got = d * (x.px / e - 1), manual = !["止盈", "止损", "强平"].includes(x.why);
    if (mfe >= 0.003 && got < mfe * 0.5 && x.why !== "止盈") f.push(["giveback", `拿不住：最高浮盈 ${pc(mfe)}，最后只拿到 ${pc(got)}`]);
    if (!plan.sl) f.push(["nosl", "开仓时没设止损"]);
    else if (Number.isFinite(mae) && mae < -Math.abs(plan.sl / e - 1) - 0.0005) f.push(["hold", `扛单：浮亏 ${pc(mae)} 超过了原止损（${pc(-Math.abs(plan.sl / e - 1))}）`]);
    let away = 0, tight = 0;
    (tr.mods || []).forEach(m => { const a = m.from.sl, b = m.sl; if (a === b) return; if (!b || (a && d * (b - a) < 0)) away++; else if (!a || d * (b - a) > 0) tight++; });
    if (away) f.push(["slaway", `止损往远处挪 / 撤掉了 ${away} 次`]);
    if (tight) f.push(["sltight", `收紧止损 ${tight} 次`]);
    if (manual && plan.tp && d * (x.px - plan.tp) < 0) f.push(["early", `原计划止盈 ${p1(plan.tp)} 没到就平了`]);
    if (x.why === "强平") f.push(["liq", "被强平"]);
    return { flags: f, mfe, mae, got };
  }

  // ---------------- 总结文字 ----------------
  const trendLine = snap => ["5m", "1h", "4h"].map(tf => { const r = snap.rd[tf]; return r ? `${TFN[tf]}${r.trend}（${r.order}，价格在 MA99 ${r.c >= r.ma99 ? "上" : "下"}方）` : null; }).filter(Boolean).join("；");
  const mktLine = snap => { const m = snap.mkt, a = [];
    if (Number.isFinite(m.fund)) a.push(`资金费率 ${pc(m.fund, 4)}`);
    if (Number.isFinite(m.oi1h)) a.push(`OI 1 小时 ${pc(m.oi1h)}`);
    if (Number.isFinite(m.topLS)) a.push(`大户多空比 ${m.topLS.toFixed(2)}`);
    if (Number.isFinite(m.retailLS)) a.push(`散户 ${m.retailLS.toFixed(2)}`);
    if (Number.isFinite(m.taker1h)) a.push(`1 小时主动买卖比 ${m.taker1h.toFixed(2)}`);
    return a.join("，"); };
  const sessLine = snap => { const s = snap.sess; return s.name + (s.name === "美盘" ? `（开盘后 ${dur(s.usMin * 6e4)}）` : s.usIn != null ? `（距美股开盘 ${dur(s.usIn * 6e4)}）` : ""); };
  const indLine = (snap, tf) => { const r = snap.rd[tf]; if (!r) return ""; return `${TFN[tf]} RSI6 ${r.rsi6}，KDJ ${r.k}/${r.d}/${r.j}${r.kdjX ? "（" + r.kdjX + "）" : ""}，MACD 柱 ${r.hist}${r.macdX ? "（" + r.macdX + "）" : ""}，布林位置 ${Number.isFinite(r.pb) ? Math.round(r.pb * 100) + "%" : "—"}，量比 ${r.volR ?? "—"}`; };
  function openSummary(tr, snap, rsn, chk, flags) {
    const L = tr.side === "long", plan = planOf(tr), e = tr.entry, out = [];
    out.push(`${L ? "做多" : "做空"} ${tr.qty.toFixed(3)} BTC @ ${p1(e)}（${tr.lev}x ${tr.mode === "isolated" ? "逐仓" : "全仓"}）。`);
    if (rsn && rsn.tags && rsn.tags.length) out.push(`理由：\n${chk.map(([k, ok, txt]) => `  ${ok === true ? "✓" : ok === false ? "✗" : "·"} ${TAG_NAME[k] || k}：${txt}`).join("\n")}`);
    if (rsn && rsn.conf) out.push(`信心：${CONF_NAME[rsn.conf]}。`);
    if (rsn && rsn.note) out.push(`${rsn.tags && rsn.tags.length ? "备注" : "理由（手写）"}：${rsn.note}`);
    out.push(`盘面：${trendLine(snap)}。过去 5 分钟 ${pc(snap.chg.m5)}、1 小时 ${pc(snap.chg.h1)}、4 小时 ${pc(snap.chg.h4)}、24 小时 ${pc(snap.chg.h24)}。`);
    const v = snap.view && snap.view.tf; out.push(`指标：${indLine(snap, v && snap.rd[v] ? v : "5m")}。`);
    const ml = mktLine(snap); if (ml) out.push(ml + "。");
    out.push(`${sessLine(snap)}。`);
    if (snap.news && snap.news.upcoming[0]) out.push(`下一个事件：${snap.news.upcoming[0].title}（${snap.news.upcoming[0].in} 分钟后）。`);
    if (plan.sl || plan.tp) { const sl = plan.sl ? Math.abs(plan.sl / e - 1) : NaN, tp = plan.tp ? Math.abs(plan.tp / e - 1) : NaN;
      out.push(`计划：止损 ${plan.sl ? `${p1(plan.sl)}（-${(sl * 100).toFixed(2)}%）` : "没设"}，止盈 ${plan.tp ? `${p1(plan.tp)}（+${(tp * 100).toFixed(2)}%）` : "没设"}${sl && tp ? `，盈亏比 1:${(tp / sl).toFixed(1)}` : ""}。`); }
    else out.push("计划：没设止盈止损。");
    if (!snap.live) out.push("（这一单是页面关着时成交、打开后补算的，盘面按 K 线补拍，没有盘口和新闻。）");
    flags.forEach(([, t]) => out.push("⚠ " + t + "。"));
    return out.join("\n");
  }
  // 第 i+1 次加仓：这一笔自己的理由、核对、当时盘面
  function addSummary(tr, i, snap, chk, flags) {
    const ad = tr.adds[i], L = tr.side === "long", out = [];
    const base = tr.qty - tr.adds.reduce((t, a) => t + a.qty, 0), after = base + tr.adds.slice(0, i + 1).reduce((t, a) => t + a.qty, 0);
    out.push(`第 ${i + 1} 次加仓：${L ? "多" : "空"} +${ad.qty.toFixed(3)} BTC @ ${p1(ad.px)}，开仓后 ${dur(ad.t - tr.t)}，加完共 ${after.toFixed(3)} BTC。`);
    if (ad.tags && ad.tags.length) out.push(`理由：\n${chk.map(([k, ok, txt]) => `  ${ok === true ? "✓" : ok === false ? "✗" : "·"} ${TAG_NAME[k] || k}：${txt}`).join("\n")}`);
    else if (!ad.note) out.push("这一笔加仓没有记理由（更新之前下的单）。");
    if (ad.conf) out.push(`信心：${CONF_NAME[ad.conf]}。`);
    if (ad.note) out.push(`${ad.tags && ad.tags.length ? "备注" : "理由（手写）"}：${ad.note}`);
    out.push(`盘面：${trendLine(snap)}。过去 5 分钟 ${pc(snap.chg.m5)}、1 小时 ${pc(snap.chg.h1)}、4 小时 ${pc(snap.chg.h4)}。`);
    const v = snap.view && snap.view.tf; out.push(`指标：${indLine(snap, v && snap.rd[v] ? v : "5m")}。`);
    const ml = mktLine(snap); if (ml) out.push(ml + "。");
    out.push(`${sessLine(snap)}。`);
    if (!snap.live) out.push("（页面关着时成交、打开后补算的，盘面按 K 线补拍。）");
    flags.forEach(([, t]) => out.push("⚠ " + t + "。"));
    return out.join("\n");
  }
  function closeSummary(tr, snap, cf, why) {
    const x = tr.exit, out = [], net = (tr.real || 0) - (tr.fee || 0), im = tr.qty * tr.entry / tr.lev;
    const wTxt = why ? (why.why === "other" && why.note ? why.note : `${CLOSE_NAME[why.why] || why.why}${why.note ? "，" + why.note : ""}`) : "";
    out.push(`拿了 ${dur(x.t - tr.t)}，平在 ${p1(x.px)}（${x.why}${wTxt ? ` · 理由：${wTxt}` : ""}）。`);
    out.push(`盈亏 ${sg(tr.real || 0)} USDT，扣手续费后 ${sg(net)} USDT（按价格 ${pc(cf.got)}，保证金回报 ${im ? sg(net / im * 100) + "%" : "—"}）。`);
    if (Number.isFinite(cf.mfe)) out.push(`持仓中最高浮盈 ${pc(cf.mfe)}${tr.hiT || tr.loT ? `（开仓后 ${dur((tr.side === "long" ? tr.hiT : tr.loT) - tr.t)}）` : ""}，最大浮亏 ${pc(cf.mae)}${tr.hiT || tr.loT ? `（开仓后 ${dur((tr.side === "long" ? tr.loT : tr.hiT) - tr.t)}）` : ""}。`);
    if (snap) { out.push(`平仓时盘面：${trendLine(snap)}。`); out.push(`指标：${indLine(snap, "5m")}。`); const ml = mktLine(snap); if (ml) out.push(ml + "。"); }
    cf.flags.forEach(([, t]) => out.push("⚠ " + t + "。"));
    return out.join("\n");
  }

  // ---------------- 平仓后走势（等平仓后够久了再算：看的时长 = 持仓时长，最少 1 小时、最多 24 小时） ----------------
  const afterWin = tr => Math.min(24 * 36e5, Math.max(36e5, tr.exit.t - tr.t));
  async function after(tr) {
    if (!tr.exit) return null;
    const W = afterWin(tr), end = tr.exit.t + W;
    if (Date.now() < end + 6e4) return { pending: true, readyAt: end };
    const span = end - tr.t, tf = span <= 4 * 864e5 ? "1m" : span <= 15 * 864e5 ? "5m" : "1h";
    const bars = await klRange(tf, tr.t - TFMS[tf], end, 10);
    if (!bars.length) return null;
    const d = dirOf(tr.side), e = tr.entry, X = tr.exit.px, plan = planOf(tr);
    const hold = bars.filter(b => b.t + TFMS[tf] > tr.t && b.t <= tr.exit.t), post = bars.filter(b => b.t > tr.exit.t && b.t <= end);
    const ext = (bs, ref) => { let fav = 0, adv = 0, favT = null; for (const b of bs) { const f = d > 0 ? b.h / ref - 1 : 1 - b.l / ref, a = d > 0 ? b.l / ref - 1 : 1 - b.h / ref; if (f > fav) { fav = f; favT = b.t; } if (a < adv) adv = a; } return { fav, adv, favT }; };
    const H = ext(hold, e), P = ext(post, X), endPx = post.length ? post.at(-1).c : NaN;
    // 按原计划：从开仓起只看原来的止盈止损，同一根 K 线里两个都碰到按先打止损算
    let planRes = null;
    if (plan.tp || plan.sl) {
      for (const b of bars) { if (b.t + TFMS[tf] <= tr.t) continue;
        const slHit = plan.sl && (d > 0 ? b.l <= plan.sl : b.h >= plan.sl), tpHit = plan.tp && (d > 0 ? b.h >= plan.tp : b.l <= plan.tp);
        if (slHit) { planRes = { hit: "止损", t: b.t, pct: d * (plan.sl / e - 1) }; break; }
        if (tpHit) { planRes = { hit: "止盈", t: b.t, pct: d * (plan.tp / e - 1) }; break; } }
      if (!planRes) planRes = { hit: null, t: end, pct: Number.isFinite(endPx) ? d * (endPx / e - 1) : NaN };
    }
    const at1h = bars.find(b => b.t >= tr.t + 36e5 - TFMS[tf]), m1h = at1h ? d * (at1h.c / e - 1) : NaN;
    const dir1h = !Number.isFinite(m1h) ? null : m1h > 0.001 ? "对" : m1h < -0.001 ? "错" : "平";
    const got = d * (X / e - 1);
    return { v: 1, W, tf, mfe: H.fav, mae: H.adv, mfeT: H.favT, postFav: P.fav, postAdv: P.adv, postEnd: Number.isFinite(endPx) ? d * (endPx / X - 1) : NaN, plan: planRes, m1h, dir1h, got, at: Date.now() };
  }
  function afterSummary(tr, a) {
    if (!a || a.pending) return "";
    const out = [`平仓后 ${dur(a.W)} 内：价格最多再朝原方向走了 ${pc(a.postFav)}，最多反向 ${pc(a.postAdv)}，到时比平仓价 ${pc(a.postEnd)}。`];
    if (a.postFav >= 0.005 && a.postFav > Math.abs(a.got)) out.push("卖早了：平仓后还有一大段。");
    else if (a.postAdv <= -0.005) out.push("平得及时：平仓后价格反向走了一段。");
    if (a.plan) out.push(a.plan.hit ? `如果完全按原计划不动：${dur(a.plan.t - tr.t)} 后打到${a.plan.hit}，结果 ${pc(a.plan.pct)}（实际 ${pc(a.got)}）。` : `如果完全按原计划不动：到 ${dur(a.plan.t - tr.t)} 时止盈止损都没碰到，当时 ${pc(a.plan.pct)}（实际 ${pc(a.got)}）。`);
    if (a.dir1h) out.push(`方向判断（开仓后 1 小时）：${a.dir1h}（${pc(a.m1h)}）。`);
    return out.join("\n");
  }

  // ---------------- 本机存储（IndexedDB）：快照带截图太大，不放 localStorage ----------------
  let dbP = null;
  const db = () => dbP || (dbP = new Promise((ok, no) => { try { const r = indexedDB.open("btc_review", 1); r.onupgradeneeded = () => r.result.createObjectStore("snap"); r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); } catch (e) { no(e); } }));
  const idbPut = (k, v) => db().then(d => new Promise(ok => { const tx = d.transaction("snap", "readwrite"); tx.objectStore("snap").put(v, k); tx.oncomplete = () => ok(true); tx.onerror = () => ok(false); })).catch(() => false);
  const idbGet = k => db().then(d => new Promise(ok => { const q = d.transaction("snap").objectStore("snap").get(k); q.onsuccess = () => ok(q.result || null); q.onerror = () => ok(null); })).catch(() => null);

  // ---------------- 云端：私有仓库 btc-records 的 reviews/（张主任在 zhang/reviews/），每单开仓、平仓各一个文件 ----------------
  const b64e = s => { const u = new TextEncoder().encode(s); let b = ""; for (let i = 0; i < u.length; i += 0x8000) b += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(b); };
  const b64d = s => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, "")), c => c.charCodeAt(0)));
  const snapPath = (acct, id, kind) => `${acct === "main" ? "" : acct + "/"}reviews/${id}-${kind}.json`;
  async function cloudPut(token, path, obj) {
    const url = `https://api.github.com/repos/${REC_REPO}/contents/${path}`, H = { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" };
    const put = sha => fetch(url, { method: "PUT", headers: H, body: JSON.stringify({ message: "复盘快照", content: b64e(JSON.stringify(obj)), ...(sha ? { sha } : {}) }) });
    let r = await put();
    if (r.status === 422 || r.status === 409) { const g = await fetch(url, { headers: H, cache: "no-store" }); if (g.ok) r = await put((await g.json()).sha); }
    return r.ok;
  }
  async function cloudGet(token, path) {
    const r = await fetch(`https://api.github.com/repos/${REC_REPO}/contents/${path}`, { headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github.raw+json" }, cache: "no-store" });
    if (!r.ok) return null;
    return r.json();
  }
  async function loadSnap(token, acct, id, kind) {
    const k = `${acct}:${id}:${kind}`, loc = await idbGet(k);
    if (loc) return loc;
    if (!token) return null;
    const s = await cloudGet(token, snapPath(acct, id, kind)).catch(() => null);
    if (s) idbPut(k, s);
    return s;
  }

  return { TAGS, TAG_NAME, CONF, CONF_NAME, CLOSE_WHY, CLOSE_NAME, TFN, TFMS, SNAP_TF, REC_REPO, readings, session, capture, verify, openFlags, closeFlags, planOf,
    openSummary, addSummary, closeSummary, after, afterSummary, afterWin, idbPut, idbGet, cloudPut, cloudGet, snapPath, loadSnap, unpack, klRange, b64d, dur, pc, sg, p1,
    ind: { sma, ema, rsi, kdj, boll, macd } };
})();
