import { describe, expect, it } from "vitest";
import { ApiError, errorText } from "./api";

describe("limit and refusal wording", () => {
  it("429 names the wait and says nothing was lost", () => {
    const t = errorText(new ApiError(429, { error: "too many requests" }, false, 7));
    expect(t).toContain("7 seconds");
    expect(t).toContain("Nothing was lost");
  });
  it("503 from the safety counter or a busy pool says to retry, not 'server error'", () => {
    const t = errorText(new ApiError(503, { error: "rate limiter unavailable" }, false, 15));
    expect(t).toContain("15 seconds");
    expect(t).not.toContain("HTTP 503");
  });
  it("budget, concurrency and operator switch use the server's own sentence and next action", () => {
    for (const code of ["budget_exceeded", "concurrency_limit", "ai_switched_off", "person_quota", "role_not_enabled", "ai_roles_not_configured", "ai_config_unreadable"]) {
      const t = errorText(new ApiError(409, { code, error: "E.", next_action: "N." }));
      expect(t).toBe("E. N.");
    }
  });
  it("a plain 500 is still reported as a server error with nothing changed", () => {
    expect(errorText(new ApiError(500, {}))).toContain("HTTP 500");
  });
});
