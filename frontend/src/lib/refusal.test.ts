import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { REFUSAL_WORDS, UNKNOWN_REFUSAL, refusalOf, refusalWords } from "./refusal";

describe("refusal wording", () => {
  it("words every stable code and keeps the code", () => {
    for (const code of Object.keys(REFUSAL_WORDS)) expect(refusalOf(new ApiError(409, { code, error: "server text" })).words).toBe(REFUSAL_WORDS[code]);
  });
  it("never turns an unknown code or the server's text into an explanation", () => {
    const r = refusalOf(new ApiError(409, { code: "made_up_code", error: "internal path C:/secret" }));
    expect(r.words).toBe(UNKNOWN_REFUSAL);
    expect(r.code).toBe("made_up_code");
    expect(r.words).not.toContain("secret");
    expect(refusalWords(undefined)).toBe(UNKNOWN_REFUSAL);
  });
  it("a 403 without a code is a missing permission; a 404 is 'not available' without saying whether it exists", () => {
    expect(refusalOf(new ApiError(403, {})).code).toBe("permission_missing");
    expect(refusalOf(new ApiError(404, {})).words).toBe("This is not available to your account.");
  });
  it("keeps the request id when the server sent one", () => {
    expect(refusalOf(new ApiError(409, { code: "not_approved" }, false, null, "0123456789abcdef0123456789abcdef")).requestId).toBe("0123456789abcdef0123456789abcdef");
  });
});
