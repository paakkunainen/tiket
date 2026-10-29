"""Markdown -> Jira wiki markup, HTML -> markdown-ish text, and the ticket markdown document."""

import pytest

from tiket.backends.azure_devops import _html
from tiket.backends.base import (
    Choice,
    Comment,
    Profile,
    Ticket,
    _demote,
    html_to_text,
    rank,
)
from tiket.backends.jira import _jql_str, md_to_jira


# --- md_to_jira ---------------------------------------------------------------

@pytest.mark.parametrize(
    "md, wiki",
    [
        ("# Title", "h1. Title"),
        ("### Steps", "h3. Steps"),
        ("- one", "* one"),
        ("* one", "* one"),
        ("  - nested", "** nested"),
        ("    - deeper", "*** deeper"),
        ("1. first", "# first"),
        ("  2) second", "## second"),
        ("---", "----"),
        ("**bold** text", "*bold* text"),
        ("__bold__", "*bold*"),
        ("an *italic* word", "an _italic_ word"),
        ("use `a*b*c` here", "use {{a*b*c}} here"),
        ("[docs](https://x.test/a)", "[docs|https://x.test/a]"),
        ("plain text", "plain text"),
    ],
)
def test_md_to_jira_lines(md, wiki):
    assert md_to_jira(md) == wiki


def test_md_to_jira_tables():
    md = "Intro\n\n| Legacy | Replaced by | Switch? |\n|---|:---:|---|\n| FUNDING | `F_FUNDING_GRANT` + **D_X** | Yes |\n| FUNDING_EVENT_TEXT | | No |\n\n---"
    assert md_to_jira(md) == (
        "Intro\n\n||Legacy||Replaced by||Switch?||\n"
        "|FUNDING|{{F_FUNDING_GRANT}} + *D_X*|Yes|\n"
        "|FUNDING_EVENT_TEXT| |No|\n\n----"
    )


def test_md_to_jira_table_without_header():
    assert md_to_jira("| a | b |\n| c | d |") == "|a|b|\n|c|d|"


def test_md_to_jira_code_blocks_are_left_alone():
    md = "```sql\nSELECT **x** FROM t; -- # not a heading\n```\n- after"
    assert md_to_jira(md) == "{code:sql}\nSELECT **x** FROM t; -- # not a heading\n{code}\n* after"


def test_md_to_jira_closes_unterminated_code_block():
    assert md_to_jira("```\ncode") == "{code}\ncode\n{code}"


def test_md_to_jira_full_ticket():
    md = "## Kuvaus\n\nLataus **kaatuu**.\n\n## Hyväksymiskriteerit\n\n- Toimii\n- `ajo` onnistuu"
    assert md_to_jira(md) == "h2. Kuvaus\n\nLataus *kaatuu*.\n\nh2. Hyväksymiskriteerit\n\n* Toimii\n* {{ajo}} onnistuu"


def test_jql_string_escaping():
    assert _jql_str('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


def test_markdown_to_html_for_azure_devops():
    html = _html("## Steps\n\n- a\n- b\n\n```\ncode\n```\n\n| x | y |\n|---|---|\n| 1 | 2 |")
    assert "<h2>Steps</h2>" in html
    assert "<li>a</li>" in html
    assert "<code>code" in html
    assert "<table>" in html


# --- html_to_text -------------------------------------------------------------

def test_html_to_text_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_html_to_text_structure():
    html = (
        "<h2>Steps</h2><p>Do <b>this</b> &amp; <strong>that</strong>&nbsp;now</p>"
        "<ul><li><p>a</p></li><li><p>b</p></li></ul><p>after</p>"
        "<script>alert(1)</script><style>p{}</style>"
    )
    assert html_to_text(html) == "## Steps\nDo **this** & **that** now\n\n- a\n- b\n\nafter"


def test_html_to_text_tables_are_tight():
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table><p>x</p>"
    assert html_to_text(html) == "A | B\n1 | 2\n\nx"


def test_html_to_text_code():
    html = '<p>Run <code>make</code>:</p><pre class="code-java"><span>int</span> x = 1;\n</pre>'
    assert html_to_text(html) == "Run `make`:\n\n```\nint x = 1;\n```"


@pytest.mark.parametrize(
    "a, text",
    [
        ('<a href="https://x.test/doc">the doc</a>', "[the doc](https://x.test/doc)"),
        ('<a href="https://x.test/doc">https://x.test/doc</a>', "https://x.test/doc"),
        ('<a href="#anchor">jump</a>', "jump"),
        ('<a href="https://j/secure/ViewProfile.jspa?name=m" class="user-hover">Matti</a>', "Matti"),
        ('<a href="https://j/browse/BI-1" class="issue-link">BI-1</a>', "BI-1"),
        ('<a href="https://x.test/a"><b>bold</b> link</a>', "[bold link](https://x.test/a)"),
    ],
)
def test_html_to_text_links(a, text):
    assert html_to_text(a) == text


# --- models -------------------------------------------------------------------

def test_rank_orders_by_count_then_newest():
    choices = rank([("B", "b"), ("A", "a"), ("A", "a"), ("C", "c"), ("B", "b"), ("A", "a")])
    assert [(c.value, c.count) for c in choices] == [("A", 3), ("B", 2), ("C", 1)]
    # ties keep first-seen (= newest) order
    assert [c.value for c in rank([("new", "n"), ("old", "o")])] == ["new", "old"]


def test_profile_round_trip():
    p = Profile(
        sample=3,
        parents=[Choice("BI-1", "BI-1 Epic", 2)],
        components=[Choice("UI", "UI", 1)],
        labels=[Choice("ai", "ai", 1)],
        priorities=[Choice("Medium", "Medium", 3)],
        points=[("Title", 2.0)],
        area="Esko\\Team",
        sprint_hint={"board": 7, "name": "S 1"},
        field_ids={"story_points": "customfield_1"},
    )
    assert Profile.from_dict(p.to_dict()) == p


def test_demote_skips_code_blocks_and_caps_at_h6():
    md = "# Top\ntext #not\n```\n# comment in code\n```\n##### Deep"
    assert _demote(md, 2) == "### Top\ntext #not\n```\n# comment in code\n```\n###### Deep"


def test_ticket_markdown():
    t = Ticket(
        ref="BI-1",
        title="Broken export",
        url="https://cloud.test/browse/BI-1",
        issue_type="Bug",
        status="Open",
        project="BI",
        created="2026-01-02T03:04:05.000+0200",
        updated="2026-01-03T10:00:00Z",
        fields={"Assignee": "Me", "Sprint": "", "Labels": "a, b"},
        description="# Steps\n- click",
        acceptance_criteria="- works",
        links=["blocks BI-2 Other"],
        attachments=["log.txt (https://x/log)"],
        comments=[Comment("Ann", "2026-01-04T08:00:00Z", "## Note\nok"), Comment("Bob", "2026-01-05T09:30:00Z", "")],
    )
    assert t.to_markdown() == (
        "# BI-1: Broken export\n\n"
        "- **URL:** https://cloud.test/browse/BI-1\n"
        "- **Project:** BI\n- **Type:** Bug\n- **Status:** Open\n"
        "- **Assignee:** Me\n- **Labels:** a, b\n"  # empty Sprint is left out
        "- **Created:** 2026-01-02 03:04\n- **Updated:** 2026-01-03 10:00\n\n"
        "## Description\n\n### Steps\n- click\n\n"
        "## Acceptance criteria\n\n- works\n\n"
        "## Linked tickets\n\n- blocks BI-2 Other\n\n"
        "## Attachments\n\n- log.txt (https://x/log)\n\n"
        "## Comments (2)\n\n"
        "### Ann, 2026-01-04 08:00\n\n##### Note\nok\n\n"
        "### Bob, 2026-01-05 09:30\n\n_(empty)_\n"
    )


def test_ticket_markdown_minimal():
    md = Ticket("1", "T", "u", "Task", "New", "P").to_markdown()
    assert "## Description\n\n_(empty)_" in md
    assert md.endswith("## Comments (0)\n")
    for section in ("Acceptance criteria", "Linked tickets", "Attachments", "Created"):
        assert section not in md
