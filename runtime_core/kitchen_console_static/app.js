const $ = (selector) => document.querySelector(selector);

function ingredientAmountDisplay(item = {}) {
  let amount = String(item.amount ?? "").trim() || "适量";
  const unit = String(item.unit ?? "").trim();
  const qualitative = amount.match(/^(适量|少量|少许|若干|按口味)/);
  if (qualitative) return amount === qualitative[1] || /^(适量|少量|少许|若干)[块个片瓣勺碗份把圈]+$/.test(amount) ? qualitative[1] : amount;
  if (!unit) return amount;
  while (amount.endsWith(unit + unit)) amount = amount.slice(0, -unit.length);
  if (amount.endsWith(unit) || /(?:\d|[一二两三四五六七八九十半])\s*(?:毫升|毫克|千克|公斤|汤匙|茶匙|汤勺|小勺|勺|匙|碗|杯|克|斤|两|个|块|片|瓣|份|把|圈|kg|g|ml)$/i.test(amount)) return amount;
  return amount + unit;
}
let selectedImageDataUrl = "";
let ingredients = [];
let statusTimer = null;
let statusRefreshInFlight = null;
let currentKitchen = null;
let cookingMutationPending = false;
let activeRecipeId = "";
let selectedRecipeCategory = "";
let lastRecommendationRequest = null;
let recommendationsPending = false;
let videoImportId = "";
let videoImportStage = "";
let videoImportDraft = null;
let videoImportFile = null;
let videoImportFileDuration = null;
let videoImportPending = false;
let videoImportDraftDirty = false;
let videoImportPollTimer = null;
let videoImportPollErrors = 0;
let videoImportRequest = null;
let videoImportPreviewSignature = "";
let importedRecipes = [];
let importedRecipesExpanded = false;

const VIDEO_IMPORT_STAGE_LABELS = {
  queued: "排队中",
  fetching: "读取视频",
  analyzing: "识别画面与语音",
  structuring: "整理菜谱",
  ready: "等待确认",
  confirmed: "已保存",
  failed: "处理失败",
  cancelled: "已取消"
};
const VIDEO_IMPORT_STAGE_ORDER = ["fetching", "analyzing", "structuring", "ready"];
const VIDEO_IMPORT_TERMINAL_STAGES = new Set(["ready", "confirmed", "failed", "cancelled"]);
const VIDEO_MAX_BYTES = 100 * 1024 * 1024;
const VIDEO_MAX_SECONDS = 3 * 60;

const stateLabels = {
  IDLE: "空闲", COLLECTING_REQUEST: "收集需求", COLLECTING_INGREDIENTS: "确认食材",
  COLLECTING_PREFERENCES: "确认偏好", SEARCHING_RECIPES: "生成方案", PRESENTING_CANDIDATES: "等待选择",
  WAITING_RECIPE_CONFIRMATION: "确认菜谱", WAITING_MEAT_THAW: "等待食材确认", COOKING: "烹饪中",
  PAUSED: "已暂停", COMPLETED: "已完成", CANCELLED: "已取消"
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const rawError = payload.error ?? payload.message;
    const message = typeof rawError === "string"
      ? rawError
      : (rawError && typeof rawError.message === "string" ? rawError.message : `请求失败（${response.status}）`);
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function jsonPost(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function setDot(id, state) {
  const dot = $(id);
  dot.className = `status-dot ${state}`;
}

function formatTimer(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60).toString().padStart(2, "0");
  const rest = Math.floor(value % 60).toString().padStart(2, "0");
  return `${minutes}:${rest}`;
}

function updateStatus(data) {
  const { robot, camera, kitchen, timers } = data;
  currentKitchen = kitchen;
  const robotBusy = robot.state === "executing";
  $("#robot-state").textContent = robotBusy ? "执行中" : "在线待机";
  $("#robot-detail").textContent = robot.display || robot.action || "等待任务";
  setDot("#robot-dot", robotBusy ? "busy" : "online");
  renderRobotFeedback(robot);

  const cameraError = camera.state === "error";
  const cameraBusy = camera.state === "capturing";
  const cameraReady = camera.state === "ready";
  $("#camera-state").textContent = `摄像头：${cameraError ? "暂不可用" : (cameraBusy ? "读取中" : "在线待机")}`;
  setDot("#camera-dot", cameraError ? "error" : (cameraBusy ? "busy" : (cameraReady ? "online" : "")));

  const timer = timers[0];
  if (timer) {
    $("#timer-state").textContent = formatTimer(timer.remaining_seconds);
    $("#timer-detail").textContent = `${timer.label} · ${timer.state === "paused" ? "已暂停" : "进行中"}`;
    setDot("#timer-dot", timer.state === "paused" ? "busy" : "online");
  } else {
    $("#timer-state").textContent = "等待启动";
    $("#timer-detail").textContent = "可以从做菜步骤启动";
    setDot("#timer-dot", "");
  }

  const state = stateLabels[kitchen.state] || kitchen.state || "空闲";
  $("#kitchen-state").textContent = state;
  $("#hero-summary").textContent = kitchen.recipe?.name
    ? `${kitchen.recipe.name} · ${state}`
    : (kitchen.candidates?.length ? `已有 ${kitchen.candidates.length} 个菜谱方案等待选择` : "机器人、摄像头和计时状态都在这里。");
  const completed = kitchen.state === "COMPLETED" && Boolean(kitchen.recipe);
  const hasCooking = ["WAITING_MEAT_THAW", "COOKING", "PAUSED"].includes(kitchen.state) && Boolean(kitchen.recipe && kitchen.current_step);
  const importedCooking = Boolean(kitchen.recipe?.import_metadata || kitchen.recipe?.source_type === "video");
  $("#empty-cooking").classList.toggle("hidden", hasCooking || completed);
  $("#active-cooking").classList.toggle("hidden", !hasCooking);
  $("#completed-cooking").classList.toggle("hidden", !completed);
  if (completed) $("#completed-recipe-name").textContent = `${kitchen.recipe.name}完成了`;
  if (hasCooking) {
    $("#recipe-name").textContent = kitchen.recipe.name;
    $("#recipe-meta").textContent = `${kitchen.recipe.estimated_minutes || "?"} 分钟 · ${kitchen.recipe.difficulty || "家常"}`;
    $("#step-number").textContent = `第 ${kitchen.current_step.number} / ${kitchen.current_step.total} 步`;
    $("#step-instruction").textContent = kitchen.current_step.instruction;
    const details = [];
    if (kitchen.current_step.heat_level) details.push(`火力：${kitchen.current_step.heat_level}`);
    if (kitchen.current_step.duration_seconds) details.push(`建议计时：${formatTimer(kitchen.current_step.duration_seconds)}`);
    if (kitchen.current_step.safety_note) details.push(`注意：${kitchen.current_step.safety_note}`);
    $("#step-details").textContent = details.join(" · ");
    const parallel = kitchen.parallel_step;
    $("#parallel-step").classList.toggle("hidden", !parallel);
    if (parallel) {
      $("#parallel-step-title").textContent = `同步进行第 ${parallel.number} 步${parallel.completed ? "（已完成）" : ""}`;
      $("#parallel-step-instruction").textContent = parallel.instruction;
    }
    $("#step-progress").style.width = `${Math.round(kitchen.current_step.number / kitchen.current_step.total * 100)}%`;
    const waitingForIngredient = kitchen.state === "WAITING_MEAT_THAW";
    if (waitingForIngredient) {
      $("#step-number").textContent = "开始前：确认食材状态";
      $("#step-instruction").textContent = "请先确认肉类或水产是新鲜食材，或已经完全解冻。确认后会从第1步开始，不会跳过准备步骤。";
      $("#step-details").textContent = "尚未解冻时请先处理食材，准备好后再开始。";
      $("#step-progress").style.width = "0%";
    }
  }
  syncCookingNavigation();
  const actionPanel = $("#imported-cooking-actions");
  const endTaskButton = $("#cooking-end-task");
  if (actionPanel) {
    const importedGuidance = importedCooking && !completed;
    actionPanel.classList.toggle("hidden", !importedGuidance);
    const timedStep = Boolean(kitchen.current_step?.duration_seconds);
    const hasTimer = Boolean(kitchen.timer);
    $("#cooking-start-timer").classList.toggle("hidden", !importedGuidance || kitchen.state !== "COOKING" || !timedStep || hasTimer);
    $("#cooking-confirm-done").classList.toggle("hidden", !importedGuidance || kitchen.state !== "COOKING");
    $("#cooking-pause-resume").classList.toggle("hidden", !importedGuidance || !["COOKING", "PAUSED"].includes(kitchen.state));
    $("#cooking-pause-resume").textContent = kitchen.state === "PAUSED" ? "恢复指导" : "暂停指导";
    $("#cooking-cancel-timer").classList.toggle("hidden", !importedGuidance || !hasTimer);
    $("#cooking-fresh-ingredients").classList.toggle("hidden", !importedGuidance || kitchen.state !== "WAITING_MEAT_THAW");
  }
  if (endTaskButton) {
    endTaskButton.classList.toggle("hidden", !kitchen.session_active || completed || kitchen.state === "CANCELLED");
  }
  $("#scope-notice").textContent = data.scope_notice;
}

const robotActionLabels = {
  idle_wait: "原地等待", speak: "语音播报", wave_hand: "挥手", handshake: "握手",
  fist_bump: "碰拳", high_five: "击掌", nod: "点头", shake_head: "摇头",
  show_smile: "微笑", show_concern: "关心", encourage_gesture: "鼓励手势", hug: "拥抱",
  turn_left: "左转", turn_right: "右转", step_forward: "前进一步", step_back: "后退一步",
  stop: "停止运动", breathing_guide: "呼吸引导", goodbye: "送别"
};
const robotLightLabels = {
  off: "关闭", white: "白色常亮", blue: "蓝色常亮", green: "绿色常亮",
  yellow: "黄色常亮", red: "红色常亮", warm_white: "暖白低亮",
  green_dynamic: "绿色动态", blue_dynamic: "蓝色动态", rainbow: "彩色庆祝"
};
const robotExpressionLabels = {
  neutral: "平静", happy: "开心", curious: "好奇", focused: "专注", confident: "自信",
  alert: "留意", waiting: "等待", confused: "疑惑", excited: "兴奋", warning: "提醒"
};

let robotFeedbackStreamId = null;
let robotFeedbackSequence = 0;
let robotFeedbackSupported = false;

function renderRobotFeedback(robot = {}) {
  robotFeedbackSupported = Array.isArray(robot.feedback_events);
  if (!robotFeedbackSupported) return;
  if (robot.feedback_stream_id !== robotFeedbackStreamId) {
    robotFeedbackStreamId = robot.feedback_stream_id;
    robotFeedbackSequence = 0;
    $("#feedback-action").textContent = "等待任务";
    $("#feedback-light").textContent = "—";
    $("#feedback-expression").textContent = "—";
    $("#feedback-display").textContent = "等待任务";
    $("#feedback-speech").textContent = "选择菜谱或切换步骤后，机器人语音会显示在这里。";
  }
  const event = robot.feedback_events.reduce((latest, item) =>
    !latest || item.sequence > latest.sequence ? item : latest, null);
  if (!event || event.sequence <= robotFeedbackSequence) return;
  robotFeedbackSequence = event.sequence;
  $("#feedback-action").textContent = robotActionLabels[event.action] || event.action || "未提供";
  $("#feedback-light").textContent = robotLightLabels[event.led_effect] || event.led_effect || "未提供";
  $("#feedback-expression").textContent = robotExpressionLabels[event.expression] || event.expression || "未提供";
  $("#feedback-display").textContent = event.display || "未提供";
  $("#feedback-speech").textContent = event.speech || "未提供";
}

function syncCookingNavigation() {
  const kitchen = currentKitchen || {};
  const step = kitchen.current_step;
  const cooking = kitchen.state === "COOKING" && Boolean(step && kitchen.recipe);
  const waiting = kitchen.state === "WAITING_MEAT_THAW" && Boolean(step && kitchen.recipe);
  const paused = kitchen.state === "PAUSED" && Boolean(step && kitchen.recipe);
  $("#previous-step").disabled = cookingMutationPending || !cooking || step.number <= 1;
  $("#next-step").disabled = cookingMutationPending || !(cooking || waiting || paused);
  $("#next-step").textContent = waiting ? "新鲜或已完全解冻，开始" : paused ? "恢复指导" :
    (cooking && step.number >= step.total ? "完成 ✓" : "下一步 ›");
}

async function refreshStatus() {
  if (statusRefreshInFlight) return statusRefreshInFlight;
  statusRefreshInFlight = (async () => {
    try { updateStatus(await api("/api/status")); }
    catch (error) { showToast(error.message); }
    finally { statusRefreshInFlight = null; }
  })();
  return statusRefreshInFlight;
}

async function navigateCookingStep(direction, button) {
  if (cookingMutationPending) return;
  if (direction === "next" && currentKitchen?.state === "WAITING_MEAT_THAW") return sendCookingAction("fresh_ingredients", button);
  if (direction === "next" && currentKitchen?.state === "PAUSED") return sendCookingAction("resume", button);
  if (currentKitchen?.state !== "COOKING") {
    syncCookingNavigation();
    showToast("当前没有正在进行的烹饪任务，请先选择菜谱或确认食材状态。");
    return;
  }
  cookingMutationPending = true;
  syncCookingNavigation();
  try {
    const result = await api("/api/cooking/step", jsonPost({ direction }));
    await refreshStatus();
    if (!robotFeedbackSupported) showToast(result.message || (direction === "previous" ? "已返回上一步" : (currentKitchen?.state === "COMPLETED" ? "这道菜完成了" : "已跳到下一步")));
  } catch (error) {
    showToast(error.message);
  } finally {
    cookingMutationPending = false;
    syncCookingNavigation();
  }
}

async function captureServerCamera() {
  const button = $("#capture-server");
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    const data = await api("/api/camera/capture", { method: "POST" });
    $("#camera-preview").src = data.image_data_url;
    $("#camera-frame").classList.add("has-image");
    selectedImageDataUrl = data.image_data_url;
    await refreshStatus();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "刷新画面"; }
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 8 * 1024 * 1024) return reject(new Error("图片不能超过 8 MB"));
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("无法读取图片"));
    reader.readAsDataURL(file);
  });
}

async function analyzeSelectedImage() {
  if (!selectedImageDataUrl) return showToast("请先拍照或选择一张图片");
  $("#vision-loading").classList.remove("hidden");
  try {
    const result = await api("/api/ingredients/analyze", jsonPost({ image_data_url: selectedImageDataUrl }));
    ingredients = result.ingredients || [];
    renderIngredients(result);
    $("#ingredient-results").classList.remove("hidden");
    $("#ingredient-results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { showToast(error.message); }
  finally { $("#vision-loading").classList.add("hidden"); }
}

function renderIngredients(result = {}) {
  $("#vision-summary").textContent = result.summary || "请核对识别结果，可删除或补充食材。";
  $("#ingredient-chips").innerHTML = ingredients.map((item, index) => `
    <span class="ingredient-chip"><strong>${escapeHtml(item.name)}</strong><em>${escapeHtml(item.confidence || "手动")}</em><button type="button" data-remove="${index}" aria-label="删除${escapeHtml(item.name)}">×</button></span>
  `).join("");
  const uncertain = result.uncertain_items || [];
  $("#uncertain-items").classList.toggle("hidden", uncertain.length === 0);
  $("#uncertain-items").textContent = uncertain.length ? `暂时无法确认：${uncertain.join("、")}。请不要把它们直接加入方案。` : "";
}

async function requestRecommendations() {
  if (!ingredients.length) return showToast("请至少确认一种食材");
  return submitRecommendations({
    path: "/api/recommendations",
    body: {
      ingredients: ingredients.map((item) => item.name),
      servings: Number($("#servings").value),
      taste: $("#taste").value
    }
  });
}

async function submitRecommendations(request) {
  if (recommendationsPending) return;
  recommendationsPending = true;
  lastRecommendationRequest = request;
  const button = $("#recommend");
  button.disabled = true;
  button.textContent = "生成中…";
  renderRecipes({ recommendation_status: "loading", message: request.body.dish_name
    ? `正在生成“${request.body.dish_name}”的详细菜谱，请稍候。`
    : "正在准备 2–3 份菜谱方案，第一份会用上所有确认食材，请稍候。" });
  $("#recommendations").classList.remove("hidden");
  $("#recommendations").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const result = await api(request.path, jsonPost(request.body));
    renderRecipes(result, request.body.servings);
    await refreshStatus();
  } catch (error) {
    renderRecipes({ recommendation_status: "failed", recommendation_error: {
      message: error instanceof TypeError
        ? "无法连接局域网控制台，请检查网络和终端服务是否运行，然后重试。"
        : `生成请求未完成：${error.message || "请稍后重试"}`,
      retryable: true
    } });
  } finally {
    recommendationsPending = false;
    button.disabled = false;
    button.textContent = "生成菜谱方案";
  }
}

function renderRecipes(result, requestedServings = Number($("#servings").value)) {
  const cards = result.candidates || [];
  const servings = Number(requestedServings);
  const loading = result.recommendation_status === "loading";
  const failed = !loading && !cards.length;
  const labels = { mock: "本地菜谱", local_cache: "已保存菜谱", ai_generated: "AI 生成菜谱" };
  $("#provider-label").textContent = loading ? "生成中" : (failed ? "生成未成功" : (labels[result.provider_mode] || "菜谱方案"));
  $("#recommendation-message").textContent = result.recommendation_error?.message || result.message || (failed ? "未收到可用菜谱，请重试。" : "");
  $("#recipe-cards").setAttribute("aria-busy", String(loading));
  if (loading) {
    $("#recipe-cards").innerHTML = "";
    return;
  }
  $("#recipe-cards").innerHTML = cards.length ? cards.map((item, index) => `
    <article class="recipe-card" role="button" tabindex="0" data-recipe-id="${escapeHtml(item.candidate_id)}" data-servings="${Number.isInteger(servings) && servings >= 1 && servings <= 6 ? servings : 1}">
      <span class="number">0${index + 1}</span>
      <h3>${escapeHtml(item.title)}</h3>
      <span class="meta">${escapeHtml(item.estimated_minutes || "?")} 分钟 · ${escapeHtml(item.difficulty || "家常")}</span>
      <p>${escapeHtml(item.summary || item.match_reason || "根据现有食材生成的方案。")}</p>
      <p>已有：${escapeHtml((item.main_ingredients || []).join("、") || "请查看详情")}</p>
      <p class="missing">没用到：${escapeHtml((item.unused_ingredients || []).join("、") || "无")}</p>
    </article>
  `).join("") : `<div class="empty-state"><div><h3>未能生成菜谱方案</h3><p>已保留你的食材和人数设置，无需重新拍照。</p>${lastRecommendationRequest ? `<button class="primary-button" type="button" data-retry-recommendations>${result.recommendation_error?.retryable === false ? "更新配置后重试" : "重新生成"}</button>` : ""}</div></div>`;
}

function errorText(error, fallback = "请求未完成，请稍后重试。") {
  if (!error) return fallback;
  if (typeof error === "string") return error;
  if (typeof error.message === "string" && error.message.trim()) return error.message;
  return fallback;
}

function safeHttpUrl(value) {
  const Url = typeof URL === "function"
    ? URL
    : (typeof window !== "undefined" && typeof window.URL === "function" ? window.URL : null);
  if (!Url) return null;
  let parsed;
  try { parsed = new Url(String(value || "").trim()); }
  catch (_) { return null; }
  const host = String(parsed.hostname || "").toLowerCase();
  if (!(parsed.protocol === "http:" || parsed.protocol === "https:")) return null;
  if (!host || parsed.username || parsed.password) return null;
  if (host === "localhost" || host === "0.0.0.0" || host === "::1" || host === "[::1]" || host.endsWith(".local") || /^127\./.test(host) || /^169\.254\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[0-1])\./.test(host)) return null;
  return parsed;
}

function extractShareUrl(value) {
  const candidates = String(value || "").match(/https?:\/\/[^\s<>"'。，！？；：）》】》]+/gi) || [];
  for (const candidate of candidates) {
    const cleaned = candidate.replace(/[.,!?;:，。！？；：）》】》]+$/g, "");
    if (safeHttpUrl(cleaned)) return cleaned;
  }
  return "";
}

function normalizeVideoImportStage(stage, payload = {}) {
  const raw = String(stage || payload.status || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  const aliases = {
    pending: "queued", created: "queued", processing: "analyzing", downloading: "fetching", acquiring: "fetching",
    downloaded: "analyzing", transcribing: "analyzing", analyzing_video: "analyzing",
    structuring_recipe: "structuring", review: "ready", draft_ready: "ready", complete: "ready", completed: "ready",
    success: "ready", canceled: "cancelled", error: "failed"
  };
  if (aliases[raw]) return aliases[raw];
  if (VIDEO_IMPORT_STAGE_LABELS[raw]) return raw;
  if (payload.error) return "failed";
  if (payload.draft) return "ready";
  return "queued";
}

function isVideoImportTerminal(stage) {
  return VIDEO_IMPORT_TERMINAL_STAGES.has(normalizeVideoImportStage(stage));
}

function formatFileSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function formatVideoDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function setVideoImportValidation(message = "") {
  const target = $("#video-import-validation");
  if (target) target.textContent = message;
}

function updateVideoImportButtons() {
  const submit = $("#video-import-submit");
  const cancel = $("#video-import-cancel");
  if (!submit || !cancel) return;
  const active = videoImportPending || Boolean(videoImportId && !isVideoImportTerminal(videoImportStage));
  submit.disabled = active;
  submit.textContent = active ? "处理中…" : (videoImportId || ["failed", "cancelled"].includes(videoImportStage) ? "重新解析" : "解析为菜谱");
  cancel.classList.toggle("hidden", !videoImportId || isVideoImportTerminal(videoImportStage));
}

function videoImportFieldOrigin(item, metadata, path) {
  const fieldSources = metadata && typeof (metadata.field_sources || metadata.field_origins) === "object"
    ? (metadata.field_sources || metadata.field_origins) : {};
  const annotationList = Array.isArray(metadata?.annotations)
    ? metadata.annotations
    : (Array.isArray(metadata?.field_annotations) ? metadata.field_annotations : []);
  const annotation = annotationList.find((item) => String(item?.path || item?.field || "") === path);
  const mappedFields = Object.entries(fieldSources).filter(([key]) => key === path || key.startsWith(`${path}.`) || key.startsWith(`${path.replace(/\.(\d+)/g, "[$1]")}.`)).map(([, value]) => value);
  const sourceValue = value => typeof value === "object" ? (value?.origin || value?.source || value?.type) : value;
  const aiField = mappedFields.find(value => /ai|infer|model/i.test(String(sourceValue(value) || "")));
  if (aiField) return "AI 补全";
  const mapped = fieldSources[path] || mappedFields[0];
  const raw = item?.origin || item?.source || item?.source_type
    || (typeof mapped === "object" ? (mapped.origin || mapped.source || mapped.type) : mapped)
    || annotation?.origin || annotation?.source || annotation?.type;
  const normalized = String(raw || "").toLowerCase();
  if (!normalized) return "";
  if (normalized.includes("user") || normalized.includes("manual")) return "用户修改";
  if (normalized.includes("rule") || normalized.includes("default")) return "规则补全";
  if (normalized.includes("ai") || normalized.includes("infer") || normalized.includes("model")) return "AI 补全";
  if (normalized.includes("video") || normalized.includes("source")) return "视频提取";
  return String(raw);
}

function normalizeVideoDraft(draft = {}) {
  const raw = draft && typeof draft === "object" ? draft : {};
  const equipment = Array.isArray(raw.equipment)
    ? raw.equipment.map((item) => String(item || "").trim()).filter(Boolean)
    : String(raw.equipment || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const ingredients = Array.isArray(raw.ingredients) ? raw.ingredients.map((item) => ({
    name: String(item?.name || ""),
    amount: item?.amount ?? "",
    unit: String(item?.unit || ""),
    optional: Boolean(item?.optional),
    origin: item?.origin || item?.source || item?.source_type || ""
  })) : [];
  const steps = Array.isArray(raw.steps) ? raw.steps.map((item) => ({
    instruction: String(item?.instruction || ""),
    duration_seconds: item?.duration_seconds ?? "",
    heat_level: String(item?.heat_level || ""),
    safety_note: String(item?.safety_note || ""),
    origin: item?.origin || item?.source || item?.source_type || ""
  })) : [];
  const servings = Number(raw.servings);
  return {
    name: String(raw.name || ""),
    servings: Number.isInteger(servings) && servings >= 1 && servings <= 6 ? servings : 1,
    ingredients,
    equipment,
    steps,
    import_metadata: raw.import_metadata && typeof raw.import_metadata === "object" ? raw.import_metadata : {}
  };
}

function videoDraftSourceNote(draft) {
  const metadata = draft?.import_metadata || {};
  const bits = [];
  const source = metadata.source && typeof metadata.source === "object" ? metadata.source : {};
  const platform = metadata.platform || metadata.source_platform || metadata.source_name || source.platform;
  const title = metadata.source_title || metadata.title || source.title;
  if (platform) bits.push(String(platform));
  if (title) bits.push(String(title));
  if (metadata.source_url || source.source_url) bits.push(`来源：${String(metadata.source_url || source.source_url)}`);
  if ((metadata.assumptions && metadata.assumptions.length) || (metadata.ai_completed_fields && metadata.ai_completed_fields.length) || metadata.field_origins || metadata.field_sources || metadata.annotations || metadata.field_annotations) bits.push("含 AI / 规则补全");
  return bits.join(" · ");
}

function renderVideoDraft(draft) {
  const normalized = normalizeVideoDraft(draft);
  videoImportDraft = normalized;
  const panel = $("#video-import-draft");
  if (!panel) return;
  panel.classList.remove("hidden");
  $("#video-draft-name").value = normalized.name;
  $("#video-draft-servings").value = String(normalized.servings);
  $("#video-draft-equipment").value = normalized.equipment.join("\n");
  $("#video-draft-source-note").textContent = videoDraftSourceNote(normalized);
  $("#video-draft-notice").textContent = normalized.import_metadata?.notice || "这是 AI 根据视频整理的草稿。请确认关键用量、火候和时间，再保存。";
  const metadata = normalized.import_metadata || {};
  const completeButton = $("#video-draft-complete");
  if (completeButton) completeButton.classList.toggle("hidden", !(metadata.completion_needed || normalized.ingredients.some(item => item.amount === "" || (!item.unit.trim() && !/适量|少量|按口味|一圈/.test(String(item.amount)))) || (metadata.completion_needed === undefined && metadata.quality_issues?.length && !metadata.ai_completed)));
  $("#video-draft-ingredients").innerHTML = normalized.ingredients.map((item, index) => {
    const origin = videoImportFieldOrigin(item, metadata, `ingredients.${index}`);
    return `<div class="draft-ingredient-row" data-draft-ingredient="${index}">
      <label class="field-label">名称<input type="text" maxlength="60" data-draft-field="ingredient-name" value="${escapeHtml(item.name)}"></label>
      <label class="field-label">用量<input type="text" maxlength="30" data-draft-field="ingredient-amount" value="${escapeHtml(item.amount)}"></label>
      <label class="field-label">单位<input type="text" maxlength="20" data-draft-field="ingredient-unit" value="${escapeHtml(item.unit)}"></label>
      <label class="draft-checkbox"><input type="checkbox" data-draft-field="ingredient-optional" ${item.optional ? "checked" : ""}> 可选</label>
      ${origin ? `<small class="draft-field-source">${escapeHtml(origin)}</small>` : ""}
      <button class="text-button draft-remove-button" type="button" data-remove-draft-ingredient="${index}">移除</button>
    </div>`;
  }).join("");
  $("#video-draft-steps").innerHTML = normalized.steps.map((item, index) => {
    const origin = videoImportFieldOrigin(item, metadata, `steps.${index}`);
    return `<div class="draft-step-row" data-draft-step="${index}">
      <div class="draft-step-number">${index + 1}</div>
      <label class="field-label draft-step-instruction">操作说明<textarea rows="2" maxlength="500" data-draft-field="step-instruction">${escapeHtml(item.instruction)}</textarea></label>
      <label class="field-label">时间（秒）<input type="number" min="0" max="86400" step="1" inputmode="numeric" data-draft-field="step-duration" value="${escapeHtml(item.duration_seconds)}"></label>
      <label class="field-label">火候<input type="text" maxlength="30" data-draft-field="step-heat" value="${escapeHtml(item.heat_level)}"></label>
      <label class="field-label draft-step-safety">注意事项<input type="text" maxlength="160" data-draft-field="step-safety" value="${escapeHtml(item.safety_note)}"></label>
      ${origin ? `<small class="draft-field-source">${escapeHtml(origin)}</small>` : ""}
      <button class="text-button draft-remove-button" type="button" data-remove-draft-step="${index}">移除</button>
    </div>`;
  }).join("");
}

function renderVideoImportStages(stage, failedStage = "queued") {
  const current = normalizeVideoImportStage(stage);
  const progressStage = current === "failed" ? normalizeVideoImportStage(failedStage) : current;
  const currentIndex = progressStage === "queued" ? 0 : (current === "confirmed" ? VIDEO_IMPORT_STAGE_ORDER.length : VIDEO_IMPORT_STAGE_ORDER.indexOf(progressStage));
  document.querySelectorAll("#video-import-stages [data-video-stage]").forEach((item, index) => {
    const itemStage = item.dataset.videoStage;
    item.dataset.state = current === "failed" ? (index < Math.max(0, currentIndex) ? "done" : (index === Math.max(0, currentIndex) ? "error" : "pending")) : (index < currentIndex ? "done" : (index === currentIndex ? "current" : "pending"));
    if (index === currentIndex && !["failed", "cancelled", "confirmed"].includes(current)) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    if (itemStage === "ready" && ["confirmed"].includes(current)) item.dataset.state = "done";
  });
}

function renderVideoImportPreview(result, stage) {
  const panel = $("#video-import-preview");
  if (!panel) return;
  const preview = result.partial_preview;
  const active = !isVideoImportTerminal(stage);
  const ingredients = Array.isArray(preview?.ingredients) ? preview.ingredients.slice(0, 64).filter(item => typeof item?.name === "string" && item.name.trim()) : [];
  const steps = Array.isArray(preview?.steps) ? preview.steps.slice(0, 64).filter(item => typeof item?.instruction === "string" && item.instruction.trim()) : [];
  const visible = active && (ingredients.length > 0 || steps.length > 0);
  panel.classList.toggle("hidden", !visible);
  if (!visible) {
    videoImportPreviewSignature = "";
    return;
  }
  const name = typeof preview.name === "string" ? preview.name.slice(0, 80) : "";
  const signature = JSON.stringify([name, ingredients, steps]);
  if (signature === videoImportPreviewSignature) return;
  videoImportPreviewSignature = signature;
  $("#video-preview-title").textContent = name || "正在整理菜谱";
  $("#video-preview-count").textContent = `${ingredients.length} 项食材 · ${steps.length} 个步骤`;
  $("#video-preview-ingredients").textContent = ingredients.map(item => [item.name.slice(0, 60), ingredientAmountDisplay(item).slice(0, 50)].join(" ")).join("、");
  $("#video-preview-steps").innerHTML = steps.map(item => `<li>${escapeHtml(item.instruction.slice(0, 500))}</li>`).join("");
}

function renderVideoImportStatus(result = {}) {
  if (result.id) videoImportId = String(result.id);
  const stage = normalizeVideoImportStage(result.stage, result);
  const failedStage = result.failed_stage || videoImportStage;
  videoImportStage = stage;
  if (result.draft && (!videoImportDraftDirty || !videoImportDraft)) {
    renderVideoDraft(result.draft);
  }
  const label = VIDEO_IMPORT_STAGE_LABELS[stage] || "处理中";
  const error = result.error;
  const message = errorText(error, result.message || (stage === "ready" ? "请核对下面的菜谱草稿，确认后才会保存。" : `正在${label}，请稍候。`));
  const status = $("#video-import-status");
  if (status) {
    status.classList.remove("hidden");
    status.dataset.importId = videoImportId;
  }
  $("#video-import-state").textContent = label;
  $("#video-import-status-title").textContent = label;
  $("#video-import-status-message").textContent = message;
  setDot("#video-import-dot", stage === "failed" ? "error" : (isVideoImportTerminal(stage) ? "online" : "busy"));
  renderVideoImportStages(stage, failedStage);
  renderVideoImportPreview(result, stage);
  const elapsed = $("#video-import-elapsed");
  if (elapsed) {
    const seconds = Number(result.metrics?.elapsed_seconds);
    const valid = Number.isFinite(seconds) && seconds >= 0;
    elapsed.classList.toggle("hidden", !valid);
    if (valid) elapsed.textContent = `${isVideoImportTerminal(stage) ? "处理用时" : "已等待"} ${Math.round(seconds)} 秒`;
  }
  if (stage === "cancelled") $("#video-import-draft").classList.add("hidden");
  updateVideoImportButtons();
}

function stopVideoImportPolling() {
  if (videoImportPollTimer !== null) clearTimeout(videoImportPollTimer);
  videoImportPollTimer = null;
}

function startVideoImportPolling(id) {
  stopVideoImportPolling();
  videoImportPollErrors = 0;
  const poll = async () => {
    if (!id || id !== videoImportId) return;
    try {
      const result = await api(`/api/video-imports/${encodeURIComponent(id)}`);
      if (id !== videoImportId) return;
      videoImportPollErrors = 0;
      renderVideoImportStatus(result);
      if (isVideoImportTerminal(videoImportStage)) {
        stopVideoImportPolling();
        videoImportPending = false;
        updateVideoImportButtons();
        if (videoImportStage === "ready") showToast("视频菜谱已整理好，请核对后保存");
        return;
      }
      videoImportPollTimer = setTimeout(poll, 1000);
    } catch (error) {
      videoImportPollErrors += 1;
      stopVideoImportPolling();
      videoImportPending = false;
      renderVideoImportStatus({ id, stage: "failed", error: { message: `无法查询导入进度：${errorText(error)}` } });
      updateVideoImportButtons();
    }
  };
  poll();
}

function inspectVideoDuration(file) {
  const createElement = typeof document !== "undefined" && typeof document.createElement === "function" ? document.createElement.bind(document) : null;
  const Url = typeof URL === "function" ? URL : (typeof window !== "undefined" ? window.URL : null);
  if (!createElement || !Url || typeof Url.createObjectURL !== "function") return Promise.resolve(null);
  return new Promise((resolve, reject) => {
    const objectUrl = Url.createObjectURL(file);
    const video = createElement("video");
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      if (typeof Url.revokeObjectURL === "function") Url.revokeObjectURL(objectUrl);
      callback(value);
    };
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = Number(video.duration);
      if (Number.isFinite(duration) && duration > VIDEO_MAX_SECONDS) finish(reject, new Error("视频不能超过 3 分钟，请剪辑后重试。"));
      else finish(resolve, Number.isFinite(duration) ? duration : null);
    };
    video.onerror = () => finish(reject, new Error("无法读取视频时长，请选择 MP4 或 MOV 文件。"));
    video.src = objectUrl;
  });
}

async function validateVideoFile(file) {
  if (!file) return null;
  const filename = String(file.name || "").toLowerCase();
  const type = String(file.type || "").toLowerCase();
  if (!/(video\/mp4|video\/quicktime)/.test(type) && !/\.(mp4|mov)$/.test(filename)) throw new Error("只支持 MP4 或 MOV 视频。" );
  if (Number(file.size) > VIDEO_MAX_BYTES) throw new Error("视频不能超过 100 MB，请压缩或剪辑后重试。" );
  const duration = await inspectVideoDuration(file);
  if (duration !== null && duration > VIDEO_MAX_SECONDS) throw new Error("视频不能超过 3 分钟，请剪辑后重试。" );
  videoImportFileDuration = duration;
  return file;
}

async function handleVideoFileChange(event) {
  const file = event.target.files?.[0] || null;
  videoImportFile = null;
  videoImportFileDuration = null;
  $("#video-file-meta").textContent = "";
  if (!file) {
    $("#video-file-name").textContent = "MP4 / MOV · 不超过 100 MB、3 分钟";
    return;
  }
  try {
    await validateVideoFile(file);
    videoImportFile = file;
    $("#video-file-name").textContent = file.name || "已选择视频";
    $("#video-file-meta").textContent = `${formatFileSize(file.size)}${videoImportFileDuration === null ? "" : ` · ${formatVideoDuration(videoImportFileDuration)}`} · 可上传`;
    setVideoImportValidation("");
  } catch (error) {
    event.target.value = "";
    $("#video-file-name").textContent = "MP4 / MOV · 不超过 100 MB、3 分钟";
    setVideoImportValidation(errorText(error));
    showToast(errorText(error));
  }
}

async function submitVideoImport(event) {
  event?.preventDefault?.();
  if (videoImportPending) return;
  let file = videoImportFile || $("#video-file-input").files?.[0] || null;
  const shareText = $("#video-share-text").value.trim();
  try {
    if (file) file = await validateVideoFile(file);
    else if (!extractShareUrl(shareText)) throw new Error("请粘贴包含安全 http(s) 链接的分享文案，或上传 MP4 / MOV 视频。" );
  } catch (error) {
    setVideoImportValidation(errorText(error));
    showToast(errorText(error));
    return;
  }
  videoImportFile = file;
  videoImportRequest = file ? { kind: "file", file } : { kind: "share_text", shareText };
  videoImportId = "";
  videoImportStage = "queued";
  videoImportDraft = null;
  videoImportDraftDirty = false;
  stopVideoImportPolling();
  $("#video-import-draft").classList.add("hidden");
  setVideoImportValidation("");
  videoImportPending = true;
  renderVideoImportStatus({ stage: "queued", message: file ? "已收到视频，准备分析画面与语音。" : "已收到分享文案，准备读取视频。" });
  try {
    const options = file
      ? (() => { const form = new FormData(); form.append("file", file, file.name || "recipe-video.mp4"); return { method: "POST", body: form }; })()
      : jsonPost({ share_text: shareText });
    const result = await api("/api/video-imports", options);
    if (!result.id) throw new Error("导入任务没有返回 ID，请稍后重试。" );
    videoImportId = String(result.id);
    renderVideoImportStatus(result);
    if (isVideoImportTerminal(videoImportStage)) {
      videoImportPending = false;
      updateVideoImportButtons();
    } else startVideoImportPolling(videoImportId);
  } catch (error) {
    videoImportPending = false;
    stopVideoImportPolling();
    renderVideoImportStatus({ stage: "failed", error: { message: errorText(error, "视频导入未完成，请检查链接或改为上传视频。") } });
    setVideoImportValidation(errorText(error));
    updateVideoImportButtons();
  }
}

async function cancelVideoImport() {
  if (!videoImportId || isVideoImportTerminal(videoImportStage)) return;
  const id = videoImportId;
  stopVideoImportPolling();
  videoImportPending = true;
  updateVideoImportButtons();
  try {
    const result = await api(`/api/video-imports/${encodeURIComponent(id)}`, { method: "DELETE" });
    renderVideoImportStatus({ ...result, id, stage: "cancelled", message: result.message || "已取消视频处理，输入内容仍保留。" });
    videoImportPending = false;
    updateVideoImportButtons();
  } catch (error) {
    videoImportPending = false;
    renderVideoImportStatus({ id, stage: "failed", error: { message: `取消失败：${errorText(error)}` } });
    updateVideoImportButtons();
  }
}

function videoDraftRows() {
  return Array.from(document.querySelectorAll("#video-draft-ingredients [data-draft-ingredient]"));
}

function videoDraftStepRows() {
  return Array.from(document.querySelectorAll("#video-draft-steps [data-draft-step]"));
}

function valueIn(row, selector) {
  return row.querySelector(selector)?.value ?? "";
}

function collectVideoDraft() {
  const ingredients = videoDraftRows().map((row) => ({
    name: valueIn(row, '[data-draft-field="ingredient-name"]').trim(),
    amount: valueIn(row, '[data-draft-field="ingredient-amount"]').trim(),
    unit: valueIn(row, '[data-draft-field="ingredient-unit"]').trim(),
    optional: Boolean(row.querySelector('[data-draft-field="ingredient-optional"]')?.checked)
  }));
  const steps = videoDraftStepRows().map((row) => {
    const rawDuration = valueIn(row, '[data-draft-field="step-duration"]').trim();
    const duration = rawDuration === "" ? null : Number(rawDuration);
    return {
      instruction: valueIn(row, '[data-draft-field="step-instruction"]').trim(),
      duration_seconds: Number.isFinite(duration) && duration >= 0 ? Math.round(duration) : null,
      heat_level: valueIn(row, '[data-draft-field="step-heat"]').trim(),
      safety_note: valueIn(row, '[data-draft-field="step-safety"]').trim()
    };
  });
  return {
    name: $("#video-draft-name").value.trim(),
    servings: Number($("#video-draft-servings").value),
    ingredients,
    equipment: $("#video-draft-equipment").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
    steps
  };
}

function validateVideoDraft(draft) {
  if (!draft.name) return "请填写菜名。";
  if (!Number.isInteger(draft.servings) || draft.servings < 1 || draft.servings > 6) return "份量必须是 1 到 6 人。";
  if (!draft.ingredients.length) return "请至少保留一种食材。";
  if (draft.ingredients.some((item) => !item.name)) return "每项食材都需要填写名称。";
  if (!draft.steps.length) return "请至少保留一个步骤。";
  if (draft.steps.some((item) => !item.instruction)) return "每个步骤都需要填写操作说明。";
  return "";
}

function setDraftValidation(message = "") {
  const target = $("#video-draft-validation");
  if (target) target.textContent = message;
}

async function persistVideoDraft(draft, { silent = false, allowIncomplete = false } = {}) {
  if (!videoImportId) {
    setDraftValidation("导入任务已结束，请重新解析视频。");
    return false;
  }
  const validation = validateVideoDraft(draft);
  if (validation) {
    setDraftValidation(validation);
    if (!silent) showToast(validation);
    return false;
  }
  const saveButton = $("#video-draft-save");
  if (saveButton) { saveButton.disabled = true; saveButton.textContent = "保存中…"; }
  try {
    const result = await api(`/api/video-imports/${encodeURIComponent(videoImportId)}/draft`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft)
    });
    if (result?.error) {
      const message = errorText(result.error);
      if (result.draft) renderVideoDraft(result.draft);
      setDraftValidation(message);
      if (!silent) showToast(message);
      if (allowIncomplete && result.draft) {
        videoImportDraftDirty = false;
        return true;
      }
      return false;
    }
    const savedDraft = result.draft || draft;
    videoImportDraftDirty = false;
    renderVideoDraft(savedDraft);
    setDraftValidation("");
    if (!silent) showToast("菜谱修改已保存");
    return true;
  } catch (error) {
    setDraftValidation(errorText(error));
    if (!silent) showToast(errorText(error));
    return false;
  } finally {
    if (saveButton) { saveButton.disabled = false; saveButton.textContent = "保存修改"; }
  }
}

async function saveVideoDraft() {
  return persistVideoDraft(collectVideoDraft());
}

async function completeVideoDraft() {
  if (!videoImportId) return;
  if (videoImportDraftDirty) {
    if (!(await persistVideoDraft(collectVideoDraft(), { silent: true, allowIncomplete: true }))) return;
  }
  const id = videoImportId;
  const draftBeforeCompletion = JSON.stringify(collectVideoDraft());
  const buttons = ["#video-draft-complete", "#video-draft-save", "#video-draft-confirm"].map($).filter(Boolean);
  buttons.forEach(button => { button.disabled = true; });
  const complete = $("#video-draft-complete");
  if (complete) complete.textContent = "AI 正在补全…";
  setDraftValidation("正在补全用量和做法，完成后请核对 AI 建议。");
  try {
    const result = await api(`/api/video-imports/${encodeURIComponent(id)}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (id !== videoImportId) return;
    if (result.error) throw new Error(errorText(result.error));
    if (draftBeforeCompletion !== JSON.stringify(collectVideoDraft())) {
      setDraftValidation("补全期间你修改了草稿，已保留当前修改；请再次补全。");
      return;
    }
    videoImportDraftDirty = false;
    renderVideoImportStatus(result);
    const incomplete = result.draft?.import_metadata?.completion_needed;
    setDraftValidation(incomplete ? "AI 已补充建议，仍有缺失信息，可再次补全或修改草稿。" : "AI 已补全，请核对建议用量和做法后再确认保存。");
    showToast(incomplete ? "仍有缺失信息，请核对草稿" : "AI 建议已补全，请核对后保存");
  } catch (error) {
    setDraftValidation(errorText(error));
    showToast(errorText(error));
  } finally {
    buttons.forEach(button => { button.disabled = false; });
    if (complete) complete.textContent = "AI 补全缺失信息";
  }
}

async function confirmVideoDraft() {
  if (!videoImportId) return showToast("请先解析一段视频。");
  const draft = collectVideoDraft();
  const validation = validateVideoDraft(draft);
  if (validation) { setDraftValidation(validation); return showToast(validation); }
  if (videoImportDraftDirty) {
    if (!(await persistVideoDraft(draft, { silent: true }))) return;
    const message = "修改已整理，请核对后再次确认";
    setDraftValidation(message);
    showToast(message);
    return;
  }
  const button = $("#video-draft-confirm");
  if (button) { button.disabled = true; button.textContent = "保存中…"; }
  try {
    const result = await api(`/api/video-imports/${encodeURIComponent(videoImportId)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    videoImportDraftDirty = false;
    const recipe = result.recipe || {};
    const recipeId = result.recipe_id || recipe.recipe_id;
    renderVideoImportStatus({ ...result, id: videoImportId, stage: "confirmed", draft: recipe });
    $("#video-import-draft").classList.add("hidden");
    await loadImportedRecipes();
    showToast("菜谱已保存到已导入列表");
    if (recipeId) await openRecipeSheet(recipeId, recipe.servings || draft.servings);
  } catch (error) {
    setDraftValidation(errorText(error));
    showToast(errorText(error));
  } finally {
    if (button) { button.disabled = false; button.textContent = "确认并保存菜谱"; }
  }
}

function addVideoDraftIngredient() {
  if (!videoImportDraft) return;
  const draft = collectVideoDraft();
  draft.ingredients.push({ name: "", amount: "", unit: "", optional: false });
  videoImportDraftDirty = true;
  renderVideoDraft(draft);
  const names = videoDraftRows();
  names.at(-1)?.querySelector('[data-draft-field="ingredient-name"]')?.focus();
}

function addVideoDraftStep() {
  if (!videoImportDraft) return;
  const draft = collectVideoDraft();
  draft.steps.push({ instruction: "", duration_seconds: null, heat_level: "", safety_note: "" });
  videoImportDraftDirty = true;
  renderVideoDraft(draft);
  const rows = videoDraftStepRows();
  rows.at(-1)?.querySelector('[data-draft-field="step-instruction"]')?.focus();
}

function removeVideoDraftIngredient(index) {
  const draft = collectVideoDraft();
  draft.ingredients.splice(Number(index), 1);
  videoImportDraftDirty = true;
  renderVideoDraft(draft);
}

function removeVideoDraftStep(index) {
  const draft = collectVideoDraft();
  draft.steps.splice(Number(index), 1);
  videoImportDraftDirty = true;
  renderVideoDraft(draft);
}

async function loadImportedRecipes() {
  try {
    const result = await api("/api/imported-recipes");
    importedRecipes = Array.isArray(result.recipes) ? result.recipes : [];
    renderImportedRecipes();
    return importedRecipes;
  } catch (error) {
    importedRecipes = [];
    renderImportedRecipes();
    return [];
  }
}

function syncImportedRecipesLayout() {
  const panel = $("#imported-recipes-panel");
  const toggle = $("#toggle-imported-recipes");
  panel.classList.toggle("is-expanded", importedRecipesExpanded);
  toggle.textContent = importedRecipesExpanded ? "收起" : "展开";
  toggle.setAttribute("aria-expanded", String(importedRecipesExpanded));
  $("#imported-recipes-grid").setAttribute("aria-label", importedRecipesExpanded ? "全部已导入菜谱" : "已导入菜谱，可左右滑动浏览");
  $("#imported-recipes-count").textContent = importedRecipes.length
    ? `共 ${importedRecipes.length} 道${importedRecipesExpanded ? "" : " · 左右滑动"}` : "";
}

function toggleImportedRecipes() {
  importedRecipesExpanded = !importedRecipesExpanded;
  syncImportedRecipesLayout();
}

function renderImportedRecipes() {
  const panel = $("#imported-recipes-panel");
  const grid = $("#imported-recipes-grid");
  if (!panel || !grid) return;
  panel.classList.toggle("hidden", !importedRecipes.length);
  syncImportedRecipesLayout();
  grid.innerHTML = importedRecipes.map((item, index) => {
    const recipeId = item.recipe_id || item.id || "";
    const metadata = item.import_metadata || {};
    const sourceMetadata = metadata.source && typeof metadata.source === "object" ? metadata.source : {};
    const source = metadata.platform || metadata.source_platform || item.source_name || sourceMetadata.platform || "视频菜谱";
    const servings = Number($("#servings")?.value);
    const requestedServings = Number.isInteger(servings) && servings >= 1 && servings <= 6 ? servings : 1;
    return `<button class="library-card imported-recipe-card" type="button" aria-label="查看菜谱：${escapeHtml(item.name || "未命名菜谱")}" title="${escapeHtml(item.name || "未命名菜谱")}" data-imported-recipe-id="${escapeHtml(recipeId)}" data-servings="${requestedServings}">
      <span>${escapeHtml(source)} · ${escapeHtml(item.estimated_time_minutes || item.estimated_minutes || "?")} 分钟</span>
      <strong>${escapeHtml(item.name || "未命名菜谱")}</strong>
      <small>${escapeHtml((item.ingredients || []).slice(0, 3).map((ingredient) => ingredient.name).join("、") || "点击查看完整步骤")}</small>
      ${index === 0 ? "<em class=\"imported-recipe-badge\">最新</em>" : ""}
    </button>`;
  }).join("");
}

async function sendCookingAction(action, button) {
  if (!action || cookingMutationPending) return;
  cookingMutationPending = true;
  syncCookingNavigation();
  const original = button?.textContent || "处理中…";
  if (button) { button.disabled = true; button.textContent = "处理中…"; }
  try {
    const result = await api("/api/cooking/action", jsonPost({ action }));
    await refreshStatus();
    if (!robotFeedbackSupported) showToast(result.message || ({ start_timer: "计时已开始", confirm_done: "已确认完成", pause: "指导已暂停", resume: "指导已恢复", cancel_timer: "已取消当前计时", fresh_ingredients: "已确认食材状态" }[action] || "操作已完成"));
  } catch (error) { showToast(errorText(error)); }
  finally {
    cookingMutationPending = false;
    if (button) { button.disabled = false; button.textContent = original; }
    if (button === $("#cooking-pause-resume")) button.textContent = currentKitchen?.state === "PAUSED" ? "恢复指导" : "暂停指导";
    syncCookingNavigation();
  }
}

async function endCurrentCookingTask(button) {
  if (typeof window !== "undefined" && typeof window.confirm === "function" && !window.confirm("确定结束当前厨房任务吗？")) return;
  return sendCookingAction("cancel_task", button);
}

async function searchLocalRecipes() {
  const query = $("#recipe-search").value.trim();
  const button = $("#recipe-search-button");
  button.disabled = true;
  try {
    const result = await api(`/api/recipes?q=${encodeURIComponent(query)}&category=${encodeURIComponent(selectedRecipeCategory)}`);
    const categoryLabel = selectedRecipeCategory ? `· ${selectedRecipeCategory}` : "";
    $("#library-count").textContent = query ? `找到 ${result.count} 道与“${query}”相关的菜 ${categoryLabel}` : `共 ${result.count} 道本地菜谱 ${categoryLabel}`;
    $("#library-grid").innerHTML = result.recipes.length ? result.recipes.map((item) => `
      <button class="library-card" type="button" data-recipe-id="${escapeHtml(item.recipe_id)}">
        <span>${escapeHtml(item.estimated_minutes || "?")} 分钟 · ${escapeHtml(item.difficulty)}</span>
        <strong>${escapeHtml(item.name)}</strong>
        <small>${escapeHtml((item.main_ingredients || []).join("、") || item.summary)}</small>
      </button>
    `).join("") : `<div class="empty-state"><div><h3>没有找到本地菜谱</h3><p>可以确认后让 AI 生成一份详细新手菜谱。</p>${query ? `<button class="primary-button" type="button" data-generate-dish="${escapeHtml(query)}">AI 生成“${escapeHtml(query)}”</button>` : ""}</div></div>`;
    $("#recipe-library").classList.remove("hidden");
    $("#recipe-library").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
}

async function generateMissingRecipe(dishName, button) {
  const dish = String(dishName || "").trim();
  if (!dish || !window.confirm(`本地没有找到“${dish}”。是否确认调用 AI 生成一个新菜谱？`)) return;
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = "AI 生成中…";
  try {
    await submitRecommendations({ path: "/api/recipes/generate", body: {
      dish_name: dish, servings: Number($("#servings")?.value || 1)
    } });
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function loadRecipeCategories() {
  try {
    const result = await api("/api/recipe-categories");
    const categories = [{ id: "", name: "全部", count: 0 }, ...(result.categories || [])];
    $("#recipe-categories").innerHTML = categories.map((item, index) => `
      <button class="category-pill ${item.id === selectedRecipeCategory ? "active" : ""}" type="button" data-category="${escapeHtml(item.id)}">${escapeHtml(item.name)}${index ? ` ${item.count}` : ""}</button>
    `).join("");
  } catch (error) { showToast(error.message); }
}

async function openRecipeSheet(recipeId, initialServings) {
  if (!recipeId) return showToast("当前还没有可查看的完整菜谱");
  const reopening = activeRecipeId === recipeId && !$("#recipe-sheet").classList.contains("hidden");
  activeRecipeId = recipeId;
  const requestedServings = Number(initialServings);
  if (Number.isInteger(requestedServings) && requestedServings >= 1 && requestedServings <= 6) {
    $("#sheet-servings").value = String(requestedServings);
  } else if (!reopening) {
    const mealServings = Number($("#servings")?.value || 1);
    $("#sheet-servings").value = String(Number.isInteger(mealServings) && mealServings >= 1 && mealServings <= 6 ? mealServings : 1);
  }
  const servings = Number($("#sheet-servings").value || 1);
  $("#sheet-title").textContent = "正在读取…";
  $("#recipe-sheet").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  try {
    const recipe = await api(`/api/recipes/${encodeURIComponent(recipeId)}?servings=${servings}`);
    renderRecipeSheet(recipe);
  } catch (error) {
    closeRecipeSheet();
    showToast(error.message);
  }
}

function renderRecipeSheet(recipe) {
  $("#sheet-title").textContent = recipe.name || "完整菜谱";
  $("#sheet-kicker").textContent = recipe.source_name || "本地完整菜谱";
  $("#sheet-meta").innerHTML = [
    `${recipe.estimated_time_minutes || "?"} 分钟`, recipe.difficulty || "家常",
    `${recipe.steps?.length || 0} 个步骤`
  ].map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  $("#sheet-ingredients").innerHTML = (recipe.ingredients || []).map((item) => `
    <div class="ingredient-row"><span>${escapeHtml(item.name)}</span><span>${escapeHtml(ingredientAmountDisplay(item))}</span></div>
  `).join("");
  $("#sheet-steps").innerHTML = (recipe.steps || []).map((step) => `
    <li>${escapeHtml(step.instruction || "")}${step.heat_level ? `<small> · ${escapeHtml(step.heat_level)}</small>` : ""}${step.duration_seconds ? `<small> · ${Math.round(step.duration_seconds / 60)} 分钟</small>` : ""}${step.safety_note ? `<small class="step-safety">注意：${escapeHtml(step.safety_note)}</small>` : ""}</li>
  `).join("");
}

function closeRecipeSheet() {
  $("#recipe-sheet").classList.add("hidden");
  document.body.style.overflow = "";
}

async function selectActiveRecipe() {
  if (!activeRecipeId) return;
  const button = $("#select-recipe");
  button.disabled = true;
  button.textContent = "正在选择…";
  try {
    const result = await api("/api/recipes/select", jsonPost({
      recipe_id: activeRecipeId,
      servings: Number($("#sheet-servings").value)
    }));
    closeRecipeSheet();
    updateStatus({ ...(await api("/api/status")) });
    if (!robotFeedbackSupported) showToast("已选择这道菜，做菜流程已准备好");
    $("#cooking-panel").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    showToast(error.status === 409 ? (error.message || "请先结束当前厨房任务，再开始这道菜。") : error.message);
  }
  finally { button.disabled = false; button.textContent = "选择这道菜"; }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function tickClock() {
  const now = new Date();
  $("#clock-time").textContent = now.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  $("#clock-date").textContent = now.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" });
}

$("#capture-server").addEventListener("click", captureServerCamera);
$("#use-camera-frame").addEventListener("click", async () => {
  if (!selectedImageDataUrl) await captureServerCamera();
  if (selectedImageDataUrl) {
    $("#selected-photo").src = selectedImageDataUrl;
    $("#upload-preview").classList.add("has-image");
    analyzeSelectedImage();
  }
});
$("#photo-input").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    selectedImageDataUrl = await fileToDataUrl(file);
    $("#selected-photo").src = selectedImageDataUrl;
    $("#upload-preview").classList.add("has-image");
    await analyzeSelectedImage();
  } catch (error) { showToast(error.message); }
});
$("#ingredient-chips").addEventListener("click", (event) => {
  const index = event.target.dataset.remove;
  if (index === undefined) return;
  ingredients.splice(Number(index), 1);
  renderIngredients();
});
$("#add-ingredient").addEventListener("click", () => {
  const name = window.prompt("补充一种食材名称");
  const cleaned = name?.trim().slice(0, 30);
  if (cleaned && !ingredients.some((item) => item.name === cleaned)) {
    ingredients.push({ name: cleaned, confidence: "手动" });
    renderIngredients();
  }
});
$("#recommend").addEventListener("click", requestRecommendations);
$("#recipe-search-button").addEventListener("click", searchLocalRecipes);
$("#recipe-categories").addEventListener("click", (event) => {
  const button = event.target.closest("[data-category]");
  if (!button) return;
  selectedRecipeCategory = button.dataset.category || "";
  loadRecipeCategories().then(searchLocalRecipes);
});
$("#recipe-search").addEventListener("keydown", (event) => { if (event.key === "Enter") searchLocalRecipes(); });
$("#close-library").addEventListener("click", () => $("#recipe-library").classList.add("hidden"));
$("#library-grid").addEventListener("click", (event) => {
  const generateButton = event.target.closest("[data-generate-dish]");
  if (generateButton) return generateMissingRecipe(generateButton.dataset.generateDish, generateButton);
  const card = event.target.closest("[data-recipe-id]");
  if (card) openRecipeSheet(card.dataset.recipeId);
});
loadRecipeCategories();
$("#recipe-cards").addEventListener("click", (event) => {
  if (event.target.closest("[data-retry-recommendations]") && lastRecommendationRequest) {
    return lastRecommendationRequest.path === "/api/recommendations"
      ? requestRecommendations()
      : submitRecommendations(lastRecommendationRequest);
  }
  const card = event.target.closest("[data-recipe-id]");
  if (card) return openRecipeSheet(card.dataset.recipeId, card.dataset.servings);
});
$("#cooking-panel").addEventListener("click", (event) => {
  if (!event.target.closest("button")) openRecipeSheet(currentKitchen?.recipe?.recipe_id, currentKitchen?.recipe?.servings);
});
$("#cooking-panel").addEventListener("keydown", (event) => {
  if (!event.target.closest("button") && (event.key === "Enter" || event.key === " ")) openRecipeSheet(currentKitchen?.recipe?.recipe_id, currentKitchen?.recipe?.servings);
});
$("#previous-step").addEventListener("click", (event) => { event.stopPropagation(); navigateCookingStep("previous", event.currentTarget); });
$("#next-step").addEventListener("click", (event) => { event.stopPropagation(); navigateCookingStep("next", event.currentTarget); });
$("#choose-another").addEventListener("click", (event) => { event.stopPropagation(); $("#pantry").scrollIntoView({ behavior: "smooth", block: "start" }); });
$("#browse-after-complete").addEventListener("click", async (event) => { event.stopPropagation(); await searchLocalRecipes(); });
$("#sheet-close").addEventListener("click", closeRecipeSheet);
$("#recipe-sheet").addEventListener("click", (event) => { if (event.target.id === "recipe-sheet") closeRecipeSheet(); });
$("#sheet-servings").addEventListener("change", () => openRecipeSheet(activeRecipeId, Number($("#sheet-servings").value)));
$("#select-recipe").addEventListener("click", selectActiveRecipe);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeRecipeSheet(); });

$("#video-import-submit").addEventListener("click", submitVideoImport);
$("#video-file-input").addEventListener("change", handleVideoFileChange);
$("label[for='video-file-input']").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("#video-file-input").click(); }
});
$("#video-import-cancel").addEventListener("click", cancelVideoImport);
$("#video-draft-save").addEventListener("click", saveVideoDraft);
$("#video-draft-complete").addEventListener("click", completeVideoDraft);
$("#video-draft-confirm").addEventListener("click", confirmVideoDraft);
$("#video-draft-add-ingredient").addEventListener("click", addVideoDraftIngredient);
$("#video-draft-add-step").addEventListener("click", addVideoDraftStep);
$("#video-import-draft").addEventListener("input", (event) => {
  if (event.target.matches("input, textarea, select")) videoImportDraftDirty = true;
});
$("#video-import-draft").addEventListener("click", (event) => {
  const removeIngredient = event.target.closest("[data-remove-draft-ingredient]");
  if (removeIngredient) return removeVideoDraftIngredient(removeIngredient.dataset.removeDraftIngredient);
  const removeStep = event.target.closest("[data-remove-draft-step]");
  if (removeStep) return removeVideoDraftStep(removeStep.dataset.removeDraftStep);
});
$("#imported-recipes-grid").addEventListener("click", (event) => {
  const card = event.target.closest("[data-imported-recipe-id]");
  if (card) openRecipeSheet(card.dataset.importedRecipeId, card.dataset.servings);
});
$("#refresh-imported-recipes").addEventListener("click", loadImportedRecipes);
$("#toggle-imported-recipes").addEventListener("click", toggleImportedRecipes);
$("#cooking-start-timer").addEventListener("click", (event) => sendCookingAction("start_timer", event.currentTarget));
$("#cooking-confirm-done").addEventListener("click", (event) => sendCookingAction("confirm_done", event.currentTarget));
$("#cooking-pause-resume").addEventListener("click", (event) => sendCookingAction(currentKitchen?.state === "PAUSED" ? "resume" : "pause", event.currentTarget));
$("#cooking-cancel-timer").addEventListener("click", (event) => sendCookingAction("cancel_timer", event.currentTarget));
$("#cooking-fresh-ingredients").addEventListener("click", (event) => sendCookingAction("fresh_ingredients", event.currentTarget));
$("#cooking-end-task").addEventListener("click", (event) => endCurrentCookingTask(event.currentTarget));

tickClock();
setInterval(tickClock, 30000);
refreshStatus();
statusTimer = setInterval(refreshStatus, 500);
loadImportedRecipes();
