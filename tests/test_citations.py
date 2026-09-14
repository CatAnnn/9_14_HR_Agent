from backend.rag.citation import (
    merge_citations,
    skill_payloads_to_citations,
    validated_references_to_citations,
)
from backend.schemas.retrieval import Citation, RetrievedChunk


def test_skill_citation_links_only_exact_meaningful_output_phrase():
    core_knowledge = (
        "## 绩效等级说明\n"
        "Partially Achieved 表示部分达到岗位绩效要求，需要明确差距和改进行动。"
    )
    citations = skill_payloads_to_citations(
        [
            {
                "id": "performance",
                "version": "1.0.0",
                "matched_triggers": ["绩效", "绩效反馈"],
                "scopes": ["performance", "feedback"],
                "core_knowledge": core_knowledge,
                "stale": False,
            }
        ],
        {
            "summary": "建议说明 Partially Achieved 表示部分达到岗位绩效要求。",
            "basis": "本次绩效沟通需要更具体。",
        },
    )

    assert len(citations) == 1
    citation = citations[0]
    assert citation.source_type == "skill"
    assert citation.scope == "performance"
    assert citation.title == "绩效等级说明"
    assert citation.quote == core_knowledge
    assert citation.targets == ["summary"]


def test_skill_citation_normalizes_job_level_scope():
    citations = skill_payloads_to_citations(
        [
            {
                "id": "job_level",
                "version": "1.0.1",
                "matched_triggers": ["岗位要求"],
                "scopes": ["job level"],
                "core_knowledge": "# 岗位与职级\n岗位要求需要与对应职级的职责范围保持一致。",
                "stale": False,
            }
        ],
        {"summary": "请将岗位要求与对应职级的职责范围逐项对齐。"},
    )

    assert len(citations) == 1
    assert citations[0].scope == "job_level"


def test_skill_citation_ignores_short_generic_trigger_without_shared_phrase():
    citations = skill_payloads_to_citations(
        [
            {
                "id": "performance",
                "version": "1.0.0",
                "matched_triggers": ["绩效"],
                "scopes": ["performance"],
                "core_knowledge": "# 绩效等级说明\n评价时需要说明实际结果与标准之间的差距。",
                "stale": False,
            }
        ],
        {"summary": "建议把绩效讲清楚。"},
    )

    assert citations == []


def test_merge_citations_combines_targets_and_keeps_fullest_quote():
    citations = merge_citations(
        [
            Citation(
                chunk_id="skill:performance:1",
                source_id="skill:performance",
                title="绩效等级说明",
                scope="performance",
                quote="简短原文",
                targets=["summary"],
                source_type="skill",
            ),
            Citation(
                chunk_id="skill:performance:1",
                source_id="skill:performance",
                title="绩效等级说明",
                scope="performance",
                quote="更完整的绩效等级说明原文",
                targets=["dimension_scores.0.basis", "summary"],
                source_type="skill",
            ),
        ]
    )

    assert len(citations) == 1
    assert citations[0].quote == "更完整的绩效等级说明原文"
    assert citations[0].targets == ["summary", "dimension_scores.0.basis"]

def test_precise_citation_rejects_wrong_scope_for_mission_must_be_accomplished():
    target = "建议围绕使命必达说明可观察行为。"
    source_quote = "使命必达强调围绕结果说明可观察行为。"
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="performance-chunk",
                source_id="performance.md",
                title="绩效指南",
                scope="performance",
                text=source_quote,
            ),
            RetrievedChunk(
                chunk_id="culture-chunk",
                source_id="Be LikeABosch.md",
                title="Be LikeABosch",
                scope="culture",
                text=f"# 高绩效文化\n{source_quote}\n协同共进强调主动合作。",
            ),
        ],
        [],
        {
            "summary": (
                target,
                [
                    {
                        "source_ref": "performance-chunk",
                        "highlight_text": "围绕使命必达说明可观察行为",
                        "source_quote": source_quote,
                    },
                    {
                        "source_ref": "culture-chunk",
                        "highlight_text": "围绕使命必达说明可观察行为",
                        "source_quote": source_quote,
                    },
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].chunk_id == "culture-chunk"
    assert citations[0].scope == "culture"
    assert citations[0].anchors[0].highlight_text == "围绕使命必达说明可观察行为"
    assert "高绩效文化" in citations[0].anchors[0].source_context


def test_precise_citation_highlights_the_complete_english_phrase():
    target = "请用 Commitment to WIN 对齐结果标准。"
    source_quote = "Commitment to WIN means honoring commitments on QCD."
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="culture-commitment-win",
                source_id="culture.md",
                title="Culture",
                scope="culture",
                text=source_quote,
            )
        ],
        [],
        {
            "summary": (
                target,
                [
                    {
                        "source_ref": "culture-commitment-win",
                        "highlight_text": "Commitment to WIN",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert citations[0].anchors[0].highlight_text == "Commitment to WIN"


def test_asr_rating_reference_only_accepts_performance_scope():
    target = "请核对 ASR rating 的正式结果。"
    source_quote = "ASR rating 应以正式绩效流程记录为准。"
    references = [
        {
            "source_ref": "culture-asr",
            "highlight_text": "ASR rating",
            "source_quote": source_quote,
        },
        {
            "source_ref": "performance-asr",
            "highlight_text": "ASR rating",
            "source_quote": source_quote,
        },
    ]
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="culture-asr",
                source_id="culture.md",
                title="Culture",
                scope="culture",
                text=source_quote,
            ),
            RetrievedChunk(
                chunk_id="performance-asr",
                source_id="performance.md",
                title="Performance",
                scope="performance",
                text=source_quote,
            ),
        ],
        [],
        {"summary": (target, references)},
    )

    assert len(citations) == 1
    assert citations[0].chunk_id == "performance-asr"


def test_citation_downgrades_generic_highlight_but_rejects_missing_source_text():
    chunk = RetrievedChunk(
        chunk_id="performance",
        source_id="performance.md",
        title="Performance",
        scope="performance",
        text="绩效评价需要结合事实与标准。",
    )
    citations = validated_references_to_citations(
        [chunk],
        [],
        {
            "summary": (
                "绩效评价需要结合事实与标准。",
                [
                    {
                        "source_ref": "performance",
                        "highlight_text": "绩效",
                        "source_quote": "绩效评价需要结合事实与标准。",
                    },
                    {
                        "source_ref": "performance",
                        "highlight_text": "结合事实与标准",
                        "source_quote": "并不存在于来源中的文字。",
                    },
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].chunk_id == "performance"
    assert citations[0].targets == ["summary"]
    assert citations[0].anchors == []


def test_citation_recovers_exact_spans_across_harmless_formatting_differences():
    target = "建议依据“Career Elements”核对跨职能经历。"
    source_quote = (
        "Career Elements 将 Cross-functional move 列为职业要素之一。"
    )
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="career-normalized",
                source_id="career.md",
                title="Career Elements",
                scope="career",
                text=source_quote,
            )
        ],
        [],
        {
            "summary": (
                target,
                [
                    {
                        "source_ref": "career-normalized",
                        "highlight_text": (
                            '依据 "career elements" 核对跨职能经历'
                        ),
                        "source_quote": (
                            "career elements 将 cross functional move "
                            "列为职业要素之一"
                        ),
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    anchor = citations[0].anchors[0]
    assert anchor.highlight_text == "依据“Career Elements”核对跨职能经历"
    assert anchor.source_quote == source_quote.rstrip("。")
    assert anchor.source_quote in anchor.source_context


def test_citation_accepts_a_compact_multi_clause_highlight():
    source_quote = "发展计划需要形成具体行动、验证方式和后续复盘。"
    highlight = "明确当前基础、具体行动、验证方式和后续复盘"
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="development-plan",
                source_id="development.md",
                title="发展计划",
                scope="development_dialog",
                text=source_quote,
            )
        ],
        [],
        {
            "suggestion": (
                f"建议{highlight}。",
                [
                    {
                        "source_ref": "development-plan",
                        "highlight_text": highlight,
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].anchors[0].highlight_text == highlight


def test_citation_still_rejects_a_paraphrased_source_quote():
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="performance-source",
                source_id="performance.md",
                title="Performance",
                scope="performance",
                text="绩效评价需要结合事实与标准。",
            )
        ],
        [],
        {
            "summary": (
                "建议结合绩效事实与标准进行评价。",
                [
                    {
                        "source_ref": "performance-source",
                        "highlight_text": "结合绩效事实与标准进行评价",
                        "source_quote": "绩效评价需要结合目标和结果。",
                    }
                ],
            )
        },
    )

    assert citations == []


def test_citation_searches_chunk_text_when_parent_context_omits_the_quote():
    source_quote = "直接片段说明需要核对当前事实。"
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="chunk-plus-parent",
                source_id="performance.md",
                title="Performance",
                scope="performance",
                text=source_quote,
                metadata={"parent_context": "父级上下文只提供章节背景。"},
            )
        ],
        [],
        {
            "summary": (
                "建议核对当前事实。",
                [
                    {
                        "source_ref": "chunk-plus-parent",
                        "highlight_text": "核对当前事实",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].anchors[0].source_quote == source_quote


def test_citation_accepts_compact_multiline_source_quote():
    source_quote = "\n".join(
        [
            "发展计划需要明确当前基础，",
            "连接岗位要求，",
            "说明具体行动，",
            "定义可观察结果，",
            "并安排后续复盘。",
        ]
    )
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="multiline-development",
                source_id="development.md",
                title="发展计划",
                scope="development_dialog",
                text=source_quote,
            )
        ],
        [],
        {
            "suggestion": (
                "建议明确行动、结果和后续复盘。",
                [
                    {
                        "source_ref": "multiline-development",
                        "highlight_text": "明确行动、结果和后续复盘",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].anchors[0].source_quote == source_quote


def test_required_scopes_preserve_exact_hover_source_quote():
    cases = [
        (
            "performance",
            "按 WHAT 与 HOW 两部分核对绩效依据",
            "绩效评价需要同时核对 WHAT 与 HOW 两部分的事实依据。",
        ),
        (
            "culture",
            "用使命必达对应可观察行为",
            "使命必达要求把结果责任落实到具体、可观察的行为。",
        ),
        (
            "career",
            "核对 Career Elements 的跨职能经历要求",
            "Career Elements 将 Cross-functional move 列为职业要素之一。",
        ),
    ]

    for scope, highlight_text, source_quote in cases:
        citations = validated_references_to_citations(
            [
                RetrievedChunk(
                    chunk_id=f"{scope}-exact",
                    source_id=f"{scope}.md",
                    title=f"{scope} source",
                    scope=scope,
                    text=f"# {scope}\n{source_quote}\n不相关的下一段原文。",
                )
            ],
            [],
            {
                "summary": (
                    f"建议先{highlight_text}，再结合当前案例补充事实。",
                    [
                        {
                            "source_ref": f"{scope}-exact",
                            "highlight_text": highlight_text,
                            "source_quote": source_quote,
                        }
                    ],
                )
            },
        )

        assert len(citations) == 1
        assert citations[0].scope == scope
        assert citations[0].anchors[0].highlight_text == highlight_text
        assert citations[0].anchors[0].source_quote == source_quote
        assert source_quote in citations[0].anchors[0].source_context


def test_citation_downgrades_overbroad_multi_sentence_highlight():
    target = (
        "先核对绩效事实。再依据绩效评价规则说明标准。"
        "最后明确绩效结论。"
    )
    source_quote = "绩效评价需要基于已经确认的目标与结果事实。"
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="performance-precise",
                source_id="performance.md",
                title="绩效评价",
                scope="performance",
                text=source_quote,
            )
        ],
        [],
        {
            "summary": (
                target,
                [
                    {
                        "source_ref": "performance-precise",
                        "highlight_text": target,
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].targets == ["summary"]
    assert citations[0].anchors == []


def test_citation_downgrades_whole_source_section_quote():
    source_quote = "绩效评价应核对事实。" * 80
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="performance-section",
                source_id="performance.md",
                title="绩效评价",
                scope="performance",
                text=source_quote,
            )
        ],
        [],
        {
            "summary": (
                "建议核对绩效评价的事实依据。",
                [
                    {
                        "source_ref": "performance-section",
                        "highlight_text": "核对绩效评价的事实依据",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    assert citations[0].targets == ["summary"]
    assert citations[0].anchors == []


def test_precise_g9_context_does_not_include_other_job_levels():
    source_quote = "G9 要求独立处理复杂任务并形成跨团队影响。"
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="job-level-g9",
                source_id="job_level.md",
                title="岗位等级标准",
                scope="job_level",
                text=(
                    "# G9 标准\n"
                    f"{source_quote}\n"
                    "SL1 要求负责团队管理与人员发展。\n"
                    "SL2G9 要求承担更大组织范围的责任。"
                ),
            )
        ],
        [],
        {
            "basis": (
                "需要核对 G9 对应的产出要求。",
                [
                    {
                        "source_ref": "job-level-g9",
                        "highlight_text": "G9 对应的产出要求",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert len(citations) == 1
    context = citations[0].anchors[0].source_context
    assert "G9 要求" in context
    assert "SL1" not in context
    assert "SL2G9" not in context


def test_precise_business_code_reference_rejects_mixed_level_quote():
    source_quote = (
        "G9 要求独立处理复杂任务。 "
        "SL1 要求负责团队管理与人员发展。"
    )
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="mixed-levels",
                source_id="job_level.md",
                title="岗位等级标准",
                scope="job_level",
                text=source_quote,
            )
        ],
        [],
        {
            "basis": (
                "需要核对 G9 对应的产出要求。",
                [
                    {
                        "source_ref": "mixed-levels",
                        "highlight_text": "G9 对应的产出要求",
                        "source_quote": source_quote,
                    }
                ],
            )
        },
    )

    assert citations == []


def test_valid_knowledge_base_reference_wins_over_duplicate_skill_reference():
    target = "建议采用逐项核对事实与标准的方法。"
    source_quote = "逐项核对事实与标准，再邀请员工补充信息。"
    references = [
        {
            "source_ref": "kb-method",
            "highlight_text": "逐项核对事实与标准",
            "source_quote": source_quote,
        },
        {
            "source_ref": "skill:feedback:1.0.0",
            "highlight_text": "逐项核对事实与标准",
            "source_quote": source_quote,
        },
    ]
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="kb-method",
                source_id="feedback.md",
                title="反馈方法",
                scope="development_dialog",
                text=source_quote,
            )
        ],
        [
            {
                "id": "feedback",
                "version": "1.0.0",
                "scopes": ["development_dialog"],
                "core_knowledge": source_quote,
                "stale": False,
            }
        ],
        {"suggestion": (target, references)},
    )

    assert len(citations) == 1
    assert citations[0].source_type == "knowledge_base"
    assert citations[0].chunk_id == "kb-method"


def test_same_output_phrase_keeps_distinct_source_evidence():
    target = "建议把目标和事实逐项对齐后再讨论行动。"
    references = [
        {
            "source_ref": "feedback-method",
            "highlight_text": "目标和事实逐项对齐",
            "source_quote": "反馈前先把目标、标准和实际事实逐项对齐。",
        },
        {
            "source_ref": "planning-method",
            "highlight_text": "目标和事实逐项对齐",
            "source_quote": "共同规划应建立在双方已核对目标和事实的基础上。",
        },
    ]
    citations = validated_references_to_citations(
        [
            RetrievedChunk(
                chunk_id="feedback-method",
                source_id="feedback.md",
                title="反馈方法",
                scope="feedback",
                text=references[0]["source_quote"],
            ),
            RetrievedChunk(
                chunk_id="planning-method",
                source_id="planning.md",
                title="共同规划方法",
                scope="general",
                text=references[1]["source_quote"],
            ),
        ],
        [],
        {"summary": (target, references)},
    )

    assert len(citations) == 2
    assert {citation.chunk_id for citation in citations} == {
        "feedback-method",
        "planning-method",
    }
