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
    missing = []
    for rel, ids in AI_BUTTONS.items():
        lines = (SRC / rel).read_text(encoding="utf-8").splitlines()
        for tid in ids:
            hits = [l for l in lines if re.search(r"data-testid=[{\"`]+" + re.escape(tid) + r"[`\"}]", l)]
            if not hits or not all("aiOff" in l for l in hits):
                missing.append(f"{rel}:{tid}")
    assert not missing, missing
