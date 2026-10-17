import { describe, expect, it } from "vitest";
import { failureAdvice, plain } from "./reasons";

describe("plain()", () => {
  it("rewrites known reason codes into sentences in Business view", () => {
    expect(plain("quote_not_in_stored_observation", false)).toBe("the quoted text was not found in the stored file, so it was not used");
    expect(plain("applies_to:emergency: no classified evidence item addresses it", false)).toBe('the case "emergency": no classified evidence item addresses it');
    expect(plain("Simulation limit: empty_corpus (mode EXECUTED)", false)).toContain("no sample data was available");
  });
  it("never leaves a snake_case or UPPER_CASE code in Business view", () => {
    const t = plain("a_new_unknown_code and SOME_OTHER_CODE", false);
    expect(t).not.toMatch(/_/);
  });
  it("hides raw ids and fingerprints in Business view", () => {
    const t = plain("scoped to target artifact id='art-6db7b667b8ecdd5526ee0490c1e57872'; contract id=vc-a97e541c81b547c8b8f470039849acb4; sha256=aca1e679584019fdd9dcf09ecf54256bcc4914a0c1da7174ca8271f5a14cb3e6", false);
    expect(t).not.toMatch(/art-|vc-|[0-9a-f]{32}/);
    expect(t).toContain("a file");
  });
  it("tells a policy deny, a switched-off server and a used-up allowance apart", () => {
    expect(plain("model_data_policy_denied", false)).toContain("project policy does not allow");
    expect(plain("provider_disarmed", false)).toContain("switched off");
    expect(plain("allocation_exhausted", false)).toContain("used up");
    expect(plain("model_data_policy_denied", true)).toBe("model_data_policy_denied");
  });
  it("shows the server text unchanged in Technical view", () => {
    expect(plain("applies_to: say which kinds of case", true)).toBe("applies_to: say which kinds of case");
  });
});

describe("reanalysis scope reasons are sentences, not Python lists", () => {
  it("counts the files and says what happened", () => {
    const raw = "dependency_coverage_missing: ['art-aaaaaaaa', 'art-bbbbbbbb', 'art-cccccccc']";
    expect(plain(raw, false)).toBe("3 unchanged files had no relation coverage, so every file was read again");
    expect(plain(raw, true)).toBe(raw);
  });
  it("explains the targeted case", () => {
    expect(plain("dependency_coverage_available_for_all_unchanged_artifacts", false)).toContain("only the changed files were read again");
  });
});

describe("timeline notes", () => {
  it("does not show job ids or the word lineage", () => {
    const t = plain("job job-extraction-5d6a6b1c0b: 2 candidates share the lineage key; not merged", false);
    expect(t).toBe("2 rules read from this file look like the same rule, so they were kept as separate entries and not merged");
  });
});

describe("failureAdvice", () => {
  it("says a cut-off answer was not used and that retrying unchanged will not help", () => {
    const a = failureAdvice("provider_output_truncated: provider response did not finish normally (finish_reason=MAX_TOKENS): output plus thinking reached the request cap of 6144 tokens");
    expect(a.what).toMatch(/cut off/);
    expect(a.next).toMatch(/administrator/);
    expect(a.retryHelps).toBe(false);
  });
  it("names a withheld answer and keeps a transient failure retryable", () => {
    expect(failureAdvice("provider_response_blocked: x (finish_reason=SAFETY)").what).toMatch(/withheld.*safety/);
    expect(failureAdvice("provider_execution_failed: ProviderInvalidResponseError: x").retryHelps).toBe(true);
  });
  it("never shows a bare machine code for an unknown reason", () => {
    expect(failureAdvice("some_new_reason_code").what).not.toMatch(/_/);
  });
});

describe("failureAdvice: a question over too much text", () => {
  it("says there is too much text and what to do, not that the allowance is used up", () => {
    const a = failureAdvice("budget_exceeded: source prompt exceeds input ceiling");
    expect(a.what).toMatch(/too much text/);
    expect(a.next).toMatch(/fewer files/);
    expect(a.what).not.toMatch(/allowance/);
  });
});
