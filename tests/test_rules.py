"""Free-form user rules, email headers and model switching."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import make_engine, packet
from sorto.cli import main
from sorto.config import config_for_model, load_config, packaged_prompt
from sorto.identify import identify_file, looks_like_email
from sorto.llm import packet_user_message, parse_classification
from sorto.models import AnalysisView
from sorto.rules import RULES_TEMPLATE, ensure_rules_file, load_rules
from sorto.tui import render_view


def _rules_path() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "sorto" / "rules.md"


def test_template_alone_adds_no_rules() -> None:
    path = ensure_rules_file()
    assert path == _rules_path()
    assert path.read_text(encoding="utf-8") == RULES_TEMPLATE
    assert not load_rules()


def test_rules_reach_the_system_prompt(cfg) -> None:
    path = ensure_rules_file()
    path.write_text(
        RULES_TEMPLATE + "\n- Emails from billing@example.com go to 13.13.\n- Scans of X-rays go to 99.99.\n",
        encoding="utf-8",
    )
    rules = load_rules()
    assert rules.line_count == 2
    assert rules.ids == ["13.13", "99.99"]
    eng = make_engine(cfg)
    prompt = eng.system_prompt
    assert "USER RULES" in prompt and "billing@example.com" in prompt
    assert "Copy them below the comment" not in prompt  # the template comment stays out
    assert prompt.index("TARGET JOHNNY.DECIMAL OUTLINE") < prompt.index("billing@example.com")
    eng.start()
    eng.request_stop()
    eng.join(timeout=10)
    assert any("99.99" in line for line in eng.snapshot().log_lines)


def test_rules_reminder_and_rule_field() -> None:
    msg = packet_user_message(packet("a.txt"), rules=True)
    assert "USER RULES" in msg
    assert "USER RULES" not in packet_user_message(packet("a.txt"))
    cls = parse_classification('{"jd_id": "13.13", "summary": "s", "rule": "billing@example.com → 13.13"}')
    assert cls.rule == "billing@example.com → 13.13"


def test_rules_file_from_cli_flag(inbox: Path, target: Path, tmp_path: Path) -> None:
    custom = tmp_path / "my-rules.md"
    custom.write_text("- Everything about cats goes to 51.11.\n", encoding="utf-8")
    import argparse

    cfg = load_config(inbox, target, cli=argparse.Namespace(rules=custom))
    assert cfg.rules_file == custom
    assert load_rules(cfg.rules_file).text.startswith("- Everything about cats")


def test_rules_command(target: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules"]) == 0
    assert "no rules yet" in capsys.readouterr().out
    _rules_path().write_text("- Bills go to 13.13\n- Old stuff goes to 42.42\n", encoding="utf-8")
    assert main(["rules", str(target)]) == 1
    out = capsys.readouterr().out
    assert "2 rule line(s)" in out and "42.42" in out


EML = (
    "Return-Path: <billing@example.com>\n"
    "Received: from mx.example.com by mail.example.org\n"
    "From: Example Billing <billing@example.com>\n"
    "To: alex@example.org\n"
    "Subject: =?UTF-8?B?SW52b2ljZSBmb3IgTWFyY2g=?=\n"
    "Date: Mon, 03 Mar 2025 10:00:00 +0000\n"
    "Content-Type: text/plain; charset=utf-8\n"
    "\n"
    "Your invoice for March is 42.00 EUR.\n"
)


def test_email_headers_reach_the_packet(inbox: Path, cfg) -> None:
    maildir = inbox / "1500000000.M100000P1000Q1R0a0b0c.host"
    maildir.write_text(EML, encoding="utf-8")
    assert looks_like_email(maildir, None)
    pkt = identify_file(maildir, maildir.name, cfg)
    assert "From: Example Billing <billing@example.com>" in pkt.extra_meta["email"]
    assert "Subject: Invoice for March" in pkt.extra_meta["email"]
    assert "42.00 EUR" in pkt.extra_meta["document_text"]
    assert pkt.text_preview == ""
    note = inbox / "notes.md"
    note.write_text("# Title\nFrom: here to there\n", encoding="utf-8")
    assert not looks_like_email(note, "text/markdown")


def test_render_view_shows_rule_and_location() -> None:
    view = AnalysisView(
        src_rel="x.txt", summary="A bill.", jd_id="13.13", jd_name="Invoices", dest_rel="a/x.txt",
        rule="billing@example.com → 13.13", current_location="51.11 Photos", outcome="in_place",
        stage="done",
    )
    text = render_view(view, "/t", 80, reorganize=True)
    assert "Rule:" in text and "billing@example.com" in text
    assert "Was in:   51.11 Photos" in text
    assert "ALREADY IN THE RIGHT PLACE" in text
    view.outcome = "kept"
    assert "LEFT WHERE IT IS" in render_view(view, "/t", 80, reorganize=True)
    assert "KEPT IN SOURCE" in render_view(view, "/t", 80)


def test_model_switch_uses_that_models_grok_profile(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    grok = Path(os.environ["GROK_HOME"])
    grok.mkdir(parents=True, exist_ok=True)
    (grok / "config.toml").write_text(
        '[model."big"]\nmodel = "big:35b"\nbase_url = "http://127.0.0.1:11435/v1"\ncontext_window = 65536\n'
        '[model."small"]\nmodel = "small:9b"\nbase_url = "http://localhost:11500/v1"\ncontext_window = 16384\n',
        encoding="utf-8",
    )
    small = config_for_model(cfg, "small:9b")
    assert (small.llm_url, small.context_window) == ("http://localhost:11500/v1", 16384)
    assert "small" in small.llm_profile_source
    # A model without a profile of its own stays on the server already in use.
    big = config_for_model(cfg, "big:35b")
    assert config_for_model(big, "local-tag:1b").llm_url == "http://127.0.0.1:11435/v1"


def test_rules_section_lets_a_rule_ask_for_an_id_of_its_own(tmp_path: Path) -> None:
    from sorto.llm import RULES_REMINDER
    from sorto.rules import RULES_TEMPLATE, rules_prompt_section

    path = tmp_path / "rules.md"
    path.write_text("- Everything about Example Club goes under an ID of its own.\n", encoding="utf-8")
    section = rules_prompt_section(load_rules(path))
    assert 'answer jd_id "new" with new_id_name' in section and "temporary home" in section
    assert "only choose IDs from the outline" not in section  # that sentence made models park such files
    assert 'answer jd_id "new"' in RULES_REMINDER
    assert "ID of its own" in RULES_TEMPLATE
    assert "no one creates the ID later" in packaged_prompt()
