"""Every button that starts or allows a paid AI call must be disabled when the server is DISARMED (UI promise in the banner).
Static check of the UI source; the server-side 409 gate is tested in test_provider_gate."""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "features"
AI_BUTTONS = {
    "changes/IntentStage.tsx": ["action-parse-intent"],
    "changes/DraftStage.tsx": ["action-run-draft", "action-revise-draft", "gap-suggest"],
    "changes/ApplyStage.tsx": ["action-run-query"],
    "changes/ChatPanel.tsx": ["chat-allow", "chat-allow-other"],
    "freetext/PurposeChat.tsx": ["${tp}-allow", "${tp}-allow-other"],
    "explain/ExplanationCard.tsx": ["ai-explanation-request"],
    "explain/SimulationAnalysisCard.tsx": ["sim-analysis-request", "sim-analysis-retry"],
    "sources/Lifecycle.tsx": ["action-analyze", "action-reanalyze", "action-reanalyze-confirm"],
}


def test_ai_buttons_are_disabled_while_disarmed():
    # `s.aiKind(kind)` is false while the server is disarmed as well as for a kind the allowlist does not enable (session.tsx), so a button gated by it
    # follows the provider switch; checked here so the static rule cannot pass on a gate that forgot the switch (2026-10-27: the rule still looked for the
    # literal `aiOff` after the allowlist gate replaced it, and failed on correct code)
    session = (SRC.parent / "session.tsx").read_text(encoding="utf-8")
    assert re.search(r"aiKind: \(k\) => !aiOff && kindEnabled\(", session), "aiKind must include the provider switch"
    missing = []
    for rel, ids in AI_BUTTONS.items():
        lines = (SRC / rel).read_text(encoding="utf-8").splitlines()
        for tid in ids:
            hits = [l for l in lines if re.search(r"data-testid=[{\"`]+" + re.escape(tid) + r"[`\"}]", l)]
            if not hits or not all("aiOff" in l or re.search(r"disabled=\{[^}]*!s\.aiKind\(", l) for l in hits):
                missing.append(f"{rel}:{tid}")
    assert not missing, missing
