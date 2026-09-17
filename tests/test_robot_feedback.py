from runtime_core.executor import RuntimeExecutor


def test_robot_feedback_tracks_execution_and_retains_last_output(monkeypatch):
    executor = RuntimeExecutor(no_play=True)
    assert executor.status_snapshot()["expression"] is None
    during_execution = []
    monkeypatch.setattr(executor.voice, "speak", lambda _: during_execution.append(executor.status_snapshot()))
    executor.execute_plan({
        "robot_action": "nod", "led_effect": "warm_white", "expression": "focused",
        "display": "第 2 步：切洋葱", "speech": "现在切洋葱。",
    })
    snapshot = executor.status_snapshot()
    assert during_execution[0]["state"] == "executing"
    assert snapshot["state"] == "idle"
    for field, expected in {
        "action": "nod", "led_effect": "warm_white", "expression": "focused",
        "display": "第 2 步：切洋葱", "speech": "现在切洋葱。",
    }.items():
        assert snapshot[field] == during_execution[0][field] == expected
    executor.execute_plan({
        "robot_action": "invalid", "led_effect": "invalid", "expression": "invalid", "speech": "下一步",
    })
    assert executor.status_snapshot()["action"] == "idle_wait"
    assert executor.status_snapshot()["led_effect"] == "white"
    assert executor.status_snapshot()["expression"] == "neutral"


def test_feedback_events_keep_every_terminal_output_between_polls(monkeypatch, capsys):
    executor = RuntimeExecutor(no_play=True)
    monkeypatch.setattr(executor.voice, "speak", lambda _: None)
    executor.execute_plan({"steps": [
        {"robot_action": "encourage_gesture", "led_effect": "green", "expression": "confident",
         "display": "开始做菜", "speech": "我们开始吧。"},
        {"robot_action": "nod", "led_effect": "warm_white", "expression": "focused",
         "display": "准备食材", "speech": "先切洋葱。"},
        {"robot_action": "nod", "led_effect": "green_dynamic", "expression": "happy",
         "display": "开始计时", "speech": "给你计时。"},
    ]})
    snapshot = executor.status_snapshot()
    events = snapshot["feedback_events"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["led_effect"] for event in events] == ["green", "warm_white", "green_dynamic"]
    assert [event["expression"] for event in events] == ["confident", "focused", "happy"]
    output = capsys.readouterr().out
    for event in events:
        assert f"effect={event['led_effect']}" in output
        assert f"[模拟SDK-表情] {event['expression']}" in output
        assert f"[模拟SDK-语音请求] {event['speech']}" in output
    events[0]["expression"] = "changed"
    assert executor.status_snapshot()["feedback_events"][0]["expression"] == "confident"


def test_feedback_buffer_is_bounded_and_sequence_does_not_reset(monkeypatch):
    executor = RuntimeExecutor(no_play=True)
    monkeypatch.setattr(executor.voice, "speak", lambda _: None)
    for _ in range(70):
        executor.execute_plan({"robot_action": "nod", "led_effect": "blue", "expression": "focused", "speech": "测试"})
    events = executor.status_snapshot()["feedback_events"]
    assert len(events) == 64
    assert events[0]["sequence"] == 7
    assert events[-1]["sequence"] == 70
