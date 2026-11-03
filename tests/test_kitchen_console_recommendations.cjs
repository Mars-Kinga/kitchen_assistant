const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    textContent: "", innerHTML: "", disabled: false, value: selector === "#servings" ? "1" : "正常",
    attributes: {}, listeners: {}, classList: { values: new Set(), add(name) { this.values.add(name); }, remove(name) { this.values.delete(name); }, contains(name) { return this.values.has(name); }, toggle() {} },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute(key, value) { this.attributes[key] = value; }, scrollIntoView() {}
  });
  return nodes.get(selector);
}
const context = vm.createContext({
  document: { querySelector: node, body: { style: {} } }, console,
  TypeError, setTimeout, clearTimeout
});
const source = fs.readFileSync(path.join(__dirname, "../runtime_core/kitchen_console_static/app.js"), "utf8");
// Load the real functions without page bootstrapping/polling.
vm.runInContext(source.slice(0, source.indexOf('$("#capture-server").addEventListener')), context);
vm.runInContext(source.slice(
  source.indexOf('$("#recipe-cards").addEventListener'),
  source.indexOf('$("#cooking-panel").addEventListener')
), context);

(async () => {
  vm.runInContext('ingredients = [{ name: "玉米" }, { name: "牛奶" }]; refreshStatus = async () => {};', context);
  const requests = [];
  let finishRequest;
  context.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return new Promise((resolve) => { finishRequest = resolve; });
  };
  const pending = vm.runInContext("requestRecommendations()", context);
  assert.equal(node("#provider-label").textContent, "生成中");
  assert.equal(node("#recipe-cards").innerHTML, "");
  assert.equal(node("#recipe-cards").attributes["aria-busy"], "true");
  assert.equal(node("#recommend").disabled, true);
  await vm.runInContext("requestRecommendations()", context);
  assert.equal(requests.length, 1, "duplicate submissions must not call AI twice");
  finishRequest({ ok: true, json: async () => ({
    candidates: [], provider_mode: "mock", recommendation_status: "failed",
    recommendation_error: { code: "authentication_failed", message: "接口鉴权失败，请更新配置", retryable: false }
  }) });
  await pending;
  assert.equal(node("#provider-label").textContent, "生成未成功");
  assert.match(node("#recommendation-message").textContent, /鉴权失败/);
  assert.match(node("#recipe-cards").innerHTML, /更新配置后重试/);
  assert.doesNotMatch(node("#recipe-cards").innerHTML, /调整食材/);
  assert.equal(node("#recipe-cards").attributes["aria-busy"], "false");
  assert.equal(node("#recommend").disabled, false);

  context.fetch = async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ provider_mode: "ai_generated", candidates: [
      { candidate_id: "milk_corn", title: "牛奶玉米", main_ingredients: ["玉米", "牛奶"], unused_ingredients: [] }
    ] }) };
  };
  await vm.runInContext("submitRecommendations(lastRecommendationRequest)", context);
  assert.deepEqual(requests[1], requests[0], "retry must preserve the confirmed request");
  assert.equal(node("#provider-label").textContent, "AI 生成菜谱");
  assert.match(node("#recipe-cards").innerHTML, /牛奶玉米/);
  assert.match(node("#recipe-cards").innerHTML, /data-servings="1"/);
  assert.doesNotMatch(node("#recipe-cards").innerHTML, /data-retry-recommendations/);

  context.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await vm.runInContext("requestRecommendations()", context);
  assert.match(node("#recommendation-message").textContent, /无法连接局域网控制台/);
  assert.match(node("#recipe-cards").innerHTML, /重新生成/);
  assert.equal(node("#recommend").disabled, false);
  vm.runInContext('renderRecipes({ candidates: [] })', context);
  assert.match(node("#recommendation-message").textContent, /未收到可用菜谱/);

  const detailRequests = [];
  const selectRequests = [];
  context.fetch = async (url, options) => {
    if (url.startsWith("/api/recipes/milk_corn?")) {
      detailRequests.push(url);
      const servings = Number(new URL(url, "http://localhost").searchParams.get("servings"));
      return { ok: true, json: async () => ({ name: "牛奶玉米", servings,
        ingredients: [{ name: "牛奶", amount: 100 * servings, unit: "毫升" }], steps: []
      }) };
    }
    if (url === "/api/recipes/select") selectRequests.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({}) };
  };
  vm.runInContext("updateStatus = () => {};", context);
  const clickRecommendation = async () => {
    const html = node("#recipe-cards").innerHTML;
    const card = { dataset: {
      recipeId: html.match(/data-recipe-id="([^"]+)"/)[1],
      servings: html.match(/data-servings="([^"]+)"/)[1]
    } };
    return node("#recipe-cards").listeners.click({ target: {
      closest(selector) { return selector === "[data-recipe-id]" ? card : null; }
    } });
  };
  for (let servings = 1; servings <= 6; servings++) {
    vm.runInContext(`renderRecipes({ candidates: [{ candidate_id: "milk_corn", title: "牛奶玉米" }] }, ${servings})`, context);
    // Changing unrelated selectors or a previous sheet must not override the
    // number captured when these recommendations were generated.
    node("#servings").value = "6";
    node("#sheet-servings").value = "2";
    await clickRecommendation();
    assert.equal(node("#sheet-servings").value, String(servings));
    assert.equal(detailRequests.at(-1), `/api/recipes/milk_corn?servings=${servings}`);
    assert.match(node("#sheet-ingredients").innerHTML, new RegExp(`${100 * servings}毫升`));
    await vm.runInContext("selectActiveRecipe()", context);
    assert.equal(selectRequests.at(-1).servings, servings);
  }
  // Explicit edits inside the sheet remain effective and must not reset to
  // generation defaults during the change-handler's detail reload.
  node("#sheet-servings").value = "3";
  await vm.runInContext('openRecipeSheet("milk_corn", Number($("#sheet-servings").value))', context);
  assert.equal(detailRequests.at(-1), "/api/recipes/milk_corn?servings=3");
  await vm.runInContext("selectActiveRecipe()", context);
  assert.equal(selectRequests.at(-1).servings, 3);

  node("#servings").value = "1";
  node("#sheet-servings").value = "2";
  await vm.runInContext('openRecipeSheet("milk_corn")', context);
  assert.equal(node("#sheet-servings").value, "1", "opening a closed recipe uses the current meal size instead of stale sheet state");
  assert.equal(detailRequests.at(-1), "/api/recipes/milk_corn?servings=1");
  await vm.runInContext("selectActiveRecipe()", context);
  assert.equal(selectRequests.at(-1).servings, 1);

  node("#servings").value = "1";
  context.fetch = async () => ({ ok: true, json: async () => ({
    provider_mode: "ai_generated", candidates: [{ candidate_id: "named_recipe", title: "新菜谱" }]
  }) });
  await vm.runInContext('submitRecommendations({ path: "/api/recipes/generate", body: { dish_name: "新菜谱", servings: 5 } })', context);
  assert.match(node("#recipe-cards").innerHTML, /data-servings="5"/);
  console.log("Recommendation UI regression checks passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
