(() => {
  const form = document.querySelector("#batch-runner");
  if (!form) return;
  const progress = document.querySelector("#batch-progress");
  let busy = false;
  async function post(url, action) {
    const body = new URLSearchParams({csrf_token: form.elements.csrf_token.value, action});
    const response = await fetch(url, {method: "POST", body, credentials: "same-origin"});
    return response.ok && !response.redirected && response.headers.get("content-type")?.includes("application/json");
  }
  async function run() {
    if (busy) return;
    busy = true;
    const rows = [...document.querySelectorAll("[data-batch-queued]")];
    try {
      for (let i = 0; i < rows.length; i++) {
        progress.textContent = `处理第 ${i + 1} / ${rows.length} 票…`;
        if (!await post(rows[i].dataset.batchQueued, "run")) throw Error();
      }
      window.location.reload();
    } catch {
      progress.textContent = "批次已暂停。刷新查看结果；已提交或结果不明的订单不会重发。";
    }
  }
  form.addEventListener("submit", event => { event.preventDefault(); run(); });
  for (const button of document.querySelectorAll("[data-batch-query]")) {
    button.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      try {
        if (!await post(button.dataset.batchQuery, "query")) throw Error();
        window.location.reload();
      } catch {
        progress.textContent = "当前不能完成查询。请稍后再查订单记录。";
        busy = false;
      }
    });
  }
  if (form.dataset.start === "yes" && document.querySelector("[data-batch-queued]")) run();
})();
