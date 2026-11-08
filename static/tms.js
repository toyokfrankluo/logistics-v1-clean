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
  const quantity = scaled(field("quantity"), 3);
  const rate = scaled(field("rate"), 4);
  const validQuantity = quantity !== null && quantity > 0n && quantity <= 1000000000n &&
    (field("unit") !== "shipment" || quantity === 1000n) &&
    (field("unit") !== "box" || quantity % 1000n === 0n);
  const base = validQuantity && rate !== null && rate <= 10000000000n ?
    (quantity * rate + 50000n) / 100000n : null;
  const fixed = ["surcharge", "operation", "high_value"].map(name => scaled(field(name), 2));
  const rows = [...form.querySelectorAll("[data-fee-row]")];
  const amounts = rows.map(row => scaled(row.querySelector('[name="other_amount"]').value, 2));
  const validAmounts = entries => entries.every(amount => amount !== null && amount <= 10000000000n);
  const mode = field("other_mode");
  const other = (mode === "none" && rows.length === 0) ||
    (mode === "items" && rows.length > 0 && validAmounts(amounts) && rows.every(row =>
      row.querySelector('[name="other_name"]').value.trim() &&
      row.querySelector('[name="other_currency"]').value === currency))
    ? amounts.reduce((sum, amount) => sum + amount, 0n) : null;
  form.querySelector("[data-base]").textContent = base === null ? "—" : formatCents(base, currency);
  form.querySelector("[data-other-total]").textContent = other === null ? "—" : formatCents(other, currency);
  const total = base !== null && other !== null && currency && field("unit") && validAmounts(fixed) ?
    formatCents(base + other + fixed.reduce((sum, amount) => sum + amount, 0n), currency) : "填写完整后计算";
  form.querySelector("[data-total]").textContent = total;
  form.querySelector("[data-add-fee]").disabled = rows.length >= 200;
}
document.addEventListener("input", event => {
  const form = event.target.closest("[data-pricing]");
  if (form) {
    if (event.target.name !== "confirmed") form.elements.namedItem("confirmed").checked = false;
    pricingPreview(form);
  }
});
document.addEventListener("change", event => {
  const agent = event.target;
  if (agent.name !== "agent_id" || !agent.form?.matches("[data-pricing]")) return;
  const channel = agent.form.elements.namedItem("agent_service");
  const list = agent.selectedOptions[0]?.dataset.channelList;
  channel.value = "";
  if (list) channel.setAttribute("list", list);
  else channel.removeAttribute("list");
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
