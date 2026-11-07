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
        drawerBody.scrollTop = 0;
      } catch {
        if (!request.signal.aborted) window.location.assign(link.href);
      } finally {
        if (!request.signal.aborted) drawerBody.setAttribute("aria-busy", "false");
      }
    });
  });
}
