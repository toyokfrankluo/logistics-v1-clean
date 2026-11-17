"use strict";

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
    if (!images || attachments.length !== 1) throw new Error();
    const draft = manifest.draft;
    const draftStatus = draft.error ? `下单预览：${draft.error}`
      : draft.combined ? `下单预览同时包含图片和装箱单，${draft.po_rows} 行使用客户原始发票 PO。`
      : "下单预览已通过当前渠道规则。";
    status.textContent = `外部 HTTPS 验收通过：${images} 张商品图及 packing.xlsx 均无 Cookie 读取成功，字节一致；无签名请求被拒绝。装箱单链接 ${attachments[0]} 字符。${draftStatus}`;
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
document.addEventListener("change", event => {
  const agent = event.target;
  if (agent.name !== "agent_id" || !agent.form?.matches("[data-pricing]")) return;
  const form = agent.form;
  const list = agent.selectedOptions[0]?.dataset.channelList;
  const picker = form.querySelector("[data-channel-picker]");
  const select = picker.querySelector("select");
  const manual = form.querySelector("[data-manual-channel]");
  const input = manual.querySelector("input");
  const options = list && form.querySelector(`template[data-agent-options="${list}"]`);
  select.replaceChildren(new Option("请选择实际渠道", ""));
  if (options) select.append(options.content.cloneNode(true));
  select.value = "";
  input.value = "";
  picker.querySelector("[data-channel-search]").value = "";
  picker.querySelector("[data-channel-empty]").hidden = select.options.length > 1;
  picker.hidden = select.disabled = !options;
  manual.hidden = input.disabled = Boolean(options) || !agent.value;
  form.elements.namedItem("confirmed").checked = false;
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
