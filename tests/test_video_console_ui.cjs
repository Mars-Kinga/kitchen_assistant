const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : Boolean(force);
    if (next) this.values.add(name); else this.values.delete(name);
    return next;
  }
  contains(name) { return this.values.has(name); }
}

class FakeNode {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.files = [];
    this.disabled = false;
    this.checked = false;
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.fields = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) { return this.fields[selector] || null; }
  click() { this.listeners.click?.({ currentTarget: this, target: this }); }
  focus() { this.focused = true; }
  scrollIntoView() {}
}

const ids = [
  "video-share-text", "video-file-input", "video-file-name", "video-file-meta", "video-import-validation",
  "video-import-submit", "video-import-cancel", "video-import-state", "video-import-idle", "video-import-status",
  "video-import-dot", "video-import-status-title", "video-import-status-message", "video-import-stages",
  "video-import-elapsed", "video-import-preview", "video-preview-title", "video-preview-count", "video-preview-ingredients", "video-preview-steps",
  "video-import-draft", "video-draft-name",
  "video-draft-servings", "video-draft-equipment", "video-draft-source-note", "video-draft-notice",
  "video-draft-ingredients", "video-draft-steps", "video-draft-validation", "video-draft-save",
  "video-draft-confirm", "video-draft-complete", "imported-recipes-panel", "imported-recipes-count", "imported-recipes-grid", "toggle-imported-recipes",
  "cooking-start-timer", "cooking-confirm-done", "cooking-pause-resume", "cooking-cancel-timer",
  "cooking-fresh-ingredients", "cooking-end-task", "scope-notice", "robot-dot", "camera-dot", "timer-dot",
  "robot-state", "robot-detail", "camera-state", "timer-state", "timer-detail",
  "feedback-action", "feedback-light", "feedback-expression", "feedback-display", "feedback-speech",
  "kitchen-state", "hero-summary", "empty-cooking", "active-cooking", "completed-cooking", "completed-recipe-name",
  "recipe-name", "recipe-meta", "step-number", "step-instruction", "step-details", "parallel-step",
  "parallel-step-title", "parallel-step-instruction", "step-progress", "previous-step", "next-step",
  "cooking-panel", "recipe-sheet", "sheet-title", "sheet-kicker", "sheet-meta", "sheet-ingredients", "sheet-steps",
  "sheet-servings", "select-recipe", "recommend", "recipe-cards", "recommendations", "provider-label", "recommendation-message", "toast"
];
const nodes = Object.fromEntries(ids.map((id) => [id, new FakeNode(id)]));
const stageNodes = ["fetching", "analyzing", "structuring", "ready"].map((stage) => {
  const node = new FakeNode(stage);
  node.dataset.videoStage = stage;
  return node;
});
let ingredientRows = [];
let stepRows = [];
const fileLabel = new FakeNode("video-file-label");

const document = {
  body: { style: {} },
  querySelector(selector) {
    if (selector === "label[for='video-file-input']") return fileLabel;
    if (selector.startsWith("#")) return nodes[selector.slice(1)] || null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "#video-import-stages [data-video-stage]") return stageNodes;
    if (selector === "#video-draft-ingredients [data-draft-ingredient]") return ingredientRows;
    if (selector === "#video-draft-steps [data-draft-step]") return stepRows;
    return [];
  },
  addEventListener() {}
};

const timers = [];
const context = vm.createContext({
  document,
  window: { confirm: () => true, URL },
  URL,
  FormData,
  console,
  setTimeout(callback, delay) { const item = { callback, delay, cancelled: false }; timers.push(item); return item; },
  clearTimeout(item) { if (item) item.cancelled = true; },
  setInterval() { return null; },
  clearInterval() {}
});
const source = fs.readFileSync(path.join(__dirname, "../runtime_core/kitchen_console_static/app.js"), "utf8");
vm.runInContext(source.slice(0, source.indexOf('$("#capture-server").addEventListener')), context);

function response(body, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

function inputField(value = "", checked = false) {
  const field = new FakeNode();
  field.value = value;
  field.checked = checked;
  return field;
}

function makeIngredientRow(name, amount, unit, optional = false) {
  const row = new FakeNode();
  row.fields['[data-draft-field="ingredient-name"]'] = inputField(name);
  row.fields['[data-draft-field="ingredient-amount"]'] = inputField(amount);
  row.fields['[data-draft-field="ingredient-unit"]'] = inputField(unit);
  row.fields['[data-draft-field="ingredient-optional"]'] = inputField("", optional);
  return row;
}

function makeStepRow(instruction, duration, heat, safety) {
  const row = new FakeNode();
  row.fields['[data-draft-field="step-instruction"]'] = inputField(instruction);
  row.fields['[data-draft-field="step-duration"]'] = inputField(duration);
  row.fields['[data-draft-field="step-heat"]'] = inputField(heat);
  row.fields['[data-draft-field="step-safety"]'] = inputField(safety);
  return row;
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
  vm.runInContext('renderRecipeSheet({name:"蜜汁土豆泥鸡腿饭",ingredients:[{name:"鸡腿肉",amount:"适量",unit:"块"},{name:"生抽",amount:"两勺",unit:"勺"},{name:"清水",amount:"半碗",unit:"碗"},{name:"十三香",amount:"少许",unit:"份"},{name:"鸡蛋",amount:2,unit:"个"}]})', context);
  const ingredientTable = nodes["sheet-ingredients"].innerHTML;
  assert.doesNotMatch(ingredientTable, /适量块|勺勺|碗碗|少许份/);
  assert.match(ingredientTable, /两勺/);
  assert.match(ingredientTable, /半碗/);
  assert.match(ingredientTable, /2个/);
  const requests = [];
  const shareText = "🔥能让我一次扒3碗米饭的下饭届天菜❗️ https://xhslink.cn/o/7Al9WMJ4hWr";
  nodes["video-share-text"].value = shareText;
  const draft = {
    name: "蒜香鸡翅",
    servings: 2,
    ingredients: [{ name: "鸡翅", amount: 500, unit: "克", origin: "video" }, { name: "生抽", amount: "2", unit: "勺", source: "ai" }],
    equipment: ["炒锅"],
    steps: [{ instruction: "鸡翅洗净擦干。", duration_seconds: 0, heat_level: "中火", safety_note: "小心油溅。" }],
    import_metadata: { platform: "小红书", source_url: "https://xhslink.cn/o/7Al9WMJ4hWr", field_annotations: [{ path: "ingredients.1", origin: "ai" }] }
  };
  const queued = { id: "video-1", stage: "analyzing", message: "正在识别画面与语音" };
  const ready = { id: "video-1", stage: "review", message: "请核对菜谱", draft };
  const sequence = [queued, { id: "video-1", stage: "analyzing", message: "正在识别画面与语音" }, ready];
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/video-imports" && options.method === "POST") return response(sequence.shift());
    if (url === "/api/video-imports/video-1") return response(sequence.shift() || ready);
    throw new Error(`unexpected request ${url}`);
  };
  await vm.runInContext("submitVideoImport()", context);
  await flush();
  const pollTimer = timers.find((item) => item.delay === 1000 && !item.cancelled);
  assert.ok(pollTimer, "processing imports should reveal new content within one second");
  await pollTimer.callback();
  await flush();
  assert.equal(requests[0].url, "/api/video-imports");
  assert.deepEqual(JSON.parse(requests[0].options.body), { share_text: shareText });
  assert.equal(nodes["video-import-state"].textContent, "等待确认");
  assert.match(nodes["video-draft-ingredients"].innerHTML, /鸡翅/);
  assert.match(nodes["video-draft-ingredients"].innerHTML, /AI 补全/);
  assert.equal(nodes["video-share-text"].value, shareText, "source input remains available for retry");

  nodes["video-share-text"].value = "javascript:alert(1)";
  const beforeInvalid = requests.length;
  await vm.runInContext("submitVideoImport()", context);
  assert.equal(requests.length, beforeInvalid, "unsafe links must be rejected before upload");
  assert.match(nodes["video-import-validation"].textContent, /http/);
  nodes["video-share-text"].value = shareText;

  context.openRecipeSheet = async (recipeId, servings) => { context.openedRecipe = { recipeId, servings }; };
  ingredientRows = [makeIngredientRow("鸡翅", "500", "克")];
  stepRows = [makeStepRow("鸡翅煎至金黄。", "120", "中火", "注意油溅")];
  vm.runInContext("videoImportId = 'video-1'; videoImportStage = 'ready'; videoImportDraftDirty = true; renderVideoDraft({name:'蒜香鸡翅', servings:2, ingredients:[{name:'鸡翅', amount:500, unit:'克'}], steps:[{instruction:'鸡翅洗净。', duration_seconds:60, heat_level:'中火', safety_note:''}]})", context);
  nodes["video-draft-name"].value = "蒜香鸡翅（修改版）";
  requests.length = 0;
  const normalizedDraft = {
    ...draft,
    name: "蒜香鸡翅（修改版）",
    steps: [{ instruction: "鸡翅焖煮并收汁。", duration_seconds: 240, heat_level: "中火", safety_note: "小心油溅。" }]
  };
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (options.method === "PATCH") return response({ id: "video-1", stage: "ready", draft: normalizedDraft });
    if (options.method === "POST" && url.endsWith("/confirm")) return response({ id: "video-1", recipe_id: "imported-1", recipe: { ...normalizedDraft, recipe_id: "imported-1" } });
    if (url === "/api/imported-recipes") return response({ recipes: [{ recipe_id: "imported-1", name: "蒜香鸡翅（修改版）", import_metadata: { platform: "小红书" }, ingredients: normalizedDraft.ingredients }] });
    throw new Error(`unexpected request ${url}`);
  };
  await vm.runInContext("confirmVideoDraft()", context);
  assert.equal(requests[0].url, "/api/video-imports/video-1/draft");
  assert.equal(requests[0].options.method, "PATCH");

  assert.equal(JSON.parse(requests[0].options.body).name, "蒜香鸡翅（修改版）");
  assert.equal(requests.some((item) => item.url.endsWith("/confirm")), false, "a dirty draft must not confirm immediately after PATCH");
  assert.equal(nodes["video-draft-validation"].textContent, "修改已整理，请核对后再次确认");
  assert.match(nodes["video-draft-steps"].innerHTML, /焖煮并收汁/);

  // The real DOM re-creates these rows when PATCH returns a normalized draft.
  // Mirror that small part of the browser behavior for the second click.
  stepRows = [makeStepRow("鸡翅焖煮并收汁。", "240", "中火", "小心油溅。")];
  requests.length = 0;
  await vm.runInContext("confirmVideoDraft()", context);
  assert.equal(requests[0].url, "/api/video-imports/video-1/confirm");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[1].url, "/api/imported-recipes");
  assert.deepEqual(context.openedRecipe, { recipeId: "imported-1", servings: 2 });
  assert.match(nodes["imported-recipes-grid"].innerHTML, /蒜香鸡翅/);
  assert.match(nodes["imported-recipes-grid"].innerHTML, /aria-label="查看菜谱：蒜香鸡翅/);
  assert.equal(nodes["toggle-imported-recipes"].attributes["aria-expanded"], "false");
  vm.runInContext("savedImportedRecipesForStripTest = importedRecipes; importedRecipes = Array.from({length: 12}, (_, index) => ({recipe_id: `strip-${index}`, name: `导入菜谱 ${index + 1}`})); renderImportedRecipes()", context);
  assert.equal((nodes["imported-recipes-grid"].innerHTML.match(/data-imported-recipe-id=/g) || []).length, 12, "all recipes remain available in the horizontal strip");
  assert.equal(nodes["imported-recipes-count"].textContent, "共 12 道 · 左右滑动");
  vm.runInContext("importedRecipes = []; renderImportedRecipes()", context);
  assert.equal(nodes["imported-recipes-panel"].classList.contains("hidden"), true);
  assert.equal(nodes["imported-recipes-grid"].innerHTML, "");
  vm.runInContext("importedRecipes = savedImportedRecipesForStripTest; renderImportedRecipes()", context);
  assert.equal(nodes["imported-recipes-panel"].classList.contains("hidden"), false);
  assert.equal(nodes["toggle-imported-recipes"].textContent, "展开");
  assert.equal(nodes["imported-recipes-count"].textContent, "共 1 道 · 左右滑动");
  vm.runInContext("toggleImportedRecipes()", context);
  assert.equal(nodes["imported-recipes-panel"].classList.contains("is-expanded"), true);
  assert.equal(nodes["toggle-imported-recipes"].attributes["aria-expanded"], "true");
  assert.equal(nodes["toggle-imported-recipes"].textContent, "收起");
  assert.equal(nodes["imported-recipes-count"].textContent, "共 1 道");
  vm.runInContext("renderImportedRecipes()", context);
  assert.equal(nodes["imported-recipes-panel"].classList.contains("is-expanded"), true, "refresh preserves the user's expanded layout");
  vm.runInContext("toggleImportedRecipes()", context);
  assert.equal(nodes["imported-recipes-panel"].classList.contains("is-expanded"), false);
  assert.equal(nodes["toggle-imported-recipes"].attributes["aria-expanded"], "false");
  assert.doesNotMatch(requests.map((item) => item.url).join(" "), /recipes\/select/);

  vm.runInContext("videoImportId = 'video-error'; videoImportStage = 'ready'; videoImportDraftDirty = true", context);
  requests.length = 0;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (options.method === "PATCH") return response({ id: "video-error", stage: "ready", error: "步骤还需要补充时间", draft: normalizedDraft });
    throw new Error(`unexpected request ${url}`);
  };
  const persisted = await vm.runInContext("persistVideoDraft(collectVideoDraft(), { silent: true })", context);
  assert.equal(persisted, false, "a draft error response must report an unsuccessful save");
  assert.equal(nodes["video-draft-validation"].textContent, "步骤还需要补充时间");
  assert.equal(vm.runInContext("videoImportDraftDirty", context), true, "a draft error keeps the editor dirty");

  const incompleteDraft = { ...draft, ingredients: [{ name: "鸡翅", amount: "", unit: "克" }] };
  const completedDraft = { ...draft, import_metadata: { ai_completed: true, completion_needed: false } };
  ingredientRows = [makeIngredientRow("鸡翅", "", "克")];
  stepRows = [makeStepRow("鸡翅煎至金黄。", "120", "中火", "注意油溅")];
  vm.runInContext("videoImportId = 'video-complete'; videoImportStage = 'review'; videoImportDraftDirty = true", context);
  requests.length = 0;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (options.method === "PATCH") return response({ id: "video-complete", stage: "review", error: "缺少用量", draft: incompleteDraft });
    if (url.endsWith("/complete")) return response({ id: "video-complete", stage: "review", draft: completedDraft });
    throw new Error(`unexpected request ${url}`);
  };
  await vm.runInContext("completeVideoDraft()", context);
  assert.deepEqual(requests.map(item => item.options.method), ["PATCH", "POST"]);
  assert.equal(JSON.parse(requests[0].options.body).ingredients[0].amount, "", "incomplete user edits must be saved before AI completion");
  assert.equal(requests[1].url, "/api/video-imports/video-complete/complete");
  assert.doesNotMatch(requests.map(item => item.url).join(" "), /confirm|recipes\/select/);
  assert.match(nodes["video-draft-validation"].textContent, /AI.*核对|核对.*AI/);
  assert.equal(nodes["video-draft-complete"].disabled, false);
  assert.equal(nodes["video-draft-complete"].classList.contains("hidden"), true);

  vm.runInContext("videoImportDraftDirty = false", context);
  requests.length = 0;
  const beforeCompletionError = nodes["video-draft-ingredients"].innerHTML;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    return response({ error: "AI 暂时无法连接" }, false, 502);
  };
  await vm.runInContext("completeVideoDraft()", context);
  assert.equal(requests.length, 1);
  assert.equal(nodes["video-draft-ingredients"].innerHTML, beforeCompletionError, "completion failure preserves the existing draft");
  assert.match(nodes["video-draft-validation"].textContent, /无法连接/);
  assert.equal(nodes["video-draft-confirm"].disabled, false);

  vm.runInContext("videoImportDraftDirty = true", context);
  requests.length = 0;
  await vm.runInContext("completeVideoDraft()", context);
  assert.equal(requests.length, 1, "a failed save must stop completion instead of using stale edits");
  assert.equal(requests[0].options.method, "PATCH");

  vm.runInContext("videoImportDraftDirty = false", context);
  context.fetch = async () => {
    nodes["video-draft-name"].value = "用户在补全期间修改菜名";
    return response({ id: "video-complete", stage: "review", draft: completedDraft });
  };
  await vm.runInContext("completeVideoDraft()", context);
  assert.equal(nodes["video-draft-name"].value, "用户在补全期间修改菜名");
  assert.match(nodes["video-draft-validation"].textContent, /保留当前修改/);
  assert.equal(vm.runInContext('videoImportFieldOrigin({origin:"video"}, {field_origins:{"ingredients[0].name":"video", "ingredients[0].amount":"ai"}}, "ingredients.0")', context), "AI 补全", "AI quantities must be visible even when the ingredient name came from video");

  vm.runInContext("videoImportId = 'video-2'; videoImportStage = 'analyzing'; videoImportPending = false", context);
  requests.length = 0;
  context.fetch = async (url, options = {}) => { requests.push({ url, options }); return response({ id: "video-2", stage: "cancelled", message: "已取消" }); };
  await vm.runInContext("cancelVideoImport()", context);
  assert.equal(requests[0].url, "/api/video-imports/video-2");
  assert.equal(requests[0].options.method, "DELETE");
  assert.equal(nodes["video-import-state"].textContent, "已取消");
  assert.equal(nodes["video-share-text"].value, shareText, "cancel keeps the source input");

  nodes["video-share-text"].value = shareText;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    return response({ error: "分享链接暂时无法读取" }, false, 502);
  };
  await vm.runInContext("videoImportId = ''; videoImportStage = ''; submitVideoImport()", context);
  await flush();
  assert.equal(nodes["video-import-state"].textContent, "处理失败");
  assert.match(nodes["video-import-status-message"].textContent, /无法读取/);
  assert.equal(nodes["video-share-text"].value, shareText, "failed imports keep the source input for retry");
  assert.equal(nodes["video-import-submit"].textContent, "重新解析");

  vm.runInContext('renderVideoImportStatus({stage:"failed",failed_stage:"analyzing",error:"鉴权失败"})', context);
  assert.equal(stageNodes[0].dataset.state, "done");
  assert.equal(stageNodes[1].dataset.state, "error");

  vm.runInContext('videoImportDraft = null; renderVideoImportStatus({id:"stream-1",stage:"analyzing",metrics:{elapsed_seconds:12.4},partial_preview:{name:"三杯鸡",ingredients:[{name:"鸡肉",amount:500,unit:"g"}],steps:[{instruction:"<script>切好鸡肉</script>"}],complete:false}})', context);
  assert.equal(nodes["video-import-preview"].classList.contains("hidden"), false);
  assert.equal(nodes["video-preview-count"].textContent, "1 项食材 · 1 个步骤");
  assert.match(nodes["video-preview-steps"].innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(nodes["video-preview-steps"].innerHTML, /<script>/);
  assert.equal(nodes["video-import-elapsed"].textContent, "已等待 12 秒");
  assert.equal(vm.runInContext("videoImportDraft", context), null, "partial content must never become an editable or executable draft");
  vm.runInContext('renderVideoImportStatus({id:"stream-1",stage:"failed",error:"结果不完整"})', context);
  assert.equal(nodes["video-import-preview"].classList.contains("hidden"), true, "failed partial content is discarded");
  vm.runInContext('renderVideoImportStatus({id:"stream-1",stage:"review",draft:{name:"三杯鸡",ingredients:[],steps:[]},metrics:{elapsed_seconds:25}})', context);
  assert.equal(nodes["video-import-preview"].classList.contains("hidden"), true);
  assert.equal(nodes["video-import-elapsed"].textContent, "处理用时 25 秒");

  // Ordinary local recipes need the same visible preparation confirmation
  // as imported recipes. It starts step 1, rather than skipping to step 2.
  let cookingStatus = {
    state: "WAITING_MEAT_THAW", session_active: true,
    recipe: { name: "咖喱肥牛", estimated_minutes: 28, difficulty: "中等" },
    current_step: { number: 1, total: 7, instruction: "按清单称量食材。" },
    timer: null, candidates: []
  };
  const renderCooking = () => {
    context.cookingStatusData = { robot: {}, camera: {}, kitchen: cookingStatus, timers: [], scope_notice: "" };
    vm.runInContext("updateStatus(cookingStatusData)", context);
  };
  renderCooking();
  assert.match(nodes["step-number"].textContent, /开始前/);
  assert.match(nodes["step-instruction"].textContent, /完全解冻/);
  assert.equal(nodes["step-progress"].style.width, "0%");
  assert.equal(nodes["next-step"].disabled, false);
  assert.match(nodes["next-step"].textContent, /新鲜.*已完全解冻.*开始/);
  requests.length = 0;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/status") return response({ robot: {}, camera: {}, kitchen: cookingStatus, timers: [], scope_notice: "" });
    if (url === "/api/cooking/action") {
      const action = JSON.parse(options.body).action;
      if (action === "cancel_task") cookingStatus = { state: "CANCELLED", session_active: false, recipe: null, current_step: null };
      else cookingStatus = { ...cookingStatus, state: "COOKING" };
      return response({ message: "操作已完成" });
    }
    if (url === "/api/cooking/step") {
      cookingStatus = { ...cookingStatus, current_step: { ...cookingStatus.current_step, number: 2 } };
      return response({ message: "下一步" });
    }
    throw new Error(`unexpected request ${url}`);
  };
  await vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  assert.equal(JSON.parse(requests[0].options.body).action, "fresh_ingredients");
  assert.equal(cookingStatus.current_step.number, 1);
  assert.equal(nodes["next-step"].textContent, "下一步 ›", "finally must not restore the stale preparation label");
  await vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  assert.equal(cookingStatus.current_step.number, 2);
  cookingStatus.state = "PAUSED";
  renderCooking();
  assert.equal(nodes["next-step"].textContent, "恢复指导");
  await vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  assert.equal(cookingStatus.state, "COOKING");
  assert.equal(cookingStatus.current_step.number, 2, "resuming must not also advance");
  await vm.runInContext('endCurrentCookingTask($("#cooking-end-task"))', context);
  assert.equal(nodes["active-cooking"].classList.contains("hidden"), true);
  assert.equal(nodes["next-step"].disabled, true);
  assert.equal(nodes["previous-step"].disabled, true);
  const endedRequestCount = requests.length;
  await vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  assert.equal(requests.length, endedRequestCount, "cancelled tasks must never submit another next-step request");
  cookingStatus = { ...cookingStatus, recipe: { name: "旧菜谱" }, current_step: { number: 2, total: 7 } };
  renderCooking();
  assert.equal(nodes["active-cooking"].classList.contains("hidden"), true, "even a stale cancelled snapshot cannot unlock navigation");
  assert.equal(nodes["next-step"].disabled, true);

  cookingStatus = { ...cookingStatus, state: "COOKING", session_active: true,
    current_step: { number: 1, total: 7, instruction: "准备食材。" } };
  renderCooking();
  requests.length = 0;
  let finishStep;
  context.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/status") return response({ robot: {}, camera: {}, kitchen: cookingStatus, timers: [], scope_notice: "" });
    return new Promise((resolve) => { finishStep = resolve; });
  };
  const pendingStep = vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  renderCooking(); // An ordinary status poll must not unlock an in-flight button.
  assert.equal(nodes["next-step"].disabled, true);
  await vm.runInContext('navigateCookingStep("next", $("#next-step"))', context);
  assert.equal(requests.length, 1, "rapid clicks may advance only once");
  cookingStatus.current_step.number = 2;
  finishStep(response({ message: "下一步" }));
  await pendingStep;
  assert.equal(nodes["next-step"].disabled, false);
  assert.equal(nodes["next-step"].textContent, "下一步 ›");

  console.log("Video import and cooking navigation UI checks passed.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
