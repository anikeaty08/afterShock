# Tests for the evidence resolver's string parsing (SYSTEM_DESIGN.md §7.2,
# §16 Day-1 spike). These pin down the exact context-section formats
# graphrag_sdk 1.4.0 actually emits (verified against its installed source),
# so an SDK upgrade that changes them breaks a test here, not silently in
# production.

from core.evidence import Hop, parse_entity_lines, parse_hop_line, parse_passage_blocks


class TestParseHopLine:
    def test_one_hop_with_fact(self):
        line = "- Alice —[WORKS_AT]→ Acme Corp: Alice is the CTO of Acme Corp"
        hops = parse_hop_line(line)
        assert hops == [
            Hop("Alice", "WORKS_AT", "Acme Corp", "Alice is the CTO of Acme Corp")
        ]

    def test_one_hop_without_fact(self):
        line = "Alice —[KNOWS]→ Bob"
        hops = parse_hop_line(line)
        assert hops == [Hop("Alice", "KNOWS", "Bob", None)]

    def test_two_hop_chain_never_carries_a_fact(self):
        line = "Alice —[KNOWS]→ Bob —[WORKS_AT]→ Acme Corp"
        hops = parse_hop_line(line)
        assert hops == [
            Hop("Alice", "KNOWS", "Bob", None),
            Hop("Bob", "WORKS_AT", "Acme Corp", None),
        ]

    def test_empty_and_non_hop_lines_return_nothing(self):
        assert parse_hop_line("") == []
        assert parse_hop_line("## Knowledge Graph Facts") == []
        assert parse_hop_line("just a plain sentence, no arrow") == []

    def test_fact_text_containing_a_colon_only_splits_on_first(self):
        line = "GraphSchema —[REPLACED_BY]→ Ontology: renamed in v1.2: see changelog"
        hops = parse_hop_line(line)
        assert hops[0].fact_text == "renamed in v1.2: see changelog"


class TestParseEntityLines:
    def test_mixed_with_and_without_description(self):
        content = "## Key Entities\n- Alice: A person\n- Acme Corp\n- Bob: works in London"
        names = parse_entity_lines(content)
        assert names == ["Alice", "Acme Corp", "Bob"]

    def test_no_entities(self):
        assert parse_entity_lines("## Key Entities\n") == []


class TestParsePassageBlocks:
    def test_single_sourced_passage(self):
        content = "## Source Document Passages\n[Source: graphrag/getting-started.mdx]\nInstall with pip install graphrag-sdk."
        blocks = parse_passage_blocks(content)
        assert blocks == [
            ("graphrag/getting-started.mdx", "Install with pip install graphrag-sdk.")
        ]

    def test_multiple_passages_separated_by_rule(self):
        content = (
            "## Source Document Passages\n"
            "[Source: a.mdx]\nFirst passage text.\n"
            "\n---\n"
            "[Source: b.mdx]\nSecond passage text."
        )
        blocks = parse_passage_blocks(content)
        assert blocks == [
            ("a.mdx", "First passage text."),
            ("b.mdx", "Second passage text."),
        ]

    def test_passage_without_source_prefix_keeps_text_with_none_doc(self):
        content = "## Source Document Passages\nSome unattributed passage."
        blocks = parse_passage_blocks(content)
        assert blocks == [(None, "Some unattributed passage.")]
