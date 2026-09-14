from __future__ import annotations

from typing import Any

from loadtests.accounts import LoadTestAccount


MANAGER_MESSAGES = (
    "你好，今天我们一起回顾本周期目标和实际结果，请先说说你的整体判断。",
    "关于重点项目交付，请结合具体事实说明已经完成的结果和仍有差距的部分。",
    "我听到你对资源协同有顾虑。哪些支持最能帮助你稳定达到目标？",
    "接下来我们把改进动作、衡量标准和时间节点逐项确认下来。",
    "最后请复述我们达成的行动计划，并指出还需要我协调的事项。",
)


def profile_payload(account: LoadTestAccount) -> dict[str, Any]:
    marker = f"LT-{account.index:04d}"
    return {
        "profile": {
            "employee_id": marker,
            "employee_alias": f"压测员工{account.index:04d}",
            "role": "Senior Project Manager",
            "department": "Mobility Solutions",
            "level": "G9",
            "performance_rating": "2",
            "tcl": "+++",
            "review_cycle": "2026 Mid-Year",
            "conversation_topic": "绩效反馈与发展计划",
            "key_goals": [
                "按期交付关键客户项目并保持质量指标达标",
                "推动跨职能协作并形成可复用的项目方法",
            ],
            "facts": [
                {
                    "description": f"{marker} 项目里程碑按计划完成，复盘材料已提交。",
                    "impact": "客户交付保持稳定，跨团队问题得到闭环。",
                    "evidence_source": "load-test-fixture",
                }
            ],
            "historical_feedback": ["需要进一步提升跨部门影响力和授权能力。"],
            "employee_status_summary": "愿意承担更复杂职责，并希望获得清晰的发展路径。",
            "supplemental_info": f"隔离标识 {marker}。本字段用于检测跨用户数据混用。",
            "source_profile_text": f"员工隔离标识：{marker}；岗位：Senior Project Manager。",
        }
    }


def intent_request(intent_id: str, performance_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"intent_id": intent_id, "performance_items": performance_items}


def simulation_payload() -> dict[str, Any]:
    return {
        "personality": {
            "openness": 62,
            "conscientiousness": 68,
            "extraversion": 52,
            "agreeableness": 58,
            "neuroticism": 42,
        },
        "primary_motive_id": "recognition",
        "secondary_motive_ids": ["security", "affiliation"],
        "run_mode": "guidance_then_rehearsal",
    }

