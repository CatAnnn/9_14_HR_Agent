from __future__ import annotations

import json
from pathlib import Path


DASHBOARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "observability"
    / "signoz"
    / "dashboards"
    / "hr-agent-operations-api-cost-otlp-v1.json"
)
USER_SYSTEM_DASHBOARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "observability"
    / "signoz"
    / "dashboards"
    / "hr-agent-users-system-otlp-v1.json"
)


def test_signoz_dashboard_is_v5_and_has_consistent_layout() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    assert dashboard["version"] == "v5"
    assert dashboard["title"] == "HR Agent API 成本与质量"
    widget_ids = [widget["id"] for widget in dashboard["widgets"]]
    layout_ids = [item["i"] for item in dashboard["layout"]]
    assert len(widget_ids) == 12
    assert len(widget_ids) == len(set(widget_ids))
    assert set(layout_ids) == set(widget_ids)
    query_ids = [
        widget["query"]["id"]
        for widget in dashboard["widgets"]
        if "query" in widget
    ]
    assert len(query_ids) == len(set(query_ids))


def test_signoz_dashboard_is_compact_and_covers_cost_usage_and_quality() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(dashboard, ensure_ascii=False)
    titles = {widget["title"] for widget in dashboard["widgets"]}
    metric_names = {
        aggregation["metricName"]
        for widget in dashboard["widgets"]
        for query in widget.get("query", {}).get("builder", {}).get("queryData", [])
        for aggregation in query.get("aggregations", [])
    }

    assert {
        "hr_agent.llm.cost",
        "hr_agent.llm.request",
        "hr_agent.llm.pricing.missing",
        "hr_agent.llm.token.usage",
        "hr_agent.llm.request.duration.bucket",
        "hr_agent.llm.structured_output",
        "signoz_latency.bucket",
        "signoz_calls_total",
    } <= metric_names
    assert {
        "Token 计费总额（¥）",
        "模型调用次数",
        "Token 总量",
        "未配置价格调用",
        "各模型费用趋势（¥）",
        "输入/输出 Token 趋势",
        "模型调用 P95",
        "结构化输出降级率",
        "后端 API P95",
        "后端 API 错误率",
    } <= titles
    assert "container.cpu.utilization" not in metric_names
    assert "container.memory.percent" not in metric_names
    assert "hr_agent.llm.stream.milestone.duration.bucket" not in metric_names
    assert "session_id" not in serialized
    assert "user_id" not in serialized
    assert "stepInterval" not in serialized
    assert all(
        widget.get("yAxisUnit") == "none"
        for widget in dashboard["widgets"]
        if "¥" in widget["title"]
    )


def test_signoz_dashboard_avoids_high_cardinality_quality_charts() -> None:
    dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    widgets = {widget["title"]: widget for widget in dashboard["widgets"]}

    latency_query = widgets["模型调用 P95"]["query"]["builder"]["queryData"][0]
    assert [item["key"] for item in latency_query["groupBy"]] == [
        "gen_ai.request.model"
    ]

    for title in ("结构化输出降级率", "后端 API P95", "后端 API 错误率"):
        for query in widgets[title]["query"]["builder"]["queryData"]:
            assert query["groupBy"] == []


def test_user_system_dashboard_is_compact_and_has_consistent_layout() -> None:
    dashboard = json.loads(USER_SYSTEM_DASHBOARD_PATH.read_text(encoding="utf-8"))

    assert dashboard["version"] == "v5"
    assert dashboard["title"] == "HR Agent 多用户与系统健康"
    widget_ids = [widget["id"] for widget in dashboard["widgets"]]
    layout_ids = [item["i"] for item in dashboard["layout"]]
    assert len(widget_ids) == 11
    assert len(widget_ids) == len(set(widget_ids))
    assert set(layout_ids) == set(widget_ids)
    query_ids = [
        widget["query"]["id"]
        for widget in dashboard["widgets"]
        if "query" in widget
    ]
    assert len(query_ids) == len(set(query_ids))


def test_user_system_dashboard_covers_users_api_and_critical_containers() -> None:
    dashboard = json.loads(USER_SYSTEM_DASHBOARD_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(dashboard, ensure_ascii=False)
    widgets = {widget["title"]: widget for widget in dashboard["widgets"]}
    metric_names = {
        aggregation["metricName"]
        for widget in dashboard["widgets"]
        for query in widget.get("query", {}).get("builder", {}).get("queryData", [])
        for aggregation in query.get("aggregations", [])
    }

    assert {
        "hr_agent.llm.cost",
        "hr_agent.llm.token.usage",
        "hr_agent.system.gpu.utilization",
        "signoz_calls_total",
        "signoz_latency.bucket",
        "container.cpu.utilization",
        "system.cpu.logical.count",
    } <= metric_names
    assert {
        "当前范围使用用户数",
        "当前范围使用用户",
        "各用户总费用（¥）",
        "各用户总 Token",
        "后端 API 请求速率",
        "后端 API P95",
        "后端 API 错误率",
        "后端 CPU 总使用率（%）",
        "后端 GPU 总使用率（%）",
    } <= set(widgets)
    assert "已认证请求数" not in widgets
    assert "hr_agent.user.active" not in metric_names
    assert "container.memory.percent" not in metric_names

    for title in ("当前范围使用用户数", "当前范围使用用户"):
        query = widgets[title]["query"]
        sql = query["clickhouse_sql"][0]["query"]
        assert query["queryType"] == "clickhouse_sql"
        assert "hr_agent.user.request" in sql
        assert "hr_agent.user.id" in sql
        assert "user_id" not in sql
        assert "{{.start_timestamp_ms}}" in sql
        assert "{{.end_timestamp_ms}}" in sql
        assert "$deployment.environment" in sql
        assert "$service.name" in sql
        assert "temporality = 'Delta'" in sql
        assert "account_name != ''" in sql
        assert "!= 'system'" in sql

    for title in ("各用户总费用（¥）", "各用户总 Token"):
        assert widgets[title]["panelTypes"] == "table"
        assert widgets[title]["timePreferance"] == "GLOBAL_TIME"
        query = widgets[title]["query"]["builder"]["queryData"][0]
        assert [item["key"] for item in query["groupBy"]] == ["hr_agent.user.id"]
        assert "hr_agent.user.id != 'system'" in query["filter"]["expression"]
        aggregation = query["aggregations"][0]
        assert aggregation["timeAggregation"] == "increase"
        assert aggregation["spaceAggregation"] == "sum"
        assert aggregation["reduceTo"] == "sum"

    cpu_widget = widgets["后端 CPU 总使用率（%）"]
    cpu_queries = cpu_widget["query"]["builder"]["queryData"]
    assert [query["aggregations"][0]["metricName"] for query in cpu_queries] == [
        "container.cpu.utilization",
        "system.cpu.logical.count",
    ]
    cpu_filter = cpu_queries[0]["filter"]["expression"]
    assert "docker.compose.project = '06-emotion-main'" in cpu_filter
    assert "docker.compose.service = 'backend'" in cpu_filter
    assert "container.name" not in cpu_filter
    assert cpu_queries[1]["aggregations"][0]["spaceAggregation"] == "max"
    assert cpu_widget["query"]["builder"]["queryFormulas"][0]["expression"] == "A/B"
    assert cpu_widget["softMin"] == 0
    assert cpu_widget["softMax"] == 100

    gpu_widget = widgets["后端 GPU 总使用率（%）"]
    gpu_query = gpu_widget["query"]["builder"]["queryData"][0]
    assert gpu_query["aggregations"][0]["metricName"] == (
        "hr_agent.system.gpu.utilization"
    )
    assert gpu_query["aggregations"][0]["spaceAggregation"] == "avg"
    assert gpu_widget["query"]["builder"]["queryFormulas"][0]["expression"] == "100*A"
    assert gpu_widget["softMin"] == 0
    assert gpu_widget["softMax"] == 100

    for title in ("后端 CPU 总使用率（%）", "后端 GPU 总使用率（%）"):
        assert widgets[title]["yAxisUnit"] == "percent"

    assert widgets["后端 API 错误率"]["yAxisUnit"] == "percent"
    assert widgets["后端 API 错误率"]["softMin"] == 0
    assert widgets["后端 API 错误率"]["softMax"] == 100
    assert (
        widgets["后端 API 错误率"]["query"]["builder"]["queryFormulas"][0][
            "expression"
        ]
        == "100*A/B"
    )
    assert widgets["后端 API 请求速率"]["yAxisUnit"] == "reqps"
    assert widgets["各用户总 Token"]["yAxisUnit"] == "short"
    assert widgets["各用户总费用（¥）"]["decimalPrecision"] == 4
    assert all(
        widget.get("timePreferance") == "GLOBAL_TIME"
        for widget in dashboard["widgets"]
        if "query" in widget
    )
    graph_widgets = [
        widget for widget in dashboard["widgets"] if widget["panelTypes"] == "graph"
    ]
    assert all(widget["softMin"] == 0 for widget in graph_widgets)
    assert all(widget["legendPosition"] == "bottom" for widget in graph_widgets)
    for item in dashboard["layout"]:
        assert item["w"] > 0
        assert item["h"] > 0
        assert 0 <= item["x"] < 12
        assert item["x"] + item["w"] <= 12
    for index, left in enumerate(dashboard["layout"]):
        for right in dashboard["layout"][index + 1 :]:
            horizontal_overlap = (
                left["x"] < right["x"] + right["w"]
                and right["x"] < left["x"] + left["w"]
            )
            vertical_overlap = (
                left["y"] < right["y"] + right["h"]
                and right["y"] < left["y"] + left["h"]
            )
            assert not (horizontal_overlap and vertical_overlap)
    assert "session_id" not in serialized
    assert "email" not in serialized.lower()
    assert "@bosch" not in serialized.lower()
    assert "percentunit" not in serialized
    assert "15 分钟" not in serialized
    assert "hr_agent.activity.window" not in serialized
    assert "邮箱 @ 前的账号名" in serialized
    assert "stepInterval" not in serialized
