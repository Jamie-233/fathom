from fathom.ai.knowledge import KnowledgeBase, tokenize


def test_tokenizer_keeps_ops_tokens_and_handles_cjk():
    toks = tokenize("%LINK-3-UPDOWN on Gi1/0/1 端口故障")
    assert "%link-3-updown" in toks and "gi1/0/1" in toks
    assert {"端口", "口故", "故障"} <= set(toks)


def test_search_ranks_the_right_runbook_section():
    kb = KnowledgeBase()
    top = kb.search("port err-disabled bpduguard", limit=1)[0].section
    assert top.doc == "bpdu-guard"
    assert kb.search("CRC errors cable", limit=1)[0].section.doc == "crc-errors"
    assert kb.search("uptime reset rebooted", limit=1)[0].section.doc == "device-unreachable"


def test_sections_are_split_by_heading():
    kb = KnowledgeBase()
    heads = {(s.doc, s.heading) for s in kb.sections}
    assert ("link-down", "Resolution") in heads and ("link-down", "Diagnosis") in heads


def test_no_match_returns_nothing():
    assert KnowledgeBase().search("zzzz qqqq") == []
