// GENERATED from the platform's OpenAPI snapshot by `scripts/generate_wire_ts.mjs`.
// Never hand-edited. Source: contract/openapi.json at
// sha256:43d82e5ec3d54b7c90deacb2d470cd41aaf3c9c9802aee63d3f311cf86dc38d2
//
// Every operation the SDK calls is declared in it.
//
// The type bodies below are `openapi-typescript`'s own output, at the version
// `package.json` exact-pins, run over the same bytes `python/hajer/_wire.py` is generated from.
// The aliases at the end resolve THROUGH `paths`, so a route the platform renames or a body field it drops is a compile error here rather
// than a 422 in somebody's request path.
//
// To regenerate: `just generate-wire` in typescript/.

/* eslint-disable */

export interface paths {
    "/api/teams/{team_id}/verify": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Apply a pinned verifier to one supplied tuple and answer inside the caller's deadline
         * @description The claim is exactly "this supplied payload satisfies these checks against this supplied evidence".
         */
        post: operations["verify_api_teams__team_id__verify_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/teams/{team_id}/observe": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Record a flush of tuples off the response path and queue their verification
         * @description All or nothing: a submission this route refuses takes the whole flush with it.
         */
        post: operations["observe_api_teams__team_id__observe_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/teams/{team_id}/observations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * The observations this team has recorded, oldest first — the tail
         * @description Compact rows: what arrived and when, never the request, the output or the evidence it carried.
         */
        get: operations["list_observations_api_teams__team_id__observations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/teams/{team_id}/observations/{observation_id}/assessment": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * The assessment for one recorded observation, or the receipt saying it has none yet
         * @description ``accepted`` carries no status: an absent answer is never served as an unavailable one.
         */
        get: operations["get_observation_assessment_api_teams__team_id__observations__observation_id__assessment_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/teams/{team_id}/projects/{project_id}/suite-runs/execution-inputs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Read privacy-approved traffic inputs for exact committed suite bytes and an admitted CI revision */
        post: operations["read_private_suite_inputs_api_teams__team_id__projects__project_id__suite_runs_execution_inputs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Liveness probe
         * @description Always 200 while the process is up, and it says which process and which database.
         */
        get: operations["health_api_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AssessmentOut
         * @description What the SDK hands the application. A projection of the record, losing nothing it can act on.
         *
         *     ``shadow`` is a fact about trust rather than about the answer: a shadow verifier's assessment is
         *     computed and recorded in full and the caller is told ``unavailable{SHADOW}``, because nothing here
         *     has been qualified on this team's own data yet.
         *
         *     ``seam`` is empty on a real answer. It is filled when the answer stands in for work this branch
         *     does not do, and it names what will do it — so a reader is never left to infer why an admitted
         *     tuple came back unavailable.
         */
        AssessmentOut: {
            /** Checkoutcomes */
            checkOutcomes: components["schemas"]["VerificationCheckOutcomeOut"][];
            /** Costmicrousd */
            costMicrousd: number;
            /** Findings */
            findings: components["schemas"]["VerificationFindingOut"][];
            /** Latencyms */
            latencyMs: number;
            /** Limitations */
            limitations: string[];
            /** Missingevidence */
            missingEvidence: components["schemas"]["VerificationMissingEvidenceOut"][];
            /** Observationid */
            observationId: string;
            /** Seam */
            seam: string | null;
            /** Shadow */
            shadow: boolean;
            status: components["schemas"]["VerificationStatus"];
            unavailableReason: components["schemas"]["UnavailableReason"] | null;
            /** Verifier */
            verifier: string;
        };
        /**
         * CallerFrameIn
         * @description One application frame above the SDK on a captured call: where the call was made from.
         *
         *     R12's OBSERVED rule joins a captured provider request to a hot spot by *the frame the SDK
         *     recorded*, so without this a stored prompt is a prompt with no address. Locations only — a
         *     module, a qualified name, a file and a line — and never a value the frame held.
         */
        CallerFrameIn: {
            /**
             * File
             * @default
             */
            file?: string;
            /** Filedigest */
            fileDigest?: string | null;
            /**
             * Line
             * @default 0
             */
            line?: number;
            /**
             * Module
             * @default
             */
            module?: string;
            /**
             * Qualname
             * @default
             */
            qualname?: string;
        };
        /** @enum {string} */
        CheckReason: "satisfied" | "violated" | "unsupported" | "invalid" | "not_applicable" | "missing_evidence";
        /** @enum {string} */
        ClaimedCaseKeySource: "CALLER" | "DERIVED_CLIENT";
        /**
         * ClientRedactionIn
         * @description What the SDK removed before sending, as the client reported it.
         *
         *     A **claim by the client**: the server never saw the values and cannot verify the counts. That is why
         *     ``catalog`` is required — a report saying "one CARD" could not be re-read after the rules changed, and
         *     with the catalog named a stored observation says which rule set was in force in the customer's own
         *     process.
         *
         *     ``degraded`` is the sentence the client wrote when its own pass could not complete: an unknown
         *     validator, a malformed rule, or a document past one of its budgets. Absent when the pass ran whole. It
         *     reaches the assessment's ``limitations``, because a caller who believes their payload was redacted and
         *     whose pass degraded has to be told.
         */
        ClientRedactionIn: {
            /** Catalog */
            catalog: string;
            /** Countsbyclass */
            countsByClass?: components["schemas"]["RedactionCountIn"][];
            /** Degraded */
            degraded?: string | null;
        };
        /**
         * ErrorCode
         * @description Machine-readable cause on the JSON error envelope (``ErrorOut.code``).
         * @enum {string}
         */
        ErrorCode: "BAD_REQUEST" | "AUTH_FAILED" | "UNAUTHORIZED" | "FORBIDDEN" | "NOT_FOUND" | "CONFLICT" | "PAYLOAD_TOO_LARGE" | "VALIDATION_ERROR" | "HTTP_ERROR" | "INTERNAL_ERROR" | "SERVICE_UNAVAILABLE" | "GITHUB_CONNECTION_REJECTED" | "SECRET_KEY_UNAVAILABLE" | "ONBOARDING_ATTESTATION_REQUIRED";
        /**
         * ErrorOut
         * @description The one JSON error envelope — built by ``app/core/exception_handlers.py`` and declared on
         *     every route's error responses through ``app.core.openapi.error_responses``.
         */
        ErrorOut: {
            code: components["schemas"]["ErrorCode"];
            /** Correlationid */
            correlationId: string;
            /** Details */
            details?: unknown | null;
            /** Error */
            error: string;
            /** Statuscode */
            statusCode: number;
        };
        /**
         * EvidenceStampIn
         * @description What the caller alone knows about one evidence field: when it was observed, and how whole.
         *
         *     A field whose contract sets ``maxAgeSeconds`` cannot be satisfied without ``observedAt`` — an
         *     unknown age is not freshness — and a field the SDK clipped cannot satisfy a contract that requires
         *     a complete value.
         */
        EvidenceStampIn: {
            /**
             * Completeness
             * @default COMPLETE
             * @enum {string}
             */
            completeness?: "COMPLETE" | "PARTIAL" | "MISSING" | "UNKNOWN";
            /** Observedat */
            observedAt?: string | null;
            /** Path */
            path: string;
        };
        /** HealthOut */
        HealthOut: {
            instance: components["schemas"]["InstanceOut"];
            /** Service */
            service: string;
            /** Status */
            status: string;
            /** Version */
            version: string;
        };
        /** @enum {string} */
        IngestMode: "VERIFY" | "OBSERVE";
        /**
         * InstanceOut
         * @description Which backend answered: the database it is pointed at, and this process.
         *
         *     The *name* of the database and nothing else from its URL — no user, no password, no host — because
         *     this probe is unauthenticated. It exists because an operator's script cannot otherwise tell a backend
         *     it just started from one that was already listening on the port (finding F-VF-1), and a run that
         *     reads its rows out of a different database than the one it thinks it is using has proved nothing.
         */
        InstanceOut: {
            /** Database */
            database: string;
            /** Process */
            process: string;
        };
        JsonValue: unknown;
        /** @enum {string} */
        MissingEvidenceReason: "ABSENT" | "REDACTED" | "TRUNCATED" | "STALE" | "INVALID";
        /**
         * ObservationRowOut
         * @description One recorded observation, as a line in a tail. Compact on purpose: what arrived, not what it said.
         *
         *     Everything here is a fact about the *record* — when it landed, which mode recorded it, which verifier
         *     it named (``null`` for an attach-mode observation), what the first model-call receipt says the provider
         *     and model were when the SDK sent bytes to read, how much redaction removed and what ingest's reading
         *     of the tuple was. Not the request, not the output, not the evidence: a list route is how somebody
         *     watches traffic arrive, and the tuple itself is customer data that no listing needs to carry.
         *
         *     ``provider`` and ``model`` are ``null`` when the wrapped calls arrived as summaries rather than
         *     captures, because then no receipt exists and the summary's word for it is not a reading (S3-2).
         *
         *     ``caseKey`` and ``caseKeySource`` are here because a tail is where somebody sees whether their
         *     attempts are landing in one case at all — the night's repeatability gate read ``NOT_APPLICABLE`` on
         *     every live run and nothing in the listing would have said why.
         */
        ObservationRowOut: {
            /** Assessed */
            assessed: boolean;
            /** Casekey */
            caseKey: string | null;
            /** Casekeysource */
            caseKeySource: string | null;
            /** Clientredactions */
            clientRedactions: number;
            /**
             * Createdat
             * Format: date-time
             */
            createdAt: string;
            /** Disposition */
            disposition: string;
            /** Id */
            id: string;
            mode: components["schemas"]["IngestMode"];
            /** Model */
            model: string | null;
            /** Origin */
            origin: string;
            /** Provider */
            provider: string | null;
            /**
             * Rawexpiresat
             * Format: date-time
             */
            rawExpiresAt: string;
            /** Redactedclasses */
            redactedClasses: string[];
            /** Redactions */
            redactions: number;
            /** Shadow */
            shadow: boolean;
            /** Verifier */
            verifier: string | null;
        };
        /**
         * ObserveAcceptedOut
         * @description The receipt for one flush: the id of every observation the server took, in the order sent.
         *
         *     ``accepted`` is the whole claim. Nothing has been verified — the observations are recorded and
         *     queued, and the assessment for each arrives at its own observation id.
         */
        ObserveAcceptedOut: {
            /** Observationids */
            observationIds: string[];
            /**
             * State
             * @constant
             */
            state: "accepted";
        };
        /**
         * ObserveIn
         * @description One ``observe`` flush: the observations the SDK's local queue batched, in the order it sent them.
         *
         *     All or nothing. The request commits through ``get_db``, so a submission this route refuses takes
         *     the whole flush with it and the SDK's queue retries the batch — which is the behaviour a caller can
         *     reason about, unlike a partial acceptance whose gaps only the server knows about.
         *
         *     ``environment`` tags the whole flush: every observation that names none of its own takes it, and one that
         *     names its own keeps it.
         */
        ObserveIn: {
            /** Environment */
            environment?: string | null;
            /** Observations */
            observations: components["schemas"]["VerifyIn"][];
        };
        /** @enum {string} */
        ObservedOrigin: "TARGET_SELF_REPORT" | "BROKER";
        /**
         * PolledAssessmentOut
         * @description The answer to ``GET /observations/{observation_id}/assessment``, whether or not there is one.
         *
         *     ``state`` is ``accepted`` while the tuple is recorded and no verifier has answered yet, and
         *     ``assessed`` once one has. In the first case ``status`` is absent, which is exactly how the SDK's
         *     ``receipt.poll()`` reads "not yet": it parses an assessment or it gets nothing, and an absent
         *     status is never reported to an application as an answer.
         *
         *     ``verifier`` is ``null`` for an observation that named none. Polling one answers ``accepted`` for as
         *     long as it exists — not because the answer is late, but because there is no verifier to give one, and
         *     the ``seam`` says so in words.
         *
         *     ``judgeReceipts`` is F-VF-5: the semantic checks of a queued run cost money, and until now the
         *     only place that money appeared was a JSONB column nobody outside the product could read. A team
         *     polling its own observation is now told what its verification spent and on which route.
         */
        PolledAssessmentOut: {
            /** Checkoutcomes */
            checkOutcomes?: components["schemas"]["VerificationCheckOutcomeOut"][];
            /**
             * Costmicrousd
             * @default 0
             */
            costMicrousd?: number;
            /** Findings */
            findings?: components["schemas"]["VerificationFindingOut"][];
            /** Judgereceipts */
            judgeReceipts?: components["schemas"]["VerificationJudgeReceiptOut"][];
            /**
             * Latencyms
             * @default 0
             */
            latencyMs?: number;
            /** Limitations */
            limitations?: string[];
            /** Missingevidence */
            missingEvidence?: components["schemas"]["VerificationMissingEvidenceOut"][];
            /** Observationid */
            observationId: string;
            /** Seam */
            seam?: string | null;
            /**
             * Shadow
             * @default false
             */
            shadow?: boolean;
            /**
             * State
             * @enum {string}
             */
            state: "accepted" | "assessed";
            status?: components["schemas"]["VerificationStatus"] | null;
            unavailableReason?: components["schemas"]["UnavailableReason"] | null;
            /** Verifier */
            verifier?: string | null;
        };
        /** PrivateSuiteInputsIn */
        PrivateSuiteInputsIn: {
            /** Revision */
            revision: string;
            /** Suitedigest */
            suiteDigest: string;
            /** Suiteid */
            suiteId: string;
            /** Suiteversion */
            suiteVersion: number;
        };
        /** PrivateSuiteInputsOut */
        PrivateSuiteInputsOut: {
            /** Executiondocument */
            executionDocument: string;
            /** Revision */
            revision: string;
            /** Unavailablecaseids */
            unavailableCaseIds: string[];
        };
        ProviderHost: string;
        /**
         * RedactionCountIn
         * @description How many values of one class the client removed. A count, never a value.
         *
         *     The key is ``category`` rather than ``class``. Two reasons, and they agree: ``class`` is a Python
         *     keyword, so no generated client model can carry a field named after it (the SDK's wire generator emits
         *     `class: str`, which does not parse); and ``category`` is the word the accepted DX row fixes for exactly
         *     this thing on the SDK side (`RedactionEntry.category`, never ``klass``). The service's own redaction
         *     receipt spells it ``class`` inside ``redaction.countsByClass`` because that shape predates this and is
         *     stored data; the client's report is new and spells it the way both sides can read.
         */
        RedactionCountIn: {
            /** Category */
            category: string;
            /** Count */
            count: number;
        };
        /**
         * SdkIdentityIn
         * @description Which client sent this tuple. Recorded, because a wire bug is a client version.
         */
        SdkIdentityIn: {
            /** Name */
            name: string;
            /** Version */
            version: string;
        };
        /** @enum {string} */
        UnavailableReason: "SHADOW" | "DEADLINE" | "VERIFIER_UNKNOWN" | "JUDGE_UNAVAILABLE" | "BUDGET_EXHAUSTED" | "INTERNAL";
        /**
         * VerificationCheckOutcomeOut
         * @description One check's own answer, kept underneath the aggregate status.
         */
        VerificationCheckOutcomeOut: {
            /** Check */
            check: string;
            /** Detail */
            detail: string;
            /** Evidenceused */
            evidenceUsed: string[];
            reason: components["schemas"]["CheckReason"];
        };
        /**
         * VerificationFindingOut
         * @description A substantiated statement, naming the obligation it is about and the evidence it rests on.
         */
        VerificationFindingOut: {
            /** Check */
            check: string;
            /** Evidenceused */
            evidenceUsed: string[];
            /** Obligation */
            obligation: string;
            /** Statement */
            statement: string;
        };
        /**
         * VerificationJudgeReceiptOut
         * @description One judge call this assessment paid for: the lane, the route and what the ledger recorded.
         *
         *     F-VF-5. It is a fact about the **request**, not a reading of the payload — which is why it is
         *     here at all and why it crosses the shadow mask beside ``costMicrousd`` and ``latencyMs``. A
         *     verifier that has not qualified on this team's traffic still spent this team's money, and a
         *     developer who cannot see that cannot reconcile a bill.
         *
         *     ``settledMicrousd`` is ``null`` when a settlement never landed. That is an unknown liability held
         *     at its reservation, never a zero, and ``costSource`` says which of the two the row is.
         *     ``disposition`` is the provider call's own status (``COMPLETED``, ``REFUSED``, ``FAILED``,
         *     ``UNKNOWN``); what the judge *said* stays in ``checkOutcomes``, behind the mask.
         */
        VerificationJudgeReceiptOut: {
            /** Callid */
            callId: string;
            /** Costsource */
            costSource: string;
            /** Disposition */
            disposition: string;
            /** Lane */
            lane: string;
            /** Latencyms */
            latencyMs: number;
            /** Modelid */
            modelId: string;
            /** Reservationmicrousd */
            reservationMicrousd?: number | null;
            /** Routeid */
            routeId: string;
            /** Settledmicrousd */
            settledMicrousd?: number | null;
        };
        /**
         * VerificationMissingEvidenceOut
         * @description One field the assessment needed and did not get, and which of the five reasons applies.
         */
        VerificationMissingEvidenceOut: {
            /** Detail */
            detail: string;
            /** Field */
            field: string;
            reason: components["schemas"]["MissingEvidenceReason"];
        };
        /** @enum {string} */
        VerificationStatus: "satisfied" | "violated" | "insufficient_evidence" | "unavailable";
        /**
         * VerifyIn
         * @description One ``verify`` or ``observe`` submission — the wire form of ``VerifyEnvelope``.
         *
         *     ``deadlineMs`` is what is **left** of the caller's budget when the request arrived, not what they
         *     started with: the SDK owns the whole operation's clock and sends the remainder (§9 finding E). An
         *     ``OBSERVE`` submission carries none, because it is off the response path.
         *
         *     ``verifier`` is optional and ``null`` means exactly one thing: **no verifier**, so the tuple is
         *     recorded and nothing is verified. That is what the SDK's attach mode sends for a provider call the
         *     application never declared an obligation about (``hajer.attach()``). A ``VERIFY`` with no verifier is
         *     refused here rather than in the engine, because a caller asking for an answer about nothing is a
         *     request error and not an ingest decision.
         */
        VerifyIn: {
            /** Casekey */
            caseKey?: string | null;
            caseKeySource?: components["schemas"]["ClaimedCaseKeySource"] | null;
            clientRedaction?: components["schemas"]["ClientRedactionIn"] | null;
            /**
             * Contentcaptured
             * @default false
             */
            contentCaptured?: boolean;
            /** Deadlinems */
            deadlineMs?: number | null;
            /** Environment */
            environment?: string | null;
            /** Evidence */
            evidence?: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Evidencestamps */
            evidenceStamps?: components["schemas"]["EvidenceStampIn"][];
            /** Idempotencykey */
            idempotencyKey: string;
            mode: components["schemas"]["IngestMode"];
            origin?: components["schemas"]["ObservedOrigin"] | null;
            output: components["schemas"]["JsonValue"];
            /** Request */
            request: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            sdk: components["schemas"]["SdkIdentityIn"];
            /** Verifier */
            verifier?: string | null;
            /** Wrappedcalls */
            wrappedCalls?: (components["schemas"]["WrappedCallCaptureIn"] | components["schemas"]["WrappedCallSummaryIn"])[];
            /**
             * Wrappedcallsdropped
             * @default 0
             */
            wrappedCallsDropped?: number;
        };
        /** @enum {string} */
        Wire: "OPENAI_CHAT_COMPLETIONS" | "OPENAI_RESPONSES" | "ANTHROPIC_MESSAGES";
        WorkflowHint: string;
        /**
         * WrappedCallCaptureIn
         * @description One provider request/response pair, as bytes. This is the shape that becomes a receipt.
         */
        WrappedCallCaptureIn: {
            /** Callerframes */
            callerFrames?: components["schemas"]["CallerFrameIn"][];
            /** Durationms */
            durationMs: number;
            /** Replyreads */
            replyReads?: string[] | null;
            /** Request */
            request: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Responsebody */
            responseBody: string;
            /** Responsestatus */
            responseStatus: number;
            /**
             * Startedat
             * Format: date-time
             */
            startedAt: string;
            /**
             * Streamed
             * @default false
             */
            streamed?: boolean;
            /**
             * Truncated
             * @default false
             */
            truncated?: boolean;
            wire: components["schemas"]["Wire"];
            workflowHint?: components["schemas"]["WorkflowHint"] | null;
        };
        /**
         * WrappedCallSummaryIn
         * @description What ``hajer.wrap(client)`` records without the bytes: the call, described.
         *
         *     Admitted and persisted; it produces no ``ModelCallReceipt``. Every field is what the SDK's own
         *     ``WrappedCall.to_wire()`` emits, so a client that captures no content still has somewhere to put
         *     what it did observe.
         */
        WrappedCallSummaryIn: {
            /** Api */
            api: string;
            /** Callerframes */
            callerFrames?: components["schemas"]["CallerFrameIn"][];
            /** Content */
            content?: {
                [key: string]: components["schemas"]["JsonValue"];
            } | null;
            /** Declaredtools */
            declaredTools?: string[];
            /** Durationms */
            durationMs: number;
            /** Error */
            error?: string | null;
            /** Errortype */
            errorType?: string | null;
            /** Finishreason */
            finishReason?: string | null;
            /** Limitations */
            limitations?: string[];
            /**
             * Messagecount
             * @default 0
             */
            messageCount?: number;
            /** Messageroles */
            messageRoles?: string[];
            /** Model */
            model?: string | null;
            /** Provider */
            provider: string;
            providerHost?: components["schemas"]["ProviderHost"] | null;
            /** Requestsettings */
            requestSettings?: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Responseid */
            responseId?: string | null;
            /**
             * Retries
             * @default 0
             */
            retries?: number;
            /**
             * Startedat
             * Format: date-time
             */
            startedAt: string;
            /**
             * Streamchunks
             * @default 0
             */
            streamChunks?: number;
            /** Streamcomplete */
            streamComplete?: boolean | null;
            /**
             * Streamed
             * @default false
             */
            streamed?: boolean;
            /** Toolcalls */
            toolCalls?: {
                [key: string]: components["schemas"]["JsonValue"];
            }[];
            /** Toolresults */
            toolResults?: {
                [key: string]: components["schemas"]["JsonValue"];
            }[];
            /** Usage */
            usage?: {
                [key: string]: number;
            };
            workflowHint?: components["schemas"]["WorkflowHint"] | null;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    verify_api_teams__team_id__verify_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                team_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["VerifyIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssessmentOut"];
                };
            };
            /** @description Recorded and queued: the remaining deadline left no room to answer inline */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AssessmentOut"];
                };
            };
            /** @description Missing, invalid or expired bearer token */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Not found, or not visible to the caller */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conflicts with existing state */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Payload larger than a declared ingest bound */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Request validation failed */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Internal server error */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description A dependency (database, identity provider) is unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    observe_api_teams__team_id__observe_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                team_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ObserveIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ObserveAcceptedOut"];
                };
            };
            /** @description Missing, invalid or expired bearer token */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Not found, or not visible to the caller */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conflicts with existing state */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Payload larger than a declared ingest bound */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Request validation failed */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Internal server error */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description A dependency (database, identity provider) is unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_observations_api_teams__team_id__observations_get: {
        parameters: {
            query?: {
                /** @description Inclusive: rows recorded at or after this instant (one flush shares an instant) */
                since?: string | null;
                /** @description Clipped to INGEST_OBSERVATIONS_PAGE_MAX */
                limit?: number | null;
            };
            header?: never;
            path: {
                team_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ObservationRowOut"][];
                };
            };
            /** @description Missing, invalid or expired bearer token */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Not found, or not visible to the caller */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conflicts with existing state */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Payload larger than a declared ingest bound */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Request validation failed */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Internal server error */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description A dependency (database, identity provider) is unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_observation_assessment_api_teams__team_id__observations__observation_id__assessment_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                observation_id: string;
                team_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PolledAssessmentOut"];
                };
            };
            /** @description Missing, invalid or expired bearer token */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Not found, or not visible to the caller */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conflicts with existing state */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Payload larger than a declared ingest bound */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Request validation failed */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Internal server error */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description A dependency (database, identity provider) is unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    read_private_suite_inputs_api_teams__team_id__projects__project_id__suite_runs_execution_inputs_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                project_id: string;
                team_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PrivateSuiteInputsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PrivateSuiteInputsOut"];
                };
            };
            /** @description Missing, invalid or expired bearer token */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Not found, or not visible to the caller */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conflicts with existing state */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Request validation failed */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Internal server error */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description A dependency (database, identity provider) is unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    health_api_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthOut"];
                };
            };
        };
    };
}

/** POST /api/teams/{team_id}/verify — success 200 */
export type VerifyOperation = paths["/api/teams/{team_id}/verify"]["post"];
export type VerifyIn = VerifyOperation["requestBody"] extends { content: { "application/json": infer B } } ? B : never;
export type VerifyOut = VerifyOperation["responses"][200] extends { content: { "application/json": infer R } } ? R : never;

/** POST /api/teams/{team_id}/observe — success 202 */
export type ObserveOperation = paths["/api/teams/{team_id}/observe"]["post"];
export type ObserveIn = ObserveOperation["requestBody"] extends { content: { "application/json": infer B } } ? B : never;
export type ObserveOut = ObserveOperation["responses"][202] extends { content: { "application/json": infer R } } ? R : never;

/** GET /api/teams/{team_id}/observations — success 200 */
export type ObservationsOperation = paths["/api/teams/{team_id}/observations"]["get"];
export type ObservationsIn = never;
export type ObservationsOut = ObservationsOperation["responses"][200] extends { content: { "application/json": infer R } } ? R : never;

/** GET /api/teams/{team_id}/observations/{observation_id}/assessment — success 200 */
export type AssessmentPollOperation = paths["/api/teams/{team_id}/observations/{observation_id}/assessment"]["get"];
export type AssessmentPollIn = never;
export type AssessmentPollOut = AssessmentPollOperation["responses"][200] extends { content: { "application/json": infer R } } ? R : never;
