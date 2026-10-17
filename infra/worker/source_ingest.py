"""The worker executor for the job an import commit creates.

Zero provider calls. It re-reads every admitted artifact of the pinned
revision through `PostgresV2Store.read_blob` (which re-verifies the content
digest), runs the byte-pure format adapter for that artifact
(`core/change_intelligence/v2/source_ingest_adapters.py`; no macro/formula/
Python execution), persists the resulting observations, and reports one
honest coverage manifest bound to the revision and manifest hash. An item
the import admission already skipped is recorded `not_parsed` -- never
handed to a parser, and never counted toward "complete".
"""

from __future__ import annotations

from core.change_intelligence.v2.source_ingest_adapters import parser_for
from infra.adapters.v2_postgres import BlobIntegrityError, TenantScopeError
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError,
    ProviderCallBudget,
)

_NOT_PARSED = {
    "parse_status": "not_parsed", "parser": None, "parser_version": None,
    "encoding": None, "newline": None, "diagnostics": [],
    "observation_count": 0, "chunk_count": 0,
}


class SourceIngestExecutor:
    external = False
    executor_id = "deterministic.source_ingest.admission.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
                calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        revision_id = job["pinned_source_revision_id"]
        if not revision_id:
            raise NonRetryableExecutionError("pinned_source_revision_missing")
        try:
            view = ctx.store.source_revision_view(job["tenant_id"], job["workspace_id"],
                                                  revision_id)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("pinned_source_revision_missing",
                                             revision_id) from exc
        # The pinned revision came from an import OR a manual refresh; the job
        # payload must name the very boundary object that produced it.
        by_import = (view["import_id"] is not None
                     and view["import_id"] == job["payload"].get("import_id"))
        by_refresh = (view.get("refresh") is not None
                      and view["refresh"]["refresh_id"] == job["payload"].get("refresh_id"))
        if not (by_import or by_refresh):
            raise NonRetryableExecutionError("import_revision_mismatch", revision_id)

        artifacts = []
        manifest_artifacts = []
        for art in view["artifacts"]:
            try:
                content = ctx.store.read_blob(job["tenant_id"], job["workspace_id"], revision_id,
                                              art["artifact_id"])
            except (BlobIntegrityError, KeyError, FileNotFoundError) as exc:
                raise NonRetryableExecutionError(
                    "source_blob_integrity_failed", art["logical_path"]) from exc

            parser = parser_for(art["logical_path"])
            if parser is None:
                # Every ADMITTED item is, by construction, a format the
                # admission layer recognizes (source_admission.py); a
                # missing parser here would be a real coverage gap, so it is
                # reported honestly rather than silently skipped.
                parse_result_entry = {**_NOT_PARSED,
                                      "diagnostics": [{"code": "no_parser_registered",
                                                       "message": art["logical_path"],
                                                       "locator": {}}]}
                observations = []
            else:
                try:
                    parsed = parser(art["logical_path"], content)
                    parse_result_entry = parsed.manifest_entry(art["artifact_id"], art["logical_path"])
                    observations = parsed.observations
                    if observations:
                        ctx.store.record_source_observations(
                            job["tenant_id"], job["workspace_id"], revision_id,
                            art["artifact_id"], observations)
                except Exception as exc:  # noqa: BLE001 - one bad
                    # artifact must end the job HONESTLY (a terminal,
                    # coverage_complete=false outcome), never leave it stuck
                    # RUNNING behind an unhandled exception. Every adapter
                    # above is expected to convert an unrepresentable value
                    # into its own `failed`/`partial` ParseResult already;
                    # this is the last-resort backstop for anything that
                    # still escapes that (a bug in an adapter, a transient
                    # persistence error), so the other artifacts in the same
                    # revision still get their real outcome recorded.
                    parse_result_entry = {**_NOT_PARSED, "parse_status": "failed",
                                          "diagnostics": [{"code": "adapter_error",
                                                           "message": f"{type(exc).__name__}: {exc}",
                                                           "locator": {}}]}
                    observations = []

            entry = {"artifact_id": art["artifact_id"], "logical_path": art["logical_path"],
                     "media_type": art["media_type"], "blob_hash": art["blob_hash"],
                     "blob_verified": True, **parse_result_entry}
            artifacts.append(entry)
            manifest_artifacts.append(entry)

        for skipped in view["coverage"]["skipped"]:
            manifest_artifacts.append({"artifact_id": None, "logical_path": skipped["logical_path"],
                                       **_NOT_PARSED,
                                       "diagnostics": [{"code": "admission_skip",
                                                        "message": skipped["reason"],
                                                        "locator": {}}]})

        coverage_complete = all(a["parse_status"] == "parsed" for a in manifest_artifacts)
        coverage_reason = ("all admitted artifacts parsed with no admission skips"
                           if coverage_complete else
                           "at least one artifact is unparsed, partial, failed, or skipped")

        # Build the retrieval projection from the observations just
        # persisted above. This extends the one executor rather than adding
        # a second job kind chained off the import commit path. A build failure must not turn an otherwise-successful parse
        # into a stuck/failed job -- it is reported honestly alongside
        # coverage, not raised.
        #
        # The SAME `coverage_complete`/`coverage_reason` this job
        # just computed for its OWN parse coverage is threaded into the
        # index build, so a query against a READY index can still tell a
        # reader "this revision's source was not fully parsed" -- retrieval
        # readiness (did the projection finish building) and SOURCE coverage
        # (did every admitted artifact actually parse) are different claims
        # and must not be conflated.
        try:
            retrieval_index = ctx.store.build_retrieval_index(
                job["tenant_id"], job["workspace_id"], revision_id,
                source_coverage_complete=coverage_complete,
                source_coverage_reason=coverage_reason)
        except Exception as exc:  # noqa: BLE001 - same backstop as above
            retrieval_index = {"source_revision_id": revision_id, "status": "FAILED",
                               "reason": f"{type(exc).__name__}: {exc}"}

        return ExecutionOutcome(result={
            "retrieval_index": retrieval_index,
            "executor": self.executor_id,
            "source_revision_id": revision_id,
            "manifest_hash": view["manifest_hash"],
            "import_id": view["import_id"],
            "artifacts": artifacts,
            "skipped": view["coverage"]["skipped"],
            "coverage_complete": coverage_complete,
            "coverage_reason": coverage_reason,
            "coverage_manifest": {
                "source_revision_id": revision_id, "manifest_hash": view["manifest_hash"],
                "complete": coverage_complete, "artifacts": manifest_artifacts,
            },
        })
