from backend.parsers.text_parser import TextParser


def test_text_parser_removes_empty_lines():
    parsed = TextParser().parse("A\n\n B ", document_id="d1")
    assert parsed.text == "A\nB"
