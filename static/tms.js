"use strict";

let attachmentFixture;
document.addEventListener("change", async event => {
  if (!event.target.matches("[data-preflight-fixture]")) return;
  const status = document.querySelector("[data-preflight-status]");
  try {
    attachmentFixture = JSON.parse(await event.target.files[0].text());
    if (attachmentFixture.invoice.customer_number !== "XR-PREFLIGHT-DO-NOT-SHIP") throw new Error();
    document.querySelector("[data-sales-service]").textContent = attachmentFixture.invoice.service;
    status.textContent = "模拟资料已载入，未创建业务订单。";
  } catch { attachmentFixture = null; status.textContent = "模拟验收资料无效。"; }
});
document.addEventListener("click", async event => {
  if (!event.target.matches("[data-preflight-run]")) return;
  const status = document.querySelector("[data-preflight-status]");
  const root = document.querySelector("[data-supplier-picker]");
  const agent = root.querySelector('[name="agent_id"]').value;
  const code = root.querySelector("[data-channel-picker] select").value;
  if (!attachmentFixture || !agent || !code) { status.textContent = "请选择供应商、渠道并载入模拟资料。"; return; }
  event.target.disabled = true;
  status.dataset.state = "running";
  status.textContent = "正在匿名回读图片和装箱单，并构造预检请求…";
  try {
    const files = attachmentFixture.manifests[agent];
    for (const file of files) {
      const url = new URL(file.url);
      if (url.origin !== location.origin || !url.pathname.startsWith("/tms/supplier-files/")) throw new Error();
      const options = {credentials:"omit",redirect:"error",cache:"no-store"};
      const denied = await fetch(new URL(url.pathname, url.origin), options);
      if (![403,404].includes(denied.status)) throw new Error();
      const response = await fetch(url, options);
      const bytes = await response.arrayBuffer();
      const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(n=>n.toString(16).padStart(2,"0")).join("");
      if (response.status !== 200 || bytes.byteLength !== file.bytes || digest !== file.sha256 ||
          response.headers.get("Content-Type")?.split(";")[0] !== file.mime) throw new Error();
    }
    const response = await fetch("/tms/operations/agents/preflight", {method:"POST", credentials:"same-origin",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({invoice:attachmentFixture.invoice,
        deliveries:attachmentFixture.deliveries,agent_id:Number(agent),channel_code:code})});
    if (!response.ok) throw new Error();
    const row = (await response.json()).results[0];
    const packing = files.find(file=>file.slot === "packing");
    const verified = `匿名回读通过：${files.length-1} 张商品图及完整 packing.xlsx，文件字节一致，无签名访问被拒绝。附件链接 ${packing.url.length} 字符。`;
    if (row.passed.includes(code)) {
      status.dataset.state = "passed";
      status.textContent = verified + (row.unknown_rules?.[code]?.length
        ? " 通用请求预检通过；规则未知，首次真实提交待验证。未发送供应商。"
        : " 通用请求预检通过，未发送供应商；不代表供应商已真实接受。");
    } else {
      status.dataset.state = "blocked";
      status.textContent = verified + " 当前渠道仍阻断：" + (row.blocked[code] || "规则待核实");
    }
  } catch { status.dataset.state="error"; status.textContent="附件回读或请求预检未通过，请重新检查模拟资料与链接有效期。"; }
  finally { event.target.disabled = false; }
});

const initialPassword = document.getElementById("initial-password");
const passwordToggle = document.getElementById("toggle-password");
const passwordGenerator = document.getElementById("generate-password");
if (initialPassword && passwordToggle && passwordGenerator) {
  passwordToggle.hidden = false;
  passwordToggle.addEventListener("click", () => {
    const revealed = initialPassword.type === "password";
    initialPassword.type = revealed ? "text" : "password";
    passwordToggle.textContent = revealed ? "隐藏密码" : "显示密码";
    passwordToggle.setAttribute("aria-pressed", String(revealed));
  });
  if (window.crypto?.getRandomValues) {
    passwordGenerator.hidden = false;
    passwordGenerator.addEventListener("click", () => {
      const randomBytes = crypto.getRandomValues(new Uint8Array(12));
      initialPassword.value = Array.from(randomBytes, byte => byte.toString(16).padStart(2, "0")).join("");
      initialPassword.focus();
    });
  }
}

function regularClick(event) {
  return !event.defaultPrevented && event.button === 0 &&
    !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

// Test the same public HTTPS route that the carrier uses, without the admin session.
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("form[data-supplier-files-check]")) return;
  event.preventDefault();
  const button = form.querySelector("button");
  const status = form.querySelector("[role=status]");
  if (button.disabled) return;
  button.disabled = true;
  status.textContent = "正在验收外部读取…";
  const getOptions = {
    credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer",
  };
  try {
    const prepared = await fetch(form.action, {
      method: "POST", body: new FormData(form), credentials: "same-origin",
      cache: "no-store", redirect: "error",
    });
    const manifest = await prepared.json();
    if (!prepared.ok) {
      status.textContent = manifest.error || "无法准备验收文件。";
      return;
    }
    let images = 0;
    const attachments = [];
    for (const file of manifest.files) {
      const url = new URL(file.url);
      if (url.origin !== window.location.origin || url.protocol !== "https:") throw new Error();
      const unsigned = await fetch(new URL(url.pathname, url.origin), getOptions);
      if (unsigned.status !== 404) throw new Error();
      const download = await fetch(url, getOptions);
      if (!download.ok || download.headers.get("content-type") !== file.mime) throw new Error();
      const bytes = await download.arrayBuffer();
      const hash = await crypto.subtle.digest("SHA-256", bytes);
      const hex = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
      if (bytes.byteLength !== file.bytes || hex !== file.sha256) throw new Error();
      if (file.mime.startsWith("image/")) images++;
      else if (url.pathname.endsWith("/packing.xlsx") && file.url.length <= 500) attachments.push(file.url.length);
      else throw new Error();
    }
    if (attachments.length !== 1) throw new Error();
    const draft = manifest.draft;
    const draftStatus = draft.error ? `下单预览：${draft.error}`
      : draft.combined ? `下单预览同时包含图片和装箱单，${draft.po_rows} 行使用客户原始发票 PO。`
      : "下单预览已通过当前渠道规则。";
    status.textContent = `外部 HTTPS 验收通过：${images} 张商品图及 packing.xlsx 均无 Cookie 读取成功，字节一致；无签名请求被拒绝。装箱单链接 ${attachments[0]} 字符。${draft.status || draftStatus}`;
  } catch {
    status.textContent = "外部 HTTPS 验收未通过。请刷新订单页后重试。";
  } finally {
    button.disabled = false;
  }
});

const importDialog = document.getElementById("import-dialog");
if (typeof importDialog?.showModal === "function") {
  document.querySelectorAll("[data-open-import]").forEach(link => {
    link.addEventListener("click", event => {
      if (!regularClick(event)) return;
      event.preventDefault();
      importDialog.showModal();
    });
  });
}

const drawer = document.getElementById("order-drawer");
const drawerBody = document.getElementById("drawer-body");
if (drawerBody && typeof drawer?.showModal === "function") {
  let loadRequest;
  drawer.addEventListener("close", () => {
    loadRequest?.abort();
    drawerBody.replaceChildren();
    drawerBody.setAttribute("aria-busy", "false");
  });
  document.querySelectorAll("[data-order-drawer]").forEach(link => {
    link.addEventListener("click", async event => {
      if (!regularClick(event)) return;
      event.preventDefault();
      const url = new URL(link.href);
      url.searchParams.set("drawer", "1");
      const request = new AbortController();
      loadRequest?.abort();
      loadRequest = request;
      drawerBody.textContent = "正在加载运单…";
      drawerBody.setAttribute("aria-busy", "true");
      drawer.showModal();
      try {
        const response = await fetch(url, { signal: request.signal, credentials: "same-origin" });
        if (!response.ok || response.redirected) throw new Error("Detail unavailable");
        const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
        const detail = documentFragment.querySelector("[data-order-detail]");
        if (!detail) throw new Error("Missing detail");
        if (request.signal.aborted) return;
        drawerBody.replaceChildren(detail);
        detail.querySelectorAll("[data-pricing]").forEach(pricingPreview);
        initSupplierPickers(detail);
        drawerBody.scrollTop = 0;
      } catch {
        if (!request.signal.aborted) window.location.assign(link.href);
      } finally {
        if (!request.signal.aborted) drawerBody.setAttribute("aria-busy", "false");
      }
    });
  });
}

// Integer arithmetic here matches Decimal rounding on the server. The preview is never submitted.
function scaled(value, places) {
  if (!new RegExp(`^[0-9]{1,9}(?:\\.[0-9]{1,${places}})?$`).test(value.trim())) return null;
  const [whole, fraction = ""] = value.trim().split(".");
  return BigInt(whole + fraction.padEnd(places, "0"));
}
function formatCents(cents, currency) {
  return `${cents / 100n}.${(cents % 100n).toString().padStart(2, "0")}${currency ? " " + currency : ""}`;
}
function pricingPreview(form) {
  const field = name => form.elements.namedItem(name).value;
  const currency = field("currency");
  const rows = [...form.querySelectorAll("[data-fee-row]")];
  const amounts = rows.map(row => scaled(row.querySelector('[name="other_amount"]').value, 2));
  const validAmounts = entries => entries.every(amount => amount !== null && amount <= 10000000000n);
  const mode = field("other_mode");
  const other = (mode === "none" && rows.length === 0) ||
    (mode === "items" && rows.length > 0 && validAmounts(amounts) && rows.every(row =>
      row.querySelector('[name="other_name"]').value.trim() &&
      row.querySelector('[name="other_currency"]').value === currency))
    ? amounts.reduce((sum, amount) => sum + amount, 0n) : null;
  form.querySelector("[data-other-total]").textContent = other === null ? "—" : formatCents(other, currency);
  form.querySelector("[data-add-fee]").disabled = rows.length >= 200;
}
document.addEventListener("input", event => {
  if (event.target.matches("[data-channel-search]")) {
    const select = event.target.closest("[data-channel-picker]").querySelector("select");
    const query = event.target.value.trim().toLocaleLowerCase();
    for (const option of select.options) {
      option.hidden = Boolean(option.value && option.value !== select.value &&
        !option.textContent.toLocaleLowerCase().includes(query));
    }
    return;
  }
  const form = event.target.closest("[data-pricing]");
  if (form) {
    if (event.target.name !== "confirmed") form.elements.namedItem("confirmed").checked = false;
    pricingPreview(form);
  }
});
const supplierLoads = new WeakMap();
const supplierRequirements = new WeakMap();
function showSupplierRequirements(root) {
  const code = root.querySelector("[data-channel-picker] select").value;
  const output = root.querySelector("[data-channel-requirements]");
  output.hidden = !code;
  if (!code) return;
  const rule = supplierRequirements.get(root)?.[code];
  if (!rule) { output.textContent = "供应商尚未提供此渠道的完整规则，创建前须核实。"; return; }
  const labels = {brand:"品牌", model:"型号", sku:"商品 SKU", sale_url:"销售链接", amazon_ref_id:"Amazon 参考号",
    is_magnetic:"真实带磁属性", is_battery:"真实带电属性", photos:"商品图片", photo_url:"图片链接",
    name_zh:"中文品名", name_en:"英文品名", material:"材质", usage:"用途", hscode:"HS 编码", qty:"数量", unit_value:"申报单价"};
  const required = rule.unknown_rules?.includes("require_declaration_list") ? "申报必填列表未知" :
    rule.declarations.map(key=>labels[key] || key).join("、") || "未额外指定申报字段";
  const missing = [...new Set([...Object.values(rule.missing).flat(), ...(rule.missing_taxes || [])])]
    .map(key=>({vat_number:"VAT 税号",eori:"EORI 税号",weight:"单件产品重量",size:"单件产品尺寸",asin:"ASIN",fnsku:"FNSKU"}[key] || labels[key] || key));
  output.textContent = (rule.unknown_rules?.length ? "规则未知，首次真实提交待验证。已知要求：" : "本渠道要求：") +
    `${required}。${rule.packing === null ? "装箱单要求未知，可自动准备通用附件。" : rule.packing ? "装箱单由系统自动生成。" : "无需单独装箱单。"}` +
    (missing.length ? `尚缺真实资料：${missing.join("、")}。` : "") +
    (rule.declarations.includes("photos") && !rule.photos_confirmed ? "photos 图片编码协议仍待该供应商确认。" : "") +
    (["yes","required"].includes(rule.sender) ? "还需核对供应商配置中的发件地址。" : "");
}
async function loadSupplierChannels(root, preserve = false) {
  supplierLoads.get(root)?.abort();
  root.removeAttribute("aria-busy");
  const request = new AbortController();
  supplierLoads.set(root, request);
  const agent = root.querySelector('[name="agent_id"]');
  const form = agent.form;
  const scope = root.querySelector('[name="supplier_binding"]');
  const picker = root.querySelector("[data-channel-picker]");
  const select = picker.querySelector("select");
  const manual = root.querySelector("[data-manual-channel]");
  const input = manual.querySelector("input");
  const status = picker.querySelector("[data-channel-empty]");
  const settings = picker.querySelector("[data-channel-settings]");
  const search = picker.querySelector("[data-search-label]");
  const previous = preserve ? (select.value || root.dataset.currentChannel) : "";
  const previousBinding = scope.value;
  select.replaceChildren(new Option(agent.value ? "请选择实际渠道" : "请先选择实际物流供应商", ""));
  if (!preserve) input.value = "";
  picker.querySelector("[data-channel-search]").value = "";
  scope.value = "";
  supplierRequirements.delete(root);
  root.querySelector("[data-channel-requirements]").hidden = true;
  picker.hidden = false;
  select.disabled = manual.hidden = input.disabled = settings.hidden = search.hidden = status.hidden = true;
  if (!agent.value) return;
  if (!agent.selectedOptions[0]?.dataset.channelList) {
    picker.hidden = true;
    manual.hidden = input.disabled = false;
    return;
  }
  status.hidden = false;
  status.textContent = "正在读取此供应商的渠道…";
  root.setAttribute("aria-busy", "true");
  try {
    const requestFields = {agent_id:agent.value,csrf_token:form.elements.csrf_token.value};
    if (root.dataset.channelUrl.startsWith("/tms/operations/") && attachmentFixture) {
      requestFields.preflight_invoice = JSON.stringify(attachmentFixture.invoice);
    }
    const response = await fetch(root.dataset.channelUrl, {
      method: "POST", credentials: "same-origin", signal: request.signal,
      body: new URLSearchParams(requestFields),
    });
    if (response.redirected) throw new Error();
    const data = await response.json();
    if (request.signal.aborted || !root.isConnected) return;
    if (!response.ok || data.error || !data.managed) {
      status.textContent = data.error || "供应商配置已变化，请刷新页面重新选择。";
      if (data.settings) { settings.href = data.settings; settings.hidden = false; }
      return;
    }
    for (const [code, name] of Object.entries(data.catalogue).sort(([a], [b]) => a.localeCompare(b))) {
      select.add(new Option(`${name} · ${code}${code === data.recommended ? "（推荐）" : ""}`, code));
    }
    if (previous && previousBinding === data.binding && Object.hasOwn(data.catalogue, previous)) {
      select.value = previous;
    }
    scope.value = data.binding;
    supplierRequirements.set(root, data.requirements);
    showSupplierRequirements(root);
    select.disabled = search.hidden = false;
    status.hidden = true;
  } catch {
    if (!request.signal.aborted) status.textContent = "渠道暂时无法读取。请刷新页面或重新选择供应商。";
  } finally {
    if (!request.signal.aborted) root.removeAttribute("aria-busy");
  }
}
function initSupplierPickers(root) {
  root.querySelectorAll("[data-supplier-picker]").forEach(picker => {
    if (picker.querySelector('[name="agent_id"]').selectedOptions[0]?.dataset.channelList &&
        picker.querySelector("[data-channel-picker] select").disabled) loadSupplierChannels(picker, true);
  });
}
document.addEventListener("change", event => {
  const picker = event.target.closest("[data-supplier-picker]");
  if (!picker) return;
  const confirmed = event.target.form.elements.namedItem("assignment_confirmed");
  if (confirmed && ["agent_id", "agent_service"].includes(event.target.name)) confirmed.checked = false;
  if (event.target.name === "agent_id") loadSupplierChannels(picker);
  if (event.target.name === "agent_service") showSupplierRequirements(picker);
});
document.addEventListener("click", event => {
  const button = event.target.closest("[data-add-fee], [data-remove-fee]");
  const form = button?.closest("[data-pricing]");
  if (!form) return;
  if (button.hasAttribute("data-add-fee")) {
    if (form.querySelectorAll("[data-fee-row]").length >= 200) return;
    const fragment = form.querySelector("[data-fee-template]").content.cloneNode(true);
    const row = fragment.querySelector("[data-fee-row]");
    row.querySelector('[name="other_currency"]').value = form.elements.namedItem("currency").value;
    form.querySelector("[data-fee-lines]").append(fragment);
    form.elements.namedItem("other_mode").value = "items";
    row.querySelector("input").focus();
  } else {
    button.closest("[data-fee-row]").remove();
  }
  form.elements.namedItem("confirmed").checked = false;
  pricingPreview(form);
});
document.querySelectorAll("[data-pricing]").forEach(pricingPreview);
initSupplierPickers(document);

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-open-order-delete]");
  if (trigger) trigger.closest(".order-delete").querySelector("dialog").showModal();
});
