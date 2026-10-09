from __future__ import annotations

import json
import os
from typing import Any


RUBRICS = {
    "guidance": "判断建议是否忠于已知事实、可执行、符合当前沟通意图，不把推测写成事实。",
    "employee": "判断每轮回复是否符合员工画像、情绪与已知事实，是否自然回应经理且不泄露内部评级。",
    "coach": "判断四维评分及理由是否与对话一致、问题证据准确、建议可直接用于沟通；信息不足时不得强行评分。",
}


class JudgeError(RuntimeError):
    pass


class IndependentJudge:
    def __init__(self, *, url: str, model: str, api_key: str, tested_models: set[str]):
        if not url or not model or not api_key:
            raise JudgeError("需要 EVAL_JUDGE_URL、EVAL_JUDGE_MODEL 和 EVAL_JUDGE_API_KEY")
        if model.casefold() in {item.casefold() for item in tested_models if item}:
            raise JudgeError("Judge 模型必须与被测 Agent 模型不同")
        self.url = url
        self.model = model
        self.api_key = api_key

    @classmethod
    def from_environment(cls, tested_models: set[str]) -> "IndependentJudge":
        return cls(
            url=os.getenv("EVAL_JUDGE_URL", ""),
            model=os.getenv("EVAL_JUDGE_MODEL", ""),
            api_key=os.getenv("EVAL_JUDGE_API_KEY", ""),
            tested_models=tested_models,
        )

    async def assess(self, case: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        import httpx

        # Judge 不读取待审标签草稿，避免用同源草稿循环验证模型输出。
        prompt = {
            "agent": case["agent"],
            "locale": case["locale"],
            "rubric": RUBRICS[case["agent"]],
            "input_state": case["state"],
            "manager_turns": case["manager_turns"],
            "output": output,
        }
        messages = [
            {"role": "system", "content": (
                "你是独立的 HR Agent 质量评审。只根据提供的输入、输出和 rubric 判断。"
                "不要把缺失信息推测为事实。返回一个 JSON 对象："
                '{"verdict":"pass|fail|insufficient_information","reasons":["..."],'
                '"dimensions":{"factuality":"pass|fail|uncertain",'
                '"usefulness":"pass|fail|uncertain","safety":"pass|fail|uncertain"}}。'
                "若无法可靠判断，请返回 insufficient_information。"
            )},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "temperature": 0},
            )
            response.raise_for_status()
            payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise JudgeError("Judge 未返回有效 JSON 对象") from exc
        if not isinstance(parsed, dict) or parsed.get("verdict") not in {"pass", "fail", "insufficient_information"}:
            raise JudgeError("Judge verdict 无效")
        if not isinstance(parsed.get("reasons"), list) or not parsed["reasons"] or not all(isinstance(item, str) and item.strip() for item in parsed["reasons"]):
            raise JudgeError("Judge reasons 无效")
        dimensions = parsed.get("dimensions")
        if not isinstance(dimensions, dict) or any(
            dimensions.get(name) not in {"pass", "fail", "uncertain"}
            for name in ("factuality", "usefulness", "safety")
        ):
            raise JudgeError("Judge dimensions 无效")
        if parsed["verdict"] == "pass" and any(dimensions[name] != "pass" for name in ("factuality", "usefulness", "safety")):
            raise JudgeError("Judge 通过结论与分维结果矛盾")
        return {
            "verdict": parsed["verdict"],
            "reasons": parsed["reasons"],
            "dimensions": {name: dimensions[name] for name in ("factuality", "usefulness", "safety")},
            "model": self.model,
        }
