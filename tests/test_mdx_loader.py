from core.loaders.mdx import MDXLoader, clean_mdx


def test_codegroup_wrapper_stripped_code_kept():
    out = clean_mdx("Intro\n\n<CodeGroup>\n\n```python Python\nx = 1\n```\n\n</CodeGroup>\n")
    assert "<CodeGroup>" not in out and "</CodeGroup>" not in out
    assert "```python Python\nx = 1\n```" in out


def test_accordion_title_kept_as_bold_line():
    out = clean_mdx('<Accordion title="Can I use Cloud?">\n  Yes, you can.\n</Accordion>')
    assert "**Can I use Cloud?**" in out and "Yes, you can." in out


def test_img_alt_becomes_caption_and_bare_img_dropped():
    assert "_Architecture diagram_" in clean_mdx('<img src="a.png" alt="Architecture diagram" />')
    assert clean_mdx('<img src="a.png" />').strip() == ""


def test_jsx_inside_code_fence_untouched():
    body = "```jsx\n<Accordion title=\"x\">hi</Accordion>\n```"
    assert clean_mdx(body).strip() == body


def test_liquid_capture_becomes_fenced_code_and_include_dropped():
    body = (
        "Intro\n\n{% capture shell_0 %}\ngraph.config get *\n# Output:\n{% endcapture %}\n\n"
        '{% include code_tabs.html id="x" shell=shell_0 %}\n'
    )
    out = clean_mdx(body)
    assert "{%" not in out
    assert "```shell\ngraph.config get *\n# Output:\n```" in out


def test_captured_comment_lines_do_not_become_headings():
    body = "# Title\n\n{% capture shell_0 %}\n# Output:\n{% endcapture %}\n"
    elements = MDXLoader()._md_parser._parse_markdown(clean_mdx(body))
    assert [e.content for e in elements if e.type == "header"] == ["Title"]


def test_kramdown_ial_line_removed():
    assert "{:" not in clean_mdx("Careful here.\n{: .warning }\n")
