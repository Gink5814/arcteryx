"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  list: $("watch-list"), alerts: $("alert-list"), dialog: $("watch-dialog"),
  form: $("watch-form"), add: $("add-watch"), save: $("save-watch"),
  connection: $("connection-message"), feedback: $("feedback"),
};
let state = { watches: [], alerts: [], historical_evidence: [], supported_retailers: [], csrf_token: null };
let loaded = false;
let connected = false;
let sequence = 0;
let feedbackTimer;
let discoveryBusy = false;
let discoveredURL = null;
let variants = [];
let activeView = "overview";
let searchTerm = "";
let statusFilter = "all";
let retailerFilter = "all";
let sortMode = "recent";
let selectedHistoryId = null;
let settingsWatchId = null;
let savingWatch = false;
let savingSettings = false;
let historySignature = null;
let historyOptionsSignature = null;
let alertsSignature = null;
let retailerOptionsSignature = null;
let evidenceSignature = null;
const pending = new Set();
const expandedHistory = new Set();
const cardCache = new Map();
const alertCache = new Map();
const evidenceHosts = new Set([
  "slickdeals.net", "www.slickdeals.net", "gearjunkie.com",
  "www.switchbacktravel.com", "www.outdoorgearlab.com", "web.archive.org",
  "arcteryx.com", "outlet.arcteryx.com", "www.rei.com", "rei.com",
  "www.backcountry.com", "backcountry.com",
]);
const ruleNames = { target: "达到目标价", new_low: "已观测新低", restock: "重新有货" };
const viewLabels = {
  overview: ["关注清单", "盯住具体的颜色与尺码，让好价格有迹可循。"],
  history: ["价格记录", "查看真实观测记录，了解每个价格背后的数据覆盖。"],
  alerts: ["提醒中心", "每条提醒都经过价格与库存复核，保留触发时的记录。"],
  guide: ["购买指南", "从用途、尺码到购买渠道，做好美国购物前的功课。"],
};

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = String(value);
}

function syncWriteControls() {
  const canWrite = connected && Boolean(state.csrf_token);
  ui.add.disabled = !canWrite;
  ui.save.disabled = savingWatch || !canWrite;
  if ($("save-settings")) $("save-settings").disabled = savingSettings || !canWrite;
}

function signature(value) {
  // as_of is the query clock, not a change in price/history. Including it would
  // recreate focused cards every poll despite unchanged monitoring evidence.
  return JSON.stringify(value, (key, item) => key === "as_of" ? undefined : item);
}

function preserveWatchFocus(callback) {
  const before = document.activeElement;
  const control = before?.closest?.("[data-watch-action]");
  const remembered = control ? {
    id: control.dataset.watchId, action: control.dataset.watchAction,
  } : null;
  callback();
  if (!remembered || before?.isConnected || ![document.body, null].includes(document.activeElement)) return;
  const replacement = [...ui.list.querySelectorAll("[data-watch-action]")].find((element) =>
    element.dataset.watchId === remembered.id && element.dataset.watchAction === remembered.action);
  if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
}

function reconcile(container, children) {
  children.forEach((child, index) => {
    if (container.children[index] !== child) container.insertBefore(child, container.children[index] || null);
  });
  while (container.children.length > children.length) container.lastElementChild.remove();
}

function setView(value, writeHash = true) {
  activeView = Object.hasOwn(viewLabels, value) ? value : "overview";
  document.querySelectorAll(".view-panel").forEach((panel) => {
    const visible = panel.id === `view-${activeView}`;
    panel.hidden = !visible; panel.classList.toggle("active", visible);
  });
  document.querySelectorAll("button[data-view]").forEach((button) => {
    const selected = button.dataset.view === activeView;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  setText("view-title", viewLabels[activeView][0]);
  setText("view-subtitle", viewLabels[activeView][1]);
  document.title = `${viewLabels[activeView][0]} · North 美国价格观察`;
  if (writeHash && location.hash !== `#${activeView}`) history.pushState(null, "", `#${activeView}`);
  if (loaded && activeView === "history") renderHistory();
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function money(minor, currency) {
  if (currency && currency !== "USD") return "—";
  if (minor === null || minor === undefined || !Number.isFinite(Number(minor))) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD",
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

function productLocation(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    if (["arcteryx.com", "outlet.arcteryx.com"].includes(url.hostname)) {
      if (!/^\/us\/en\/shop\/.+/.test(url.pathname)) return null;
      return { url: url.href, market: "US", currency: "USD", retailer: "Arc'teryx 官网 / Outlet" };
    }
    if (["rei.com", "www.rei.com"].includes(url.hostname) && /^\/product\/[A-Za-z0-9]+\/arcteryx-[A-Za-z0-9-]+\/?$/.test(url.pathname)) {
      url.hostname = "www.rei.com";
      return { url: url.href, market: "US", currency: "USD", retailer: "REI Co-op" };
    }
    return null;
  } catch (_) {
    return null;
  }
}

function safeProductURL(value) {
  return productLocation(value)?.url || null;
}

function retailerName(watch) {
  if (watch.retailer_name === "Arc'teryx official") return "Arc'teryx 官网 / Outlet";
  return watch.retailer_name || productLocation(watch.url)?.retailer || "来源待确认";
}

function retailerKey(watch) {
  return watch.retailer_key || (productLocation(watch.url)?.retailer === "REI Co-op" ? "rei" : "arcteryx-official");
}

function sourceLink(label, value, className) {
  // Match the history importer source-host registry. Accepted hosts permit
  // citations; they do not by themselves establish price or variant identity.
  let url;
  try {
    url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port ||
        !evidenceHosts.has(url.hostname)) return node("span", className, label);
  } catch (_) { return node("span", className, label); }
  const link = node("a", className, label);
  link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
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

function retryDeadline(watch, now = Date.now()) {
  const times = [watch.retry_not_before, watch.cooldown_until, watch.next_retry_at]
    .filter(value => typeof value === "string")
    .map(value => Date.parse(value))
    .filter(value => Number.isFinite(value) && value > now);
  return times.length ? Math.max(...times) : null;
}

function retryDateTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short",
  }).format(new Date(timestamp));
}

function waitingMessage(watch, prefix = "已暂缓检查") {
  const deadline = retryDeadline(watch);
  return deadline ? `${prefix}，可在 ${retryDateTime(deadline)} 后再检查。` : `${prefix}，等待下一次状态同步。`;
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
    rate_limited: "来源暂缓检查", backoff: "等待重试",
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

function usRecords(records) {
  return Array.isArray(records) ? records.filter(record => record.market === "US" && record.currency === "USD") : [];
}

async function refresh() {
  const current = ++sequence;
  try {
    const result = await api("/api/state");
    // Old API caches may still contain other markets; keep them outside every view.
    result.watches = usRecords(result.watches).filter(watch => safeProductURL(watch.url));
    const usWatchIds = new Set(result.watches.map(watch => watch.id));
    // Older alert rows had currency but no market. A known US watch establishes
    // their scope; the currency alone is insufficient to infer a market.
    result.alerts = (Array.isArray(result.alerts) ? result.alerts : []).filter(alert =>
      alert.currency === "USD" && (alert.market === "US" ||
        (alert.market === undefined && usWatchIds.has(alert.watch_id))))
      .map(alert => ({ ...alert, market: "US" }));
    result.historical_evidence = usRecords(result.historical_evidence);
    result.supported_retailers = (Array.isArray(result.supported_retailers) ? result.supported_retailers : [])
      .filter(source => Array.isArray(source.markets) && source.markets.includes("US"))
      .map(source => ({ ...source, markets: ["US"] }));
    if (current !== sequence) return;
    const changed = !loaded || !connected ||
      signature(result.watches || []) !== signature(state.watches) ||
      signature(result.alerts || []) !== signature(state.alerts) ||
      signature(result.historical_evidence || []) !== signature(state.historical_evidence) ||
      signature(result.supported_retailers || []) !== signature(state.supported_retailers) ||
      result.csrf_token !== state.csrf_token;
    state = {
      watches: Array.isArray(result.watches) ? result.watches : [],
      alerts: Array.isArray(result.alerts) ? result.alerts : [],
      historical_evidence: Array.isArray(result.historical_evidence) ? result.historical_evidence : [],
      supported_retailers: Array.isArray(result.supported_retailers) ? result.supported_retailers : [],
      csrf_token: result.csrf_token || null,
    };
    loaded = true;
    connected = true;
    ui.connection.hidden = true;
    $("sync-dot").classList.remove("error");
    $("sync-label").textContent = `状态已同步 · ${dateTime(new Date().toISOString())}`;
    syncWriteControls();
    updateForm();
    if (changed) render();
    else renderWatches(); // A retry deadline can expire while the API payload stays identical.
  } catch (error) {
    if (current !== sequence) return;
    connected = false;
    state.csrf_token = null;
    syncWriteControls();
    updateForm();
    ui.connection.textContent = error.message;
    ui.connection.hidden = false;
    $("sync-dot").classList.add("error");
    $("sync-label").textContent = "状态同步失败";
    if (!loaded) {
      ui.list.replaceChildren(node("div", "loading-state", "暂时无法读取关注清单。服务恢复后会自动重试。"));
      ui.alerts.replaceChildren(node("div", "alert-empty", "暂时无法读取提醒记录。"));
      if ($("history-content")) $("history-content").replaceChildren(node("div", "empty-state", "暂时无法读取价格记录。服务恢复后会自动重试。"));
      if ($("historical-evidence-list")) $("historical-evidence-list").replaceChildren(node("p", "evidence-empty", "暂时无法读取历史来源。服务恢复后会自动重试。"));
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

function chart(watch, standalone = false) {
  const stats = watch.metrics?.average_daily_low_90d || {};
  const points = (Array.isArray(stats.daily_minima) ? stats.daily_minima : [])
    .filter((point) => /^\d{4}-\d{2}-\d{2}$/.test(point.day) && Number.isFinite(point.price_minor))
    .sort((a, b) => a.day.localeCompare(b.day));
  const details = node("details", "history-details");
  details.open = standalone || expandedHistory.has(String(watch.id));
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) expandedHistory.add(String(watch.id));
    else expandedHistory.delete(String(watch.id));
  });
  const summary = node("summary", "", "查看近 90 天的日最低价记录");
  setActionIdentity(summary, watch, "history");
  details.append(summary);
  const content = node("div", "history-content");
  if (!points.length) {
    content.append(node("p", "chart-empty", "还没有符合条件的可购价格。首次成功检查后，记录会出现在这里。"));
    details.append(content);
    return standalone ? content : details;
  }
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 650 100");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("class", points.length === 1 ? "history-chart single-observation" : "history-chart");
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
    dot.setAttribute("stroke", "none");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${point.day} · ${money(point.price_minor, watch.currency)} ${watch.currency}`;
    dot.append(title);
    svg.append(dot);
  }
  content.append(svg);
  const labels = node("div", "chart-labels");
  labels.append(node("span", "", points[0].day), node("span", "", points[points.length - 1].day));
  content.append(labels, node("p", "chart-caption", `已观测 ${points.length}/90 天 · 日期按 UTC 统计；空白日期不延续上一报价。`));
  if (points.length === 1) content.append(node("p", "chart-empty", "目前只有 1 天真实观测，尚不能形成价格趋势。后续记录会逐日积累。"));
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
  return standalone ? content : details;
}

function priceMetrics(watch) {
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
  return prices;
}

function setActionIdentity(element, watch, action) {
  element.dataset.watchId = String(watch.id);
  element.dataset.watchAction = action;
}

function watchCard(watch) {
  const card = node("article", "watch-card");
  card.dataset.watchId = String(watch.id);
  const top = node("div", "watch-topline"), identity = node("div");
  const heading = node("h3", "watch-title");
  heading.append(externalLink(watch.label || watch.sku || "未命名关注", watch.url));
  const variant = node("div", "watch-variant");
  variant.append(node("span", "", watch.color), node("span", "", `尺码 ${watch.size}`));
  identity.append(node("div", "watch-retailer", retailerName(watch)), heading, variant, node("div", "watch-sku", `SKU ${watch.sku}`));
  const badges = node("div", "watch-badges"), status = statusInfo(watch);
  badges.append(node("span", "market-badge", `${watch.market} · ${watch.currency}`),
    node("span", `status-badge ${status.tone}`.trim(), status.text));
  top.append(identity, badges);
  const prices = priceMetrics(watch);
  const bottom = node("div", "watch-bottom"), health = node("div", "watch-health");
  health.append(node("div", "", ruleDescription(watch)),
    node("div", watch.stale && watch.last_success_at ? "health-warning" : "", `最近成功检查：${dateTime(watch.last_success_at)}`));
  if (watch.last_checked_at && watch.last_checked_at !== watch.last_success_at) {
    health.append(node("div", "", `最近检查：${dateTime(watch.last_checked_at)}`));
  }
  const retryAt = watch.enabled ? retryDeadline(watch) : null;
  const failures = Number.isSafeInteger(watch.consecutive_failures) && watch.consecutive_failures > 0 ? watch.consecutive_failures : 0;
  if (failures) health.append(node("div", "health-warning", `连续 ${failures} 次检查未成功`));
  if (retryAt) {
    const retry = node("div", "watch-retry");
    retry.append(node("strong", "", "已暂缓检查"),
      node("span", "", `下次可检查：${retryDateTime(retryAt)}`),
      node("span", "retry-detail", connected ? "监控会按计划重试，提醒设置和暂停仍可使用。" : "连接恢复后，将继续同步检查安排。"));
    health.append(retry);
  } else if (connected && watch.enabled && failures) {
    health.append(node("div", "retry-ready", "等待重试已结束，可立即检查。"));
  }
  const actions = node("div", "watch-actions");
  const check = node("button", "compact-button", pending.has(`check-${watch.id}`) ? "正在检查…" : retryAt ? "等待重试" : "立即检查");
  check.type = "button";
  check.disabled = !state.csrf_token || !watch.enabled || watch.checking || pending.has(`check-${watch.id}`) || retryAt !== null;
  if (retryAt) check.title = `下次可检查：${retryDateTime(retryAt)}`;
  check.setAttribute("aria-label", `立即检查 ${watch.label || watch.sku}`);
  setActionIdentity(check, watch, "check");
  check.addEventListener("click", () => checkWatch(watch));
  const toggle = node("button", "compact-button subtle", watch.enabled ? "暂停" : "恢复");
  toggle.type = "button";
  toggle.disabled = !state.csrf_token || pending.has(`toggle-${watch.id}`);
  toggle.setAttribute("aria-label", `${watch.enabled ? "暂停" : "恢复"}监控 ${watch.label || watch.sku}`);
  setActionIdentity(toggle, watch, "toggle");
  toggle.addEventListener("click", () => toggleWatch(watch));
  const settings = node("button", "compact-button subtle", "设置提醒");
  settings.type = "button";
  settings.disabled = !state.csrf_token;
  settings.setAttribute("aria-label", `设置提醒 ${watch.label || watch.sku}`);
  setActionIdentity(settings, watch, "settings");
  settings.addEventListener("click", () => openSettings(watch));
  actions.append(check, settings, toggle); bottom.append(health, actions);
  card.append(top, prices, bottom, chart(watch));
  return card;
}

function openForm() {
  $("form-error").hidden = true;
  if (!ui.dialog.open) ui.dialog.showModal();
  $("watch-url").focus();
}

function needsAttention(watch) {
  return Boolean(watch.enabled && !watch.checking && (
    !connected || watch.stale || !watch.last_success_at || watch.availability === "unknown" ||
    !["ok", "success"].includes(watch.status)
  ));
}

function filteredWatches() {
  const query = searchTerm.trim().toLocaleLowerCase();
  const watches = state.watches.filter((watch) => {
    if (retailerFilter !== "all" && retailerKey(watch) !== retailerFilter) return false;
    if (query && ![watch.label, watch.sku, watch.color, watch.size, retailerName(watch)].filter(Boolean).join(" ").toLocaleLowerCase().includes(query)) return false;
    if (statusFilter === "in_stock" && !hasCurrentPrice(watch)) return false;
    if (statusFilter === "out_of_stock" && (!connected || !watch.enabled || watch.stale || watch.availability !== "out_of_stock")) return false;
    if (statusFilter === "attention" && !needsAttention(watch)) return false;
    if (statusFilter === "paused" && watch.enabled) return false;
    return true;
  });
  const byName = (a, b) => String(a.label || a.sku).localeCompare(String(b.label || b.sku), "zh-CN", { numeric: true, sensitivity: "base" });
  watches.sort((a, b) => {
    if (sortMode === "name") return byName(a, b);
    if (sortMode === "price_asc") {
      const priceA = hasCurrentPrice(a) ? a.current_price_minor : Infinity;
      const priceB = hasCurrentPrice(b) ? b.current_price_minor : Infinity;
      return priceA === priceB ? byName(a, b) : priceA - priceB;
    }
    const recentA = Date.parse(a.created_at || "") || 0;
    const recentB = Date.parse(b.created_at || "") || 0;
    return recentB - recentA || Number(b.id) - Number(a.id);
  });
  return watches;
}

function resetFilters() {
  const restoreSearch = ui.list.contains(document.activeElement);
  searchTerm = ""; statusFilter = "all"; retailerFilter = "all"; sortMode = "recent";
  if ($("watch-search")) $("watch-search").value = "";
  if ($("status-filter")) $("status-filter").value = "all";
  if ($("retailer-filter")) $("retailer-filter").value = "all";
  if ($("sort-watches")) $("sort-watches").value = "recent";
  if (loaded) renderWatches();
  if (restoreSearch) $("watch-search")?.focus({ preventScroll: true });
}

function renderWatches() {
  renderRetailerOptions();
  const watches = filteredWatches();
  setText("watch-count", watches.length);
  setText("result-count", `${watches.length} / ${state.watches.length} 个关注项`);
  const liveIds = new Set(state.watches.map((watch) => String(watch.id)));
  for (const id of cardCache.keys()) if (!liveIds.has(id)) cardCache.delete(id);
  if (!watches.length) {
    const empty = node("div", "empty-state");
    const icon = node("span", "empty-icon", "↗");
    icon.setAttribute("aria-hidden", "true");
    const hasAny = state.watches.length > 0;
    empty.append(icon, node("h3", "", hasAny ? "没有符合筛选条件的关注项" : "从你想要的那件装备开始"),
      node("p", "", hasAny ? "尝试调整零售商、状态或搜索词，查看其他关注的装备。" : "添加商品链接和具体颜色、尺码。监控成功后，当前报价和价格历史会自动出现在这里。"));
    const button = node("button", "button button-secondary", hasAny ? "清除筛选条件" : "＋ 添加第一个关注");
    button.type = "button"; button.disabled = !hasAny && !state.csrf_token;
    button.addEventListener("click", hasAny ? resetFilters : openForm);
    empty.append(button);
    preserveWatchFocus(() => reconcile(ui.list, [empty]));
    return;
  }
  const cards = watches.map((watch) => {
    const id = String(watch.id);
    const key = signature({ watch, connected, token: Boolean(state.csrf_token), retryAt: retryDeadline(watch),
      checkPending: pending.has(`check-${id}`), togglePending: pending.has(`toggle-${id}`) });
    let cached = cardCache.get(id);
    if (!cached || cached.signature !== key) {
      cached = { signature: key, element: watchCard(watch) };
      cardCache.set(id, cached);
    }
    return cached.element;
  });
  preserveWatchFocus(() => reconcile(ui.list, cards));
}

function renderRetailerOptions() {
  const select = $("retailer-filter");
  if (!select) return;
  const options = [...new Map(state.watches.map((watch) => [retailerKey(watch), retailerName(watch)])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1]));
  const key = signature(options);
  if (key === retailerOptionsSignature) return;
  retailerOptionsSignature = key;
  const all = node("option", "", "所有零售商"); all.value = "all";
  select.replaceChildren(all);
  options.forEach(([value, label]) => {
    const option = node("option", "", label); option.value = value; select.append(option);
  });
  if (!options.some(([value]) => value === retailerFilter)) retailerFilter = "all";
  select.value = retailerFilter;
}

function renderEvidence() {
  const container = $("historical-evidence-list");
  if (!container) return;
  const records = state.historical_evidence;
  const key = signature({ records, connected });
  if (key === evidenceSignature) return;
  evidenceSignature = key;
  setText("evidence-count", `${records.length} 条来源记录`);
  if (!records.length) {
    container.replaceChildren(node("p", "evidence-empty", "尚无符合条件的历史来源记录。只有找到可核实的价格日期与原文后，才会补充到这里。"));
    return;
  }
  const sorted = [...records].sort((a, b) => String(b.observed_at || "").localeCompare(String(a.observed_at || "")));
  const items = sorted.map((record) => {
    const item = node("article", "evidence-card");
    item.dataset.evidenceKey = record.evidence_key || "";
    item.dataset.includedInMetrics = String(record.included_in_metrics === true);
    const heading = node("div", "evidence-heading");
    const identity = node("div");
    identity.append(node("p", "evidence-retailer", `${record.retailer_name || "零售商待确认"} · ${record.market} / ${record.currency}`),
      node("h3", "", record.product_name || "历史商品记录"));
    heading.append(identity, node("strong", "evidence-price", `${money(record.price_minor, record.currency)} ${record.currency}`));
    const precise = record.date_precision === "second";
    const observed = /^\d{4}-\d{2}-\d{2}/.test(record.observed_at || "") ?
      (precise ? `${record.observed_at.replace("T", " ").replace(/\.\d+Z$/, "Z")}（UTC）` : record.observed_at.slice(0, 10)) : "日期待确认";
    const meta = node("div", "evidence-meta");
    const dateLabel = record.source_kind === "deal_report" ? "原帖发布时间" : "快照日期";
    meta.append(node("span", "", `${dateLabel}：${observed}${precise ? "" : "（来源仅注明日期）"}`),
      node("span", "", `来源：${record.publisher || "原始来源"}`));
    const included = record.included_in_metrics === true;
    const status = node("div", `evidence-status${included ? " included" : ""}`,
      included ? "已核实具体变体 · 已计入对应关注的历史统计" : "历史促销参考 · 未计入上方最低价 / 均价；与当前关注未自动匹配");
    if (record.source_kind === "deal_report") meta.append(node("span", "", "原帖记载的促销价 · 非当前报价"));
    if (record.price_type && record.price_type !== "public") meta.append(node("span", "evidence-condition",
      `价格条件：${({ coupon: "使用优惠码", member: "会员专享", bundle: "组合购买", unknown: "未明确" })[record.price_type] || record.price_type}`));
    const details = node("details", "evidence-details");
    details.append(node("summary", "", "查看来源与核验范围"));
    const scope = [record.model_code ? `型号 ${record.model_code}` : "型号版本未明确",
      record.sku ? `SKU ${record.sku}` : "SKU 未明确", record.color ? `颜色 ${record.color}` : "颜色未明确",
      record.size ? `尺码 ${record.size}` : "尺码未明确"];
    details.append(node("p", "", scope.join(" · ")));
    if (!included) details.append(node("p", "", "来源报道的款式、颜色、尺码、库存或购买条件尚未全部对应到具体关注，因此保留为参考记录。"));
    if (record.exclusion_reason) details.append(node("p", "evidence-review-note", record.exclusion_reason));
    if (record.excerpt) {
      const quote = node("blockquote", "evidence-excerpt", record.excerpt);
      quote.setAttribute("aria-label", "来源原文摘录"); details.append(quote);
    }
    if (record.notes) details.append(node("p", "evidence-review-note", record.notes));
    details.append(node("p", "evidence-captured", `核验保存日期：${String(record.captured_at || "").slice(0, 10) || "待确认"}`));
    const footer = node("div", "evidence-footer");
    footer.append(sourceLink("查看原始来源 ↗", record.source_url, "evidence-source-link"));
    item.append(heading, meta, status, details, footer);
    return item;
  });
  container.replaceChildren(...items);
}

function renderAlerts() {
  const alerts = state.alerts;
  const key = signature({ alerts, watches: state.watches.map(({ id, label, sku, url, currency }) => ({ id, label, sku, url, currency })) });
  if (alertsSignature === key) return;
  alertsSignature = key;
  const liveIds = new Set(alerts.map((alert) => String(alert.id)));
  for (const id of alertCache.keys()) if (!liveIds.has(id)) alertCache.delete(id);
  if (!alerts.length) {
    ui.alerts.replaceChildren(node("div", "alert-empty", "还没有提醒。符合你设置的条件并通过报价复核后，会在这里留下记录。"));
    return;
  }
  const items = [...alerts].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))).map((alert) => {
    const watch = state.watches.find((item) => item.id === alert.watch_id);
    const itemKey = signature({ alert, label: watch?.label, sku: watch?.sku, url: watch?.url, currency: watch?.currency });
    const cached = alertCache.get(String(alert.id));
    if (cached?.signature === itemKey) return cached.element;
    const item = node("article", "alert-item"), symbol = node("span", "alert-symbol", "↓");
    symbol.setAttribute("aria-hidden", "true");
    const copy = node("div", "alert-copy");
    const label = alert.label || watch?.label || watch?.sku || "关注的商品";
    const currency = alert.currency || watch?.currency;
    copy.append(node("h3", "alert-title", label), node("div", "alert-meta", `${ruleNames[alert.rule] || "价格提醒"} · ${dateTime(alert.created_at)} · ${currency || ""}`));
    item.append(symbol, copy, node("div", "alert-price", money(alert.price_minor, currency)),
      externalLink("查看商品 ↗", alert.url || watch?.url, "alert-link"));
    alertCache.set(String(alert.id), { signature: itemKey, element: item });
    return item;
  });
  reconcile(ui.alerts, items);
}

function renderHistory() {
  renderEvidence();
  const select = $("history-watch"), content = $("history-content");
  if (!select || !content) return;
  const optionsKey = signature(state.watches.map(({ id, label, sku, color, size, market, currency, retailer_name }) => ({ id, label, sku, color, size, market, currency, retailer_name })));
  if (optionsKey !== historyOptionsSignature) {
    historyOptionsSignature = optionsKey;
    select.replaceChildren();
    select.disabled = state.watches.length === 0;
    if (!state.watches.length) {
      const option = node("option", "", "先添加一个关注项"); option.value = ""; select.append(option);
    } else {
      for (const watch of state.watches) {
        const option = node("option", "", `${watch.label || watch.sku} · ${retailerName(watch)} · ${watch.color} / ${watch.size} · ${watch.market} ${watch.currency}`);
        option.value = String(watch.id); select.append(option);
      }
    }
  }
  if (!state.watches.some((watch) => String(watch.id) === selectedHistoryId)) {
    selectedHistoryId = state.watches.length ? String(state.watches[0].id) : null;
  }
  select.value = selectedHistoryId || "";
  const watch = state.watches.find((item) => String(item.id) === selectedHistoryId);
  const key = signature({ watch: watch || null, connected });
  if (historySignature === key) return;
  historySignature = key;
  if (!watch) {
    const empty = node("div", "empty-state");
    empty.append(node("h3", "", "价格历史从第一次关注开始"),
      node("p", "", "添加一个明确的颜色和尺码，成功采集后即可查看真实价格记录与覆盖天数。"));
    const add = node("button", "button button-primary", "添加关注"); add.type = "button"; add.disabled = !state.csrf_token;
    add.addEventListener("click", openForm); empty.append(add); content.replaceChildren(empty); return;
  }
  const wrapper = node("article", "history-watch");
  const heading = node("div", "history-heading");
  heading.append(node("h3", "", watch.label || watch.sku),
    node("p", "", `${retailerName(watch)} · ${watch.color} · 尺码 ${watch.size} · ${watch.market} / ${watch.currency}`));
  const stats = priceMetrics(watch);
  wrapper.append(heading, stats, chart(watch, true),
    node("p", "method-note", "仅统计全新、现货、公开单件报价。历史最低指已观测记录中的最低价；覆盖天数不足时，不代表完整历史。"));
  content.replaceChildren(wrapper);
}

function render() {
  if (state.supported_retailers.length) {
    const sources = state.supported_retailers.map((source) => {
      const name = source.key === "arcteryx-official" ? "Arc’teryx 官网与 Outlet" : source.name;
      return `${name}（美国）`;
    });
    setText("supported-source-hint", `粘贴美国商品链接，再选择具体颜色和尺码。当前支持：${sources.join("、")}。`);
  }
  setText("active-count", state.watches.filter((watch) => watch.enabled).length);
  setText("fresh-count", state.watches.filter(hasCurrentPrice).length);
  setText("alert-count", state.alerts.length);
  setText("attention-count", state.watches.filter(needsAttention).length);
  setText("nav-watch-count", state.watches.length);
  setText("nav-alert-count", state.alerts.length);
  renderWatches(); renderAlerts(); renderHistory();
}

function restoreActionFocus(watchId, action) {
  if (document.activeElement !== document.body) return;
  const button = [...ui.list.querySelectorAll("[data-watch-action]")].find((item) =>
    item.dataset.watchId === String(watchId) && item.dataset.watchAction === action && !item.disabled);
  if (button) button.focus({ preventScroll: true });
}

async function checkWatch(watch) {
  const key = `check-${watch.id}`;
  if (pending.has(key)) return;
  if (retryDeadline(watch)) { showFeedback(waitingMessage(watch)); return; }
  pending.add(key); renderWatches();
  try {
    const response = await api(`/api/watches/${encodeURIComponent(watch.id)}/check`, { method: "POST", body: "{}" });
    await refresh();
    const current = state.watches.find((item) => item.id === watch.id);
    if (["rate_limited", "backoff"].includes(response.check_status)) {
      showFeedback(waitingMessage(current || response,
        response.check_status === "rate_limited" ? "来源暂时需要放慢检查" : "尚未到重试时间"));
      return;
    }
    const info = current ? statusInfo({ ...current, id: null }) : null;
    showFeedback(info ? `检查完成：${info.text}。` : "检查请求已完成。", info?.tone === "warning" || info?.tone === "error");
  } catch (error) { showFeedback(error.message, true); }
  finally { pending.delete(key); renderWatches(); restoreActionFocus(watch.id, "check"); }
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
  finally { pending.delete(key); renderWatches(); restoreActionFocus(watch.id, "toggle"); }
}

function updateSettingsForm() {
  const rule = $("settings-rule")?.value, target = $("settings-target");
  if (!target) return;
  target.disabled = rule === "new_low";
  target.required = rule === "target";
  if ($("settings-target-field")) $("settings-target-field").hidden = rule === "new_low";
  setText("settings-target-hint", rule === "restock" ? "可留空。填写后只在补货且不高于该价格时提醒。" : rule === "new_low" ? "首次有效报价作为基线，之后出现更低的现货价时提醒。" : "价格达到或低于目标价并通过复核时提醒。");
}

function openSettings(watch) {
  const dialog = $("settings-dialog");
  if (!dialog) return;
  settingsWatchId = watch.id;
  setText("settings-watch-name", `${watch.label || watch.sku} · ${watch.color} / ${watch.size}`);
  $("settings-rule").value = watch.rule;
  $("settings-target").value = watch.target_price_minor ? (watch.target_price_minor / 100).toFixed(2) : "";
  setText("settings-target-currency", watch.currency);
  const interval = $("settings-interval");
  interval.querySelectorAll("option[data-custom-interval]").forEach((option) => option.remove());
  if (![...interval.options].some((option) => option.value === String(watch.interval_minutes))) {
    const custom = node("option", "", `每 ${watch.interval_minutes} 分钟（当前设置）`);
    custom.value = String(watch.interval_minutes); custom.dataset.customInterval = "true";
    interval.append(custom);
  }
  interval.value = String(watch.interval_minutes);
  $("settings-error").hidden = true;
  updateSettingsForm();
  if (!dialog.open) dialog.showModal();
  $("settings-rule").focus();
}

async function saveSettings(event) {
  event.preventDefault();
  const save = $("save-settings");
  if (save.disabled || settingsWatchId === null) return;
  const id = settingsWatchId;
  savingSettings = true; syncWriteControls(); save.textContent = "正在保存…"; $("settings-error").hidden = true;
  try {
    const rule = $("settings-rule").value;
    const target = rule === "new_low" ? null : priceToMinor($("settings-target").value);
    if (rule === "target" && target === null) throw new Error("请填写目标价。");
    await api(`/api/watches/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({
      rule, target_price_minor: target, interval_minutes: Number($("settings-interval").value),
    }) });
    $("settings-dialog").close();
    showFeedback("提醒设置已更新，将按新的条件监控。");
    await refresh(); restoreActionFocus(id, "settings");
  } catch (error) {
    $("settings-error").textContent = error.message; $("settings-error").hidden = false;
  } finally { savingSettings = false; syncWriteControls(); save.textContent = "保存设置"; }
}

function updateForm() {
  const value = $("watch-url").value.trim(), location = productLocation(value), url = location?.url;
  const selectedMarket = location?.market;
  const currency = "USD";
  $("market-hint").textContent = selectedMarket ? `${location.retailer} · 美国商品，价格以 ${currency} 统计。` : value ? "请填写已支持零售商的美国具体商品链接。" : "仅支持美国商品链接，价格以 USD 统计。";
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
$("empty-add")?.addEventListener("click", openForm);
document.querySelectorAll("button[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
window.addEventListener("hashchange", () => {
  const value = location.hash.slice(1);
  if (Object.hasOwn(viewLabels, value)) setView(value, false);
});
$("watch-search")?.addEventListener("input", (event) => {
  searchTerm = event.target.value;
  if (loaded) renderWatches();
});
$("status-filter")?.addEventListener("change", (event) => {
  statusFilter = event.target.value;
  if (loaded) renderWatches();
});
$("retailer-filter")?.addEventListener("change", (event) => {
  retailerFilter = event.target.value;
  if (loaded) renderWatches();
});
$("sort-watches")?.addEventListener("change", (event) => {
  sortMode = event.target.value;
  if (loaded) renderWatches();
});
$("reset-filters")?.addEventListener("click", resetFilters);
$("history-watch")?.addEventListener("change", (event) => {
  selectedHistoryId = event.target.value || null;
  renderHistory();
});
$("refresh-state")?.addEventListener("click", async () => {
  const button = $("refresh-state");
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    await refresh();
    if (connected) showFeedback("监控状态已刷新。");
  } finally { button.disabled = false; button.removeAttribute("aria-busy"); }
});
$("settings-rule")?.addEventListener("change", updateSettingsForm);
$("settings-form")?.addEventListener("submit", saveSettings);
$("close-settings")?.addEventListener("click", () => $("settings-dialog").close());
$("settings-dialog")?.addEventListener("click", (event) => {
  const dialog = $("settings-dialog");
  if (event.target !== dialog) return;
  const rect = dialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
});
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
ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (ui.save.disabled) return;
  $("form-error").hidden = true;
  savingWatch = true;
  syncWriteControls();
  ui.save.textContent = "正在保存…";
  try {
    const url = safeProductURL($("watch-url").value.trim());
    if (!url) throw new Error("请使用已支持零售商的美国具体商品链接，完整支持范围见窗口说明。");
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
    savingWatch = false;
    syncWriteControls();
    ui.save.textContent = "开始关注 ↗";
  }
});

updateForm();
syncWriteControls();
setView(location.hash.slice(1) || "overview", false);
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
