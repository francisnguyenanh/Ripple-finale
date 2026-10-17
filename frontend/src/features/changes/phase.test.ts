import { describe, expect, it } from "vitest";
import { phase } from "./types";
import type { Job } from "./types";

const job = (o: Partial<Job>): Job => ({ job_id: "j", kind: "change.draft", status: "APPROVED", execution_status: "QUEUED", ...o }) as Job;

describe("phase()", () => {
  it("says Running for an allowed job that is already running (live B6: it read 'waiting to run' while the model was being called)", () => {
    expect(phase(job({ execution_status: "RUNNING" }))).toBe("RUNNING");
  });
  it("keeps 'allowed, waiting to run' only while the job has not started, and the terminal state once it ended", () => {
    expect(phase(job({}))).toBe("APPROVED");
    expect(phase(job({ execution_status: "FAILED" }))).toBe("FAILED");
    expect(phase(job({ status: "PENDING_APPROVAL" }))).toBe("PENDING_APPROVAL");
  });
});
