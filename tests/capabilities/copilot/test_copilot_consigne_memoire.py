"""La consigne de l'agent intégré dit où vit ce qui dure : sans elle, il répondait
qu'il n'existe aucun équivalent à la mémoire de Claude (Corma, 2 nov. 2026)."""
from oto_mcp.capabilities.copilot import consigne


def test_la_consigne_dit_ou_vit_ce_qui_dure():
    texte = consigne.consigne()
    assert "About you" in texte
    assert "`oto_guide scope=user delivery=init`" in texte
    assert "`oto_guide scope=user`" in texte
    assert "Never tell someone" in texte


def test_remember_va_dans_about_you():
    assert 'When they say "remember …", add it to their About you.' in consigne.consigne()
