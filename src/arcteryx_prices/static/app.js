"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  list: $("watch-list"), alerts: $("alert-list"), dialog: $("watch-dialog"),
  form: $("watch-form"), add: $("add-watch"), save: $("save-watch"),
  connection: $("connection-message"), feedback: $("feedback"),
};
let state = { watches: [], alerts: [], csrf_token: null };
let loaded = false;
let connected = false;
let market = "all";
let sequence = 0;
let feedbackTimer;
let discoveryBusy = false;
let discoveredURL = null;
let variants = [];
const pending = new Set();
const expandedHistory = new Set();
const ruleNames = { target: "达到目标价", new_low: "已观测新低", restock: "重新有货" };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function money(minor, currency) {
  if (minor === null || minor === undefined || !Number.isFinite(Number(minor))) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: currency === "CAD" ? "CAD" : "USD",
    currencyDisplay: "narrowSymbol", minimumFractionDigits: 2,
  }).format(Number(minor) / 100);
}

function dateTime(value) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "尚无记录";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function safeProductURL(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    if (!["arcteryx.com", "outlet.arcteryx.com"].includes(url.hostname)) return null;
    if (!/^\/(us\/en|ca\/(en|fr))\/shop\/.+/.test(url.pathname)) return null;
    return url.href;
  } catch (_) {
    return null;
  }
}

function externalLink(label, url, className) {
  const href = safeProductURL(url);
  if (!href) return node("span", className, label);
  const link = node("a", className, label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function hasCurrentPrice(watch) {
  return connected && !watch.stale && watch.current_price_minor !== null &&
    watch.current_price_minor !== undefined &&
    (!watch.availability || watch.availability === "in_stock");
}

function statusInfo(watch) {
  if (!watch.enabled) return { text: "已暂停", tone: "neutral" };
  if (!connected) return { text: "连接中断", tone: "warning" };
  if (watch.checking || pending.has(`check-${watch.id}`)) return { text: "正在检查", tone: "neutral" };
  const statuses = {
    network_error: "连接失败", http_error: "来源请求失败", parse_error: "页面待适配",
    identity_mismatch: "变体待确认", currency_mismatch: "币种不一致",
    availability_unknown: "库存待确认", verification_failed: "复核未通过",
    confirmation_failed: "复核未通过", confirmation_changed: "复核报价变化",
    verification_changed: "复核报价变化", price_error: "价格待确认",
    invalid_url: "商品链接待确认", invalid_watch: "关注信息待确认",
    redirect_blocked: "商品链接跳转异常", content_type_error: "页面暂不可读",
    content_encoding_error: "页面暂不可读", response_too_large: "页面暂不可读",
    document_limit: "页面暂不可读", condition_unknown: "商品成色待确认",
    unsupported_offer: "报价条件待确认", ambiguous_offer: "报价存在歧义",
    stale_offer: "来源报价已失效", storage_error: "记录保存失败",
    configuration_changed: "设置已改变，待重试", lease_lost: "检查已被重新安排",
    no_verifiable_variants: "变体待确认", variant_limit: "变体待确认",
    mismatch: "报价待确认", error: "检查失败", never_checked: "等待首次检查",
    pending: "等待首次检查", checking: "正在检查", unsupported: "页面待适配",
  };
  if (statuses[watch.status]) return {
    text: statuses[watch.status],
    tone: ["never_checked", "pending", "checking"].includes(watch.status) ? "neutral" : "warning",
  };
  if (!watch.last_success_at) return { text: "等待有效报价", tone: "neutral" };
  if (watch.stale) return { text: "报价已过期", tone: "warning" };
  if (watch.availability === "out_of_stock") return { text: "当前缺货", tone: "neutral" };
  if (watch.availability === "unknown") return { text: "库存待确认", tone: "warning" };
  if (hasCurrentPrice(watch)) return { text: "有效报价", tone: "" };
  return { text: "等待有效报价", tone: "neutral" };
}

function showFeedback(message, error = false) {
  clearTimeout(feedbackTimer);
  ui.feedback.textContent = message;
  ui.feedback.className = `feedback${error ? " error" : ""}`;
  ui.feedback.hidden = false;
  feedbackTimer = setTimeout(() => { ui.feedback.hidden = true; }, error ? 12000 : 7000);
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    if (!state.csrf_token) throw new Error("连接尚未准备好，请稍后重试。");
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf_token;
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers, cache: "no-store", credentials: "same-origin" });
  } catch (_) {
    throw new Error("无法连接监控服务。请确认服务仍在运行。");
  }
  let body;
  try { body = await response.json(); } catch (_) {
    throw new Error("监控服务返回了无法读取的响应，请稍后重试。");
  }
  if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : `请求失败（${response.status}）`);
  return body;
}

async function refresh() {
  const current = ++sequence;
  try {
    const result = await api("/api/state");
    if (current !== sequence) return;
    const changed = !loaded || !connected ||
      JSON.stringify(result.watches || []) !== JSON.stringify(state.watches) ||
      JSON.stringify(result.alerts || []) !== JSON.stringify(state.alerts) ||
      result.csrf_token !== state.csrf_token;
    state = {
      watches: Array.isArray(result.watches) ? result.watches : [],
      alerts: Array.isArray(result.alerts) ? result.alerts : [],
      csrf_token: result.csrf_token || null,
    };
    loaded = true;
    connected = true;
    ui.connection.hidden = true;
    $("sync-dot").classList.remove("error");
    $("sync-label").textContent = `状态已同步 · ${dateTime(new Date().toISOString())}`;
    ui.add.disabled = !state.csrf_token;
    updateForm();
    if (changed) render();
  } catch (error) {
    if (current !== sequence) return;
    connected = false;
    ui.connection.textContent = error.message;
    ui.connection.hidden = false;
    $("sync-dot").classList.add("error");
    $("sync-label").textContent = "状态同步失败";
    if (!loaded) {
      ui.list.replaceChildren(node("div", "loading-state", "暂时无法读取关注清单。服务恢复后会自动重试。"));
      ui.alerts.replaceChildren(node("div", "alert-empty", "暂时无法读取提醒记录。"));
    } else render();
  } finally {
    ui.list.setAttribute("aria-busy", "false");
  }
}

function metric(label, value, detail, className = "") {
  const box = node("div", `metric ${className}`.trim());
  box.append(node("div", "metric-label", label),
    node("div", `metric-value${value === "—" ? " unavailable" : ""}`, value),
    node("div", "metric-detail", detail));
  return box;
}

function ruleDescription(watch) {
  const price = watch.target_price_minor;
  if (watch.rule === "target") return `目标价 ≤ ${money(price, watch.currency)} ${watch.currency}`;
  if (watch.rule === "restock") return price ? `补货且价格 ≤ ${money(price, watch.currency)} ${watch.currency}` : "从明确缺货变为有货时提醒";
  return "低于此前已观测最低价时提醒";
}

function chart(watch) {
  const stats = watch.metrics?.average_daily_low_90d || {};
  const points = (Array.isArray(stats.daily_minima) ? stats.daily_minima : [])
    .filter((point) => /^\d{4}-\d{2}-\d{2}$/.test(point.day) && Number.isFinite(point.price_minor))
    .sort((a, b) => a.day.localeCompare(b.day));
  const details = node("details", "history-details");
  details.open = expandedHistory.has(String(watch.id));
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) expandedHistory.add(String(watch.id));
    else expandedHistory.delete(String(watch.id));
  });
  details.append(node("summary", "", "查看近 90 天的日最低价记录"));
  const content = node("div", "history-content");
  if (!points.length) {
    content.append(node("p", "chart-empty", "还没有符合条件的可购价格。首次成功检查后，记录会出现在这里。"));
    details.append(content);
    return details;
  }
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 650 100");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "history-chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `近90天内${points.length}天的最低可购价，完整日期与价格见下方表格`);
  const values = points.map((point) => point.price_minor);
  const low = Math.min(...values), high = Math.max(...values);
  const dayNumber = (day) => Date.parse(`${day}T00:00:00Z`) / 86400000;
  const first = dayNumber(points[0].day), last = dayNumber(points[points.length - 1].day);
  const plotted = points.map((point) => ({
    ...point, x: first === last ? 325 : 10 + (dayNumber(point.day) - first) / (last - first) * 630,
    y: high === low ? 50 : 85 - (point.price_minor - low) / (high - low) * 70,
  }));
  // Separate paths across missing dates; the chart must not imply continuous coverage.
  let segment = [];
  const appendSegment = () => {
    if (segment.length > 1) {
      const line = document.createElementNS(ns, "polyline");
      line.setAttribute("class", "chart-line");
      line.setAttribute("points", segment.map((point) => `${point.x},${point.y}`).join(" "));
      svg.append(line);
    }
    segment = [];
  };
  for (let i = 0; i < plotted.length; i++) {
    if (i > 0 && dayNumber(plotted[i].day) - dayNumber(plotted[i - 1].day) !== 1) appendSegment();
    segment.push(plotted[i]);
  }
  appendSegment();
  for (const point of plotted) {
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("cx", String(point.x));
    dot.setAttribute("cy", String(point.y));
    dot.setAttribute("r", "2.6");
    dot.setAttribute("class", "chart-point");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${point.day} · ${money(point.price_minor, watch.currency)} ${watch.currency}`;
    dot.append(title);
    svg.append(dot);
  }
  content.append(svg);
  const labels = node("div", "chart-labels");
  labels.append(node("span", "", points[0].day), node("span", "", points[points.length - 1].day));
  content.append(labels, node("p", "chart-caption", `已观测 ${points.length}/90 天 · 日期按 UTC 统计；空白日期不延续上一报价。`));
  const scroll = node("div", "history-scroll");
  const table = node("table", "history-table");
  const head = node("thead"), row = node("tr");
  const dateHead = node("th", "", "日期（UTC）"), priceHead = node("th", "", `当日最低价 · ${watch.currency}`);
  dateHead.scope = "col"; priceHead.scope = "col";
  row.append(dateHead, priceHead); head.append(row); table.append(head);
  const body = node("tbody");
  for (const point of [...points].reverse()) {
    const line = node("tr");
    line.append(node("td", "", point.day), node("td", "", money(point.price_minor, watch.currency)));
    body.append(line);
  }
  table.append(body); scroll.append(table); content.append(scroll); details.append(content);
  return details;
}

function watchCard(watch) {
  const card = node("article", "watch-card");
  const top = node("div", "watch-topline"), identity = node("div");
  const heading = node("h3", "watch-title");
  heading.append(externalLink(watch.label || watch.sku || "未命名关注", watch.url));
  const variant = node("div", "watch-variant");
  variant.append(node("span", "", watch.color), node("span", "", `尺码 ${watch.size}`));
  identity.append(heading, variant, node("div", "watch-sku", `SKU ${watch.sku}`));
  const badges = node("div", "watch-badges"), status = statusInfo(watch);
  badges.append(node("span", "market-badge", `${watch.market} · ${watch.currency}`),
    node("span", `status-badge ${status.tone}`.trim(), status.text));
  top.append(identity, badges);
  const stats = watch.metrics || {}, coverage365 = stats.coverage_365d || {}, average = stats.average_daily_low_90d || {};
  const prices = node("div", "price-grid");
  prices.append(
    metric("当前可购价", hasCurrentPrice(watch) ? money(watch.current_price_minor, watch.currency) : "—",
      hasCurrentPrice(watch) ? `${watch.currency} · 不含税运` : "等待有效的现货报价", "metric-current"),
    metric("已观测历史最低", money(stats.observed_all_time_min?.price_minor, watch.currency),
      stats.history?.first_observed_at ? `自 ${String(stats.history.first_observed_at).slice(0, 10)} 开始记录` : "尚无价格记录"),
    metric("365 天最低", money(stats.lowest_365d?.price_minor, watch.currency),
      `已覆盖 ${coverage365.observed_days || 0}/365 天`),
    metric("90 天日最低均价", money(average.display_minor, watch.currency),
      `已覆盖 ${average.observed_days || 0}/90 天${average.complete_day_coverage ? "" : " · 数据未满"}`),
  );
  const bottom = node("div", "watch-bottom"), health = node("div", "watch-health");
  health.append(node("div", "", ruleDescription(watch)),
    node("div", watch.stale && watch.last_success_at ? "health-warning" : "", `最近成功检查：${dateTime(watch.last_success_at)}`));
  if (watch.last_checked_at && watch.last_checked_at !== watch.last_success_at) {
    health.append(node("div", "", `最近检查：${dateTime(watch.last_checked_at)}`));
  }
  const actions = node("div", "watch-actions");
  const check = node("button", "compact-button", pending.has(`check-${watch.id}`) ? "正在检查…" : "立即检查");
  check.type = "button";
  check.disabled = !state.csrf_token || !watch.enabled || watch.checking || pending.has(`check-${watch.id}`);
  check.setAttribute("aria-label", `立即检查 ${watch.label || watch.sku}`);
  check.addEventListener("click", () => checkWatch(watch));
  const toggle = node("button", "compact-button subtle", watch.enabled ? "暂停" : "恢复");
  toggle.type = "button";
  toggle.disabled = !state.csrf_token || pending.has(`toggle-${watch.id}`);
  toggle.setAttribute("aria-label", `${watch.enabled ? "暂停" : "恢复"}监控 ${watch.label || watch.sku}`);
  toggle.addEventListener("click", () => toggleWatch(watch));
  actions.append(check, toggle); bottom.append(health, actions);
  card.append(top, prices, bottom, chart(watch));
  return card;
}

function openForm() {
  $("form-error").hidden = true;
  if (!ui.dialog.open) ui.dialog.showModal();
  $("watch-url").focus();
}

function renderWatches() {
  const watches = state.watches.filter((watch) => market === "all" || watch.market === market);
  $("watch-count").textContent = String(watches.length);
  if (!watches.length) {
    const empty = node("div", "empty-state");
    const icon = node("span", "empty-icon", "↗");
    icon.setAttribute("aria-hidden", "true");
    empty.append(icon, node("h3", "", market === "all" ? "从你想要的那件装备开始" : `还没有${market === "US" ? "美国" : "加拿大"}商品的关注记录`),
      node("p", "", "添加商品链接和具体颜色、尺码。监控成功后，当前报价和价格历史会自动出现在这里。"));
    const button = node("button", "button button-secondary", "＋ 添加第一个关注");
    button.type = "button"; button.disabled = !state.csrf_token; button.addEventListener("click", openForm);
    empty.append(button); ui.list.replaceChildren(empty);
    return;
  }
  ui.list.replaceChildren(...watches.map(watchCard));
}

function renderAlerts() {
  const alerts = state.alerts.filter((alert) => {
    const watch = state.watches.find((item) => item.id === alert.watch_id);
    return market === "all" || (alert.market || watch?.market) === market;
  });
  if (!alerts.length) {
    ui.alerts.replaceChildren(node("div", "alert-empty", "还没有提醒。符合你设置的条件并通过报价复核后，会在这里留下记录。"));
    return;
  }
  const items = [...alerts].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))).map((alert) => {
    const watch = state.watches.find((item) => item.id === alert.watch_id);
    const item = node("article", "alert-item"), symbol = node("span", "alert-symbol", "↓");
    symbol.setAttribute("aria-hidden", "true");
    const copy = node("div", "alert-copy");
    const label = alert.label || watch?.label || watch?.sku || "关注的商品";
    const currency = alert.currency || watch?.currency;
    copy.append(node("h3", "alert-title", label), node("div", "alert-meta", `${ruleNames[alert.rule] || "价格提醒"} · ${dateTime(alert.created_at)} · ${currency || ""}`));
    item.append(symbol, copy, node("div", "alert-price", money(alert.price_minor, currency)),
      externalLink("查看商品 ↗", alert.url || watch?.url, "alert-link"));
    return item;
  });
  ui.alerts.replaceChildren(...items);
}

function render() {
  $("active-count").textContent = String(state.watches.filter((watch) => watch.enabled).length);
  $("fresh-count").textContent = String(state.watches.filter(hasCurrentPrice).length);
  $("alert-count").textContent = String(state.alerts.length);
  renderWatches(); renderAlerts();
}

async function checkWatch(watch) {
  const key = `check-${watch.id}`;
  if (pending.has(key)) return;
  pending.add(key); renderWatches();
  try {
    await api(`/api/watches/${encodeURIComponent(watch.id)}/check`, { method: "POST", body: "{}" });
    await refresh();
    const current = state.watches.find((item) => item.id === watch.id);
    const info = current ? statusInfo({ ...current, id: null }) : null;
    showFeedback(info ? `检查完成：${info.text}。` : "检查请求已完成。", info?.tone === "warning" || info?.tone === "error");
  } catch (error) { showFeedback(error.message, true); }
  finally { pending.delete(key); renderWatches(); }
}

async function toggleWatch(watch) {
  const key = `toggle-${watch.id}`;
  if (pending.has(key)) return;
  pending.add(key); renderWatches();
  try {
    await api(`/api/watches/${encodeURIComponent(watch.id)}`, { method: "PATCH", body: JSON.stringify({ enabled: !watch.enabled }) });
    showFeedback(watch.enabled ? "已暂停这个关注项。" : "已恢复监控，将按设置的间隔检查。");
    await refresh();
  } catch (error) { showFeedback(error.message, true); }
  finally { pending.delete(key); renderWatches(); }
}

function updateForm() {
  const value = $("watch-url").value.trim(), url = safeProductURL(value);
  const selectedMarket = url ? new URL(url).pathname.split("/")[1].toUpperCase() : null;
  const currency = selectedMarket === "US" ? "USD" : selectedMarket === "CA" ? "CAD" : "USD / CAD";
  $("market-hint").textContent = selectedMarket ? `${selectedMarket === "US" ? "美国" : "加拿大"}商品，价格以 ${currency} 统计。` : value ? "请填写美国或加拿大官网、Outlet 的具体商品链接。" : "市场与币种将根据商品链接确定。";
  $("market-hint").classList.toggle("invalid", Boolean(value && !url));
  $("target-currency").textContent = currency;
  $("discover-variants").disabled = !url || !state.csrf_token || discoveryBusy;
  const rule = $("watch-rule").value;
  $("target-field").hidden = rule === "new_low";
  $("watch-target").required = rule === "target";
  $("watch-target").disabled = rule === "new_low";
  $("target-label").textContent = rule === "restock" ? "补货时的价格上限（可选）" : "目标价";
  $("target-hint").textContent = rule === "restock" ? "留空则只关注补货；填写后，仅在有货且不高于此价格时提醒。" : "商品单价，不含税费和运费。价格达到或低于目标时提醒。";
  $("rule-hint").hidden = rule === "target";
  $("rule-hint").textContent = rule === "new_low" ? "从首次有效观测开始积累历史。首次报价作为基线，之后出现严格更低的可购价格时提醒。" : "先确认目标变体缺货，再确认恢复现货时提醒；页面读取失败不会被当作缺货。";
}

function clearDiscovery() {
  discoveredURL = null;
  variants = [];
  $("variant-field").hidden = true;
  $("variant-select").replaceChildren(node("option", "", "请选择一个变体"));
  $("variant-select").firstChild.value = "";
  $("discovery-status").textContent = "";
  $("discovery-status").classList.remove("error");
}

async function discoverVariants() {
  const url = safeProductURL($("watch-url").value.trim());
  if (!url || discoveryBusy) return;
  discoveryBusy = true;
  $("discover-variants").textContent = "正在读取…";
  $("discovery-status").textContent = "正在确认商品变体，请稍候。";
  $("discovery-status").classList.remove("error");
  updateForm();
  try {
    const result = await api("/api/discover", { method: "POST", body: JSON.stringify({ url }) });
    if (safeProductURL($("watch-url").value.trim()) !== url) return;
    const items = (Array.isArray(result.variants) ? result.variants : []).filter((variant) =>
      [variant.sku, variant.color, variant.size].every((value) => typeof value === "string" && value.trim()));
    if (!items.length) throw new Error("页面未提供可确认的颜色、尺码和 SKU。可以手动填写，或稍后重试。");
    discoveredURL = url;
    variants = items;
    const select = $("variant-select");
    select.replaceChildren();
    const placeholder = node("option", "", "请选择一个变体");
    placeholder.value = ""; select.append(placeholder);
    variants.forEach((variant, index) => {
      const status = { in_stock: "有货", out_of_stock: "缺货", unknown: "库存待确认" }[variant.availability] || "库存待确认";
      const price = variant.price_minor ? ` · ${money(variant.price_minor, result.currency)} ${result.currency}` : "";
      const option = node("option", "", `${variant.color} · ${variant.size}${price} · ${status}`);
      option.value = String(index); select.append(option);
    });
    $("variant-field").hidden = false;
    $("discovery-status").textContent = `已找到 ${variants.length} 个明确变体。`;
    if (!$("watch-label").value && typeof result.label === "string") $("watch-label").value = result.label;
    $("watch-sku").value = ""; $("watch-color").value = ""; $("watch-size").value = "";
    $("manual-fields").open = false;
    select.focus();
  } catch (error) {
    if (safeProductURL($("watch-url").value.trim()) !== url) return;
    $("discovery-status").textContent = error.message;
    $("discovery-status").classList.add("error");
  } finally {
    discoveryBusy = false;
    $("discover-variants").textContent = "读取颜色和尺码 ↗";
    updateForm();
  }
}

function priceToMinor(text) {
  if (!text.trim()) return null;
  if (!/^\d+(?:\.\d{1,2})?$/.test(text.trim())) throw new Error("价格请填写最多两位小数的正数。");
  const [major, minor = ""] = text.trim().split(".");
  const value = Number(major) * 100 + Number(minor.padEnd(2, "0"));
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error("目标价需要大于 0，且在有效金额范围内。");
  return value;
}

ui.add.addEventListener("click", openForm);
$("close-dialog").addEventListener("click", () => ui.dialog.close());
ui.dialog.addEventListener("click", (event) => { if (event.target === ui.dialog) {
  const rect = ui.dialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) ui.dialog.close();
} });
$("watch-url").addEventListener("input", () => {
  if (discoveredURL && safeProductURL($("watch-url").value.trim()) !== discoveredURL) {
    clearDiscovery();
    $("watch-sku").value = ""; $("watch-color").value = ""; $("watch-size").value = "";
  }
  updateForm();
});
$("watch-rule").addEventListener("change", updateForm);
$("discover-variants").addEventListener("click", discoverVariants);
$("variant-select").addEventListener("change", () => {
  const value = $("variant-select").value;
  const variant = value === "" ? null : variants[Number(value)];
  $("watch-sku").value = variant?.sku || "";
  $("watch-color").value = variant?.color || "";
  $("watch-size").value = variant?.size || "";
});
for (const id of ["watch-sku", "watch-color", "watch-size"]) {
  $(id).addEventListener("invalid", () => { $("manual-fields").open = true; });
}
document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
  market = button.dataset.market;
  document.querySelectorAll(".filter").forEach((item) => {
    const selected = item.dataset.market === market;
    item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected));
  });
  if (loaded) { renderWatches(); renderAlerts(); }
}));
ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (ui.save.disabled) return;
  $("form-error").hidden = true;
  ui.save.disabled = true;
  ui.save.textContent = "正在保存…";
  try {
    const url = safeProductURL($("watch-url").value.trim());
    if (!url) throw new Error("目前支持 Arc'teryx 官网和 Outlet 的美国、加拿大具体商品链接。");
    const rule = $("watch-rule").value;
    const target = rule === "new_low" ? null : priceToMinor($("watch-target").value);
    if (rule === "target" && target === null) throw new Error("请设置一个目标价。");
    const payload = {
      url, sku: $("watch-sku").value.trim(), color: $("watch-color").value.trim(),
      size: $("watch-size").value.trim(), rule, target_price_minor: target,
      interval_minutes: Number($("watch-interval").value),
    };
    const label = $("watch-label").value.trim();
    if (label) payload.label = label;
    await api("/api/watches", { method: "POST", body: JSON.stringify(payload) });
    ui.dialog.close(); ui.form.reset(); clearDiscovery(); $("manual-fields").open = false; updateForm();
    showFeedback("已添加关注。首次有效检查后，将开始积累价格历史。");
    await refresh();
  } catch (error) {
    $("form-error").textContent = error.message;
    $("form-error").hidden = false;
  } finally {
    ui.save.disabled = false;
    ui.save.textContent = "开始关注 ↗";
  }
});

updateForm();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
