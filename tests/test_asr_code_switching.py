from backend.services.bosch_realtime_asr_service import _merge_transcript
from backend.services.recording_asr_service import (
    join_overlapping_segment_transcripts,
    join_segment_transcripts,
)


def test_code_switched_segments_preserve_natural_cjk_ascii_boundaries():
    assert join_segment_transcripts(
        ["这次 Performance Rating", "需要结合G9要求说明。"]
    ) == "这次 Performance Rating需要结合G9要求说明。"
    assert join_segment_transcripts(
        ["The Career Elements", "plan is ready."]
    ) == "The Career Elements plan is ready."


def test_code_switched_overlap_is_deduplicated_across_final_segments():
    assert join_overlapping_segment_transcripts(
        [
            "我们需要对齐 Career Elements 和 Performance Rating",
            "Career Elements 和 Performance Rating 的适用标准。",
        ]
    ) == "我们需要对齐 Career Elements 和 Performance Rating的适用标准。"


def test_code_switched_preview_revision_does_not_repeat_english_term():
    assert _merge_transcript(
        "下一步讨论 Career Elements",
        "Career Elements 与G9的发展要求",
    ) == "下一步讨论 Career Elements 与G9的发展要求"
