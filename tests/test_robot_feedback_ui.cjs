const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const nodes = new Map();
const node = (selector) => {
  if (!nodes.has(selector)) nodes.set(selector, { textContent: "", hidden: true });
  return nodes.get(selector);
};
const timers = new Map();
let timerId = 0;
const context = vm.createContext({ document: { querySelector: node }, console,
  setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
  clearTimeout(id) { timers.delete(id); }
});
const source = fs.readFileSync(path.join(__dirname, "../runtime_core/kitchen_console_static/app.js"), "utf8");
vm.runInContext(source.slice(0, source.indexOf('$("#capture-server").addEventListener')), context);
vm.runInContext("renderRobotFeedback({ simulated: true, feedback_stream_id: 'run-1', feedback_events: [] })", context);
assert.doesNotMatch(source, /#robot-feedback-state/);
const events = [
  { sequence: 1, action: "nod", led_effect: "warm_white", expression: "focused", display: "步骤 2/4：切洋葱", speech: "<b>切洋葱</b>", simulated: true },
  { sequence: 2, action: "encourage_gesture", led_effect: "green_dynamic", expression: "happy", display: "开始计时", speech: "给你计时", simulated: true },
  { sequence: 3, action: "high_five", led_effect: "rainbow", expression: "excited", display: "完成", speech: "做得很好", simulated: true }
];
context.robot = { feedback_stream_id: "run-1", feedback_events: events.slice(0, 1) };
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(node("#feedback-action").textContent, "点头");
assert.equal(node("#feedback-light").textContent, "暖白低亮");
assert.equal(node("#feedback-expression").textContent, "专注");
assert.equal(node("#feedback-display").textContent, "步骤 2/4：切洋葱");
assert.equal(node("#feedback-speech").textContent, "<b>切洋葱</b>", "speech is text, never HTML");
// Repeated polls must not schedule delays or replay old records.
vm.runInContext("renderRobotFeedback(robot); renderRobotFeedback(robot)", context);
assert.equal(timers.size, 0);
context.robot.feedback_events = events.slice(0, 2);
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(node("#feedback-action").textContent, "鼓励手势");
assert.equal(node("#feedback-light").textContent, "绿色动态");
assert.equal(node("#feedback-expression").textContent, "开心");
assert.equal(node("#feedback-speech").textContent, "给你计时", "new feedback must replace old speech immediately");
context.robot.feedback_events = events;
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(node("#feedback-light").textContent, "彩色庆祝");
assert.equal(node("#feedback-expression").textContent, "兴奋");
assert.equal(node("#feedback-speech").textContent, "做得很好", "the panel must retain the latest record");
assert.equal(node("#feedback-expression").textContent, "兴奋");
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(timers.size, 0, "idle polling must not replay previous records");
context.robot.feedback_events = events.slice(0, 1);
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(node("#feedback-speech").textContent, "做得很好", "a stale poll must not roll feedback back");
// A runtime restart resets sequence numbers.
context.robot = { feedback_stream_id: "run-2", feedback_events: [events[0]] };
vm.runInContext("renderRobotFeedback(robot)", context);
assert.equal(node("#feedback-light").textContent, "暖白低亮");
assert.equal(node("#feedback-expression").textContent, "专注");
assert.equal(timers.size, 0, "feedback must never wait on a display timer");
assert.match(source, /setInterval\(refreshStatus, 500\)/);
const html = fs.readFileSync(path.join(__dirname, "../runtime_core/kitchen_console_static/index.html"), "utf8");
const overview = html.slice(html.indexOf('<section class="status-grid"'), html.indexOf('<section class="workspace-grid"'));
assert.match(overview, /id="camera-frame"/);
assert.match(overview, /id="timer-state"/);
assert.match(overview, /id="robot-state"/);
assert.match(overview, /id="robot-detail"/);
assert.match(overview, /<strong>机器人：饭宝<\/strong>/);
assert.match(overview, /<strong>计时器<\/strong>/);
assert.match(overview, /摄像头：在线待机/);
assert.match(overview, /点击“刷新画面”读取一帧/);
assert.match(overview, /id="timer-state">等待启动/);
assert.doesNotMatch(overview, /<p>机器人<\/p>|<p>计时器<\/p>/);
assert.doesNotMatch(overview, /<p>摄像头画面<\/p>|camera-overview-status|camera-detail/);
assert.ok(overview.indexOf('id="camera-state"') < overview.indexOf('id="camera-frame"'));
const css = fs.readFileSync(path.join(__dirname, "../runtime_core/kitchen_console_static/styles.css"), "utf8");
assert.match(css, /\.camera-overview \.camera-frame \{ height: 128px;/);
assert.doesNotMatch(css, /\.timer-card h2 \{[^}]*font-size:/);
assert.doesNotMatch(html, /robot-feedback-toast/);
assert.doesNotMatch(html, /id="robot-feedback-state"/);
assert.equal((overview.match(/<article class="status-card/g) || []).length, 3);
assert.ok(overview.indexOf('id="robot-state"') < overview.indexOf('id="camera-frame"'));
assert.ok(overview.indexOf('id="robot-state"') < overview.indexOf('id="timer-state"'));
assert.ok(overview.indexOf('id="timer-state"') < overview.indexOf('id="camera-frame"'));
const workspace = html.slice(html.indexOf('<section class="workspace-grid"'), html.indexOf('<section class="panel video-import-panel"'));
assert.match(workspace, /robot-feedback-panel/);
assert.doesNotMatch(workspace, /id="camera-frame"/);
for (const id of ["capture-server", "camera-frame", "camera-preview", "camera-state", "feedback-light", "feedback-expression", "feedback-speech"]) {
  assert.equal(html.split(`id="${id}"`).length - 1, 1, `${id} must be unique after moving panels`);
}
console.log("Robot feedback live update and layout checks passed");
