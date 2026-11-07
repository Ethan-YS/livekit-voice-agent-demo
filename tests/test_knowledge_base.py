from voice_agent.config import PROJECT_ROOT
from voice_agent.knowledge_base import KnowledgeBase

kb = KnowledgeBase.load(PROJECT_ROOT / "knowledge_base")


def test_loads_sections():
    assert len(kb.sections) >= 10


def test_refund_question_finds_refund_policy():
    top = kb.search("Can I get a refund on an annual plan?")[0]
    assert "Refunds" in top.section.title
    assert "60 days" in top.section.text


def test_pricing_question_finds_emergency_rates():
    top = kb.search("how much is an emergency callout at night")[0]
    assert "Emergency callout" in top.section.title


def test_unknown_topic_returns_nothing():
    assert kb.search("quantum blockchain") == []
