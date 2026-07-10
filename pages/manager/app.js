const bridge = window.AstrBotPluginPage;

const labels = {
  card: "卡片",
  record: "聊天记录",
  image: "图片",
  text: "文字",
};

const orderLabels = {
  card: "QQ 卡片",
  record: "聊天记录",
  image: "图片",
  text: "文字",
};

const defaultOrder = ["record", "card", "image", "text"];

let state = null;

await bridge.ready();

const $ = (id) => document.getElementById(id);

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 3200);
}

function listToText(value) {
  return Array.isArray(value) ? value.join("\n") : "";
}

function textToList(value) {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function activeName(collection, activeId) {
  const item = Object.values(collection || {}).find((entry) => entry.id === activeId);
  return item ? item.name : "未设置";
}

async function loadState() {
  state = await bridge.apiGet("state");
  render();
}

function render() {
  const settings = state.settings;
  $("runtime").textContent = `队列 ${state.queue_size} · Worker ${state.worker_running ? "运行中" : "未运行"}`;
  $("queueSize").textContent = String(state.queue_size);
  $("activeCard").textContent = activeName(toMap(state.cards), settings.active_card_id);
  $("activeRecord").textContent = activeName(toMap(state.records), settings.active_record_id);
  $("activeImage").textContent = activeName(toMap(state.images), settings.active_image_id);
  $("activeText").textContent = `${textSegments(settings.text_content).length} 段`;

  setValue("enabled", settings.enabled);
  setValue("mode", settings.mode);
  setValue("whitelistGroups", listToText(settings.whitelist_groups));
  setValue("blacklistGroups", listToText(settings.blacklist_groups));
  setValue("adminQqList", listToText(settings.admin_qq_list));
  setValue("sendInterval", settings.send_interval_seconds);
  setValue("retryEnabled", settings.retry_enabled);
  setValue("retryCount", settings.retry_count);
  setValue("retryInterval", settings.retry_interval_seconds);
  setValue("deliveryCompensationEnabled", settings.delivery_compensation_enabled);
  setValue("deliveryConfirmWait", settings.delivery_confirm_wait_seconds);
  setValue("deliveryCompensationCount", settings.delivery_compensation_count);
  setValue("deliveryCompensationInterval", settings.delivery_compensation_interval_seconds);
  setValue("dedupeEnabled", settings.dedupe_enabled);
  setValue("dedupeMinutes", settings.dedupe_minutes);
  setValue("cardFallbackEnabled", settings.card_fallback_enabled);
  setValue("cardFallbackText", settings.card_fallback_text);
  setValue("recordFallbackEnabled", settings.record_fallback_enabled);
  setValue("recordFallbackText", settings.record_fallback_text);
  setValue("imageFallbackEnabled", settings.image_fallback_enabled);
  setValue("imageFallbackText", settings.image_fallback_text);
  setValue("textContent", settings.text_content);
  setValue("groupFallbackEnabled", settings.group_fallback_enabled);
  setValue("groupFallbackMode", settings.group_fallback_mode);
  setValue("groupFallbackAt", settings.group_fallback_at);
  setValue("groupFallbackTemplate", settings.group_fallback_template);
  setValue("notifyAdminPrivate", settings.notify_admin_private);
  setValue("notifyAdminGroup", settings.notify_admin_group);
  setValue("notifyGroupId", settings.notify_group_id);
  setValue("notifyOnSuccess", settings.notify_on_success);
  setValue("maxLogs", settings.max_logs);
  setValue("testReceiver", settings.test_receiver_qq);
  setValue("dailyTestEnabled", settings.daily_test_enabled);
  setValue("dailyTestReceiver", settings.daily_test_receiver_qq);
  setValue("dailyTestTime", settings.daily_test_time);
  setValue("maxAgentStep", state.astrbot?.max_agent_step ?? 30);

  renderOrder(settings.send_order);
  renderMaterials("cards", state.cards, settings.active_card_id, "card");
  renderMaterials("records", state.records, settings.active_record_id, "record");
  renderMaterials("images", state.images, settings.active_image_id, "image");
  renderLogs(state.logs);
}

function toMap(items) {
  return Object.fromEntries((items || []).map((item) => [item.id, item]));
}

function setValue(id, value) {
  const node = $(id);
  if (!node) return;
  if (node.type === "checkbox") {
    node.checked = Boolean(value);
  } else {
    node.value = value ?? "";
  }
}

function readSettings() {
  return {
    enabled: $("enabled").checked,
    mode: $("mode").value,
    whitelist_groups: textToList($("whitelistGroups").value),
    blacklist_groups: textToList($("blacklistGroups").value),
    admin_qq_list: textToList($("adminQqList").value),
    send_order: [...document.querySelectorAll("[data-order-step]:checked")].map(
      (input) => input.value,
    ),
    send_interval_seconds: Number($("sendInterval").value),
    retry_enabled: $("retryEnabled").checked,
    retry_count: Number($("retryCount").value),
    retry_interval_seconds: Number($("retryInterval").value),
    delivery_compensation_enabled: $("deliveryCompensationEnabled").checked,
    delivery_confirm_wait_seconds: Number($("deliveryConfirmWait").value),
    delivery_compensation_count: Number($("deliveryCompensationCount").value),
    delivery_compensation_interval_seconds: Number($("deliveryCompensationInterval").value),
    dedupe_enabled: $("dedupeEnabled").checked,
    dedupe_minutes: Number($("dedupeMinutes").value),
    card_fallback_enabled: $("cardFallbackEnabled").checked,
    card_fallback_text: $("cardFallbackText").value,
    record_fallback_enabled: $("recordFallbackEnabled").checked,
    record_fallback_text: $("recordFallbackText").value,
    image_fallback_enabled: $("imageFallbackEnabled").checked,
    image_fallback_text: $("imageFallbackText").value,
    text_content: $("textContent").value,
    group_fallback_enabled: $("groupFallbackEnabled").checked,
    group_fallback_mode: $("groupFallbackMode").value,
    group_fallback_at: $("groupFallbackAt").checked,
    group_fallback_template: $("groupFallbackTemplate").value,
    notify_admin_private: $("notifyAdminPrivate").checked,
    notify_admin_group: $("notifyAdminGroup").checked,
    notify_group_id: $("notifyGroupId").value.trim(),
    notify_on_success: $("notifyOnSuccess").checked,
    max_logs: Number($("maxLogs").value),
    test_receiver_qq: $("testReceiver").value.trim(),
    daily_test_enabled: $("dailyTestEnabled").checked,
    daily_test_receiver_qq: $("dailyTestReceiver").value.trim(),
    daily_test_time: $("dailyTestTime").value || "09:00",
  };
}

function normalizeOrder(order) {
  const selected = Array.isArray(order) ? order : [];
  const known = new Set(defaultOrder);
  const normalized = selected.filter(
    (step, index) => known.has(step) && selected.indexOf(step) === index,
  );
  return [...normalized, ...defaultOrder.filter((step) => !normalized.includes(step))];
}

function renderOrder(order) {
  const root = $("order");
  root.innerHTML = "";
  const selected = new Set(order || []);
  for (const step of normalizeOrder(order)) {
    const row = document.createElement("div");
    row.className = "order-row";
    row.draggable = true;
    row.tabIndex = 0;
    row.dataset.orderRow = step;
    row.innerHTML = `
      <span class="drag-handle" aria-hidden="true">::</span>
      <span class="order-name">${orderLabels[step]}</span>
      <input data-order-step value="${step}" type="checkbox" ${selected.has(step) ? "checked" : ""}>
    `;
    root.appendChild(row);
  }
  root.querySelectorAll("[data-order-row]").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      row.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", row.dataset.orderRow);
    });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const sibling = event.key === "ArrowUp" ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling) return;
      if (event.key === "ArrowUp") {
        root.insertBefore(row, sibling);
      } else {
        root.insertBefore(sibling, row);
      }
      row.focus();
    });
  });
  root.ondragover = (event) => {
    event.preventDefault();
    const row = root.querySelector(".order-row.dragging");
    if (!row) return;
    const next = orderDropTarget(root, event.clientY);
    if (next) {
      root.insertBefore(row, next);
    } else {
      root.appendChild(row);
    }
  };
}

function orderDropTarget(container, y) {
  const rows = [...container.querySelectorAll("[data-order-row]:not(.dragging)")];
  return rows.reduce(
    (closest, row) => {
      const box = row.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) {
        return { offset, element: row };
      }
      return closest;
    },
    { offset: Number.NEGATIVE_INFINITY, element: null },
  ).element;
}

function renderMaterials(containerId, items, activeId, kind) {
  const root = $(containerId);
  root.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无素材";
    root.appendChild(empty);
    return;
  }
  for (const item of items) {
    const node = document.createElement("article");
    node.className = `item ${item.id === activeId ? "active" : ""}`;
    node.innerHTML = `
      <div class="item-head">
        <div class="item-title">
          <strong>${escapeHtml(item.name)}</strong>
          <span>${formatTime(item.created_at)}${kind === "record" ? ` · ${recordMeta(item)}` : ""}</span>
        </div>
        <div class="item-actions">
          <button class="small" data-action="activate" data-kind="${kind}" data-id="${item.id}" type="button">启用</button>
          <button class="small" data-action="rename" data-kind="${kind}" data-id="${item.id}" type="button">重命名</button>
          <button class="small danger" data-action="delete" data-kind="${kind}" data-id="${item.id}" type="button">删除</button>
        </div>
      </div>
      <label class="rename-line">
        名称
        <input data-name-input value="${escapeHtml(item.name)}" />
      </label>
      ${previewFor(item, kind)}
    `;
    root.appendChild(node);
  }
}

function previewFor(item, kind) {
  if (kind === "card") {
    return `
      <div class="card-preview">
        <strong>${escapeHtml(item.title || "QQ JSON 卡片")}</strong>
        <p>${escapeHtml(item.desc || item.url || "已保存原始 JSON 卡片")}</p>
      </div>
    `;
  }
  if (kind === "image") {
    return `<p class="muted">${escapeHtml(item.kind === "local" ? "本地上传图片" : item.source || "")}</p>`;
  }
  if (kind === "record" && item.mode === "unavailable_legacy") {
    return `<p class="muted">${escapeHtml(item.migration_error || "旧素材缺少原消息信息，请重新采集。")}</p>`;
  }
  if (kind === "record" && item.mode === "direct_forward") {
    return `
      <p class="muted">原消息直转，模拟把被引用的原消息转发给目标 QQ。</p>
      <p class="muted">消息 ${escapeHtml(item.source_message_id || "-")} · 来源群 ${escapeHtml(item.source_group_id || "-")}</p>
      <p class="muted">资源兜底 ${escapeHtml(item.source_forward_id || "-")} · 最近策略 ${escapeHtml(item.last_strategy || "未探测")}</p>
    `;
  }
  return `
    <p class="muted">原聊天记录资源发送，不展开、不重建嵌套内容。</p>
    <p class="muted">资源 ${escapeHtml(item.record_forward_id || item.root_forward_id || "-")} · 来源群 ${escapeHtml(item.source_group_id || item.root_forward_group_id || "-")}</p>
    <p class="muted">原消息兜底 ${escapeHtml(item.source_message_id || "-")} · 最近策略 ${escapeHtml(item.last_strategy || "未探测")}</p>
  `;
}

function recordMeta(item) {
  if (item.mode === "unavailable_legacy") {
    return "旧素材不可用";
  }
  if (item.mode === "direct_forward") {
    return "原消息直转";
  }
  return "原资源发送";
}

function renderLogs(logs) {
  const root = $("logsList");
  root.innerHTML = "";
  if (!logs.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无记录";
    root.appendChild(empty);
    return;
  }
  for (const log of logs) {
    const row = document.createElement("article");
    row.className = `log-row ${log.status}`;
    row.innerHTML = `
      <div class="log-meta">
        <span>${formatTime(log.time)}</span>
        <span>群 ${escapeHtml(log.group_id)}</span>
        <span>QQ ${escapeHtml(log.user_id)}</span>
        <span>${log.status === "success" ? "成功" : "失败"}</span>
      </div>
      <p>${escapeHtml(log.error || log.step || "发送完成")}</p>
    `;
    root.appendChild(row);
  }
}

function formatTime(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString();
}

function textSegments(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $(button.dataset.target).classList.add("active");
  });
});

$("refresh").addEventListener("click", async () => {
  await loadState();
  showToast("已刷新");
});

$("save").addEventListener("click", async () => {
  const button = $("save");
  button.disabled = true;
  try {
    await bridge.apiPost("settings", { settings: readSettings() });
    await bridge.apiPost("astrbot/max-agent-step", {
      max_agent_step: Number($("maxAgentStep").value),
    });
    await loadState();
    showToast("设置已保存");
  } catch (error) {
    showToast(error.message || "保存失败");
  } finally {
    button.disabled = false;
  }
});

$("clearLogs").addEventListener("click", async () => {
  const button = $("clearLogs");
  button.disabled = true;
  try {
    await bridge.apiPost("logs/clear", {});
    await loadState();
    showToast("最近记录已清空");
  } catch (error) {
    showToast(error.message || "清空失败");
  } finally {
    button.disabled = false;
  }
});

document.body.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const { action, kind, id } = button.dataset;
  button.disabled = true;
  try {
    if (action === "activate") {
      await bridge.apiPost("activate", { kind, id });
      showToast(`${labels[kind]}已启用`);
    }
    if (action === "rename") {
      const input = button.closest(".item")?.querySelector("[data-name-input]");
      const name = input?.value?.trim();
      if (!name) {
        showToast("名称不能为空");
        return;
      }
      await bridge.apiPost("rename", { kind, id, name });
      showToast(`${labels[kind]}已重命名`);
    }
    if (action === "delete") {
      await bridge.apiPost("delete", { kind, id });
      showToast(`${labels[kind]}已删除`);
    }
    await loadState();
  } catch (error) {
    showToast(error.message || "操作失败");
  } finally {
    button.disabled = false;
  }
});

$("imageUpload").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  await bridge.upload("image/upload", file);
  event.target.value = "";
  await loadState();
  showToast("图片已上传并启用");
});

$("testSend").addEventListener("click", async () => {
  const qq = $("testReceiver").value.trim();
  $("testResult").textContent = "发送中";
  try {
    await bridge.apiPost("settings", { settings: readSettings() });
    const result = await bridge.apiPost("test", { qq });
    $("testResult").textContent = result.sent
      ? `测试已发送，实际发送 ${result.sent_count} 项`
      : "当前没有可发送项，已跳过";
  } catch (error) {
    $("testResult").textContent = error.message;
  }
});

try {
  await loadState();
} catch (error) {
  showToast(error.message);
}
