"""The attribute keys a span carries, spelled once.

The GenAI keys are the OpenTelemetry semantic conventions' own (`gen_ai.*`, as of semconv 1.37); the general ones
(`server.*`, `code.*`, `error.type`, `http.*`, `url.*`, `session.id`, `user.id`, `deployment.environment.name`) are
the stable conventions. They are written here by hand rather than imported from `opentelemetry-semantic-conventions`
because the GenAI names live under that package's incubating namespace, which has moved between releases, and a
module of string constants is reviewable and costs no dependency. `hajer.*` is this SDK's own namespace: what the
platform reads that no convention names.

Nothing here imports anything. It is the bottom of the import graph on purpose.
"""

from __future__ import annotations

from typing import Final

# ── GenAI: the operation and the model ──
OPERATION_NAME: Final[str] = "gen_ai.operation.name"
PROVIDER_NAME: Final[str] = "gen_ai.provider.name"
REQUEST_MODEL: Final[str] = "gen_ai.request.model"
RESPONSE_MODEL: Final[str] = "gen_ai.response.model"
RESPONSE_ID: Final[str] = "gen_ai.response.id"
RESPONSE_FINISH_REASONS: Final[str] = "gen_ai.response.finish_reasons"

# ── GenAI: the request settings ──
REQUEST_TEMPERATURE: Final[str] = "gen_ai.request.temperature"
REQUEST_TOP_P: Final[str] = "gen_ai.request.top_p"
REQUEST_TOP_K: Final[str] = "gen_ai.request.top_k"
REQUEST_MAX_TOKENS: Final[str] = "gen_ai.request.max_tokens"
REQUEST_SEED: Final[str] = "gen_ai.request.seed"
REQUEST_FREQUENCY_PENALTY: Final[str] = "gen_ai.request.frequency_penalty"
REQUEST_PRESENCE_PENALTY: Final[str] = "gen_ai.request.presence_penalty"
REQUEST_CHOICE_COUNT: Final[str] = "gen_ai.request.choice.count"
REQUEST_STOP_SEQUENCES: Final[str] = "gen_ai.request.stop_sequences"

# ── GenAI: the usage ──
USAGE_INPUT_TOKENS: Final[str] = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS: Final[str] = "gen_ai.usage.output_tokens"
USAGE_TOTAL_TOKENS: Final[str] = "gen_ai.usage.total_tokens"
EMBEDDINGS_DIMENSION_COUNT: Final[str] = "gen_ai.embeddings.dimension.count"

# ── GenAI: the content ──
INPUT_MESSAGES: Final[str] = "gen_ai.input.messages"
OUTPUT_MESSAGES: Final[str] = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS: Final[str] = "gen_ai.system_instructions"

# ── GenAI: tools ──
TOOL_NAME: Final[str] = "gen_ai.tool.name"
TOOL_CALL_ARGUMENTS: Final[str] = "gen_ai.tool.call.arguments"

# ── GenAI: the operation names this SDK emits ──
OPERATION_CHAT: Final[str] = "chat"
OPERATION_EMBEDDINGS: Final[str] = "embeddings"
OPERATION_GENERATE_CONTENT: Final[str] = "generate_content"
OPERATION_EXECUTE_TOOL: Final[str] = "execute_tool"

# ── general conventions ──
SERVER_ADDRESS: Final[str] = "server.address"
SERVER_PORT: Final[str] = "server.port"
ERROR_TYPE: Final[str] = "error.type"
HTTP_REQUEST_METHOD: Final[str] = "http.request.method"
HTTP_RESPONSE_STATUS_CODE: Final[str] = "http.response.status_code"
URL_TEMPLATE: Final[str] = "url.template"
CODE_FUNCTION_NAME: Final[str] = "code.function.name"
CODE_FILE_PATH: Final[str] = "code.file.path"
CODE_LINE_NUMBER: Final[str] = "code.line.number"
DEPLOYMENT_ENVIRONMENT_NAME: Final[str] = "deployment.environment.name"
#: The older spelling of the environment, kept beside the new one for a receiver that reads either.
DEPLOYMENT_ENVIRONMENT: Final[str] = "deployment.environment"

# ── hajer: the ids the platform correlates on ──
WORKFLOW_ID: Final[str] = "hajer.workflow.id"
COMPONENT_ID: Final[str] = "hajer.component.id"
TOOL_ID: Final[str] = "hajer.tool.id"
EVAL_RUN_ID: Final[str] = "hajer.eval.run.id"
EVAL_TEST_CASE_ID: Final[str] = "hajer.eval.test_case.id"
EVAL_WORKFLOW_ID: Final[str] = "hajer.eval.workflow.id"
EVAL_OBLIGATION_IDS: Final[str] = "hajer.eval.obligation.ids"

# ── hajer: what a model span says that no convention names ──
API: Final[str] = "hajer.api"
REQUEST_TOOL_NAMES: Final[str] = "hajer.request.tool_names"
USAGE_CACHE_READ_INPUT_TOKENS: Final[str] = "hajer.usage.cache_read_input_tokens"
USAGE_CACHE_WRITE_INPUT_TOKENS: Final[str] = "hajer.usage.cache_write_input_tokens"
EMBEDDING_VECTORS: Final[str] = "hajer.embedding.vectors"
STREAM: Final[str] = "hajer.stream"
STREAM_COMPLETE: Final[str] = "hajer.stream.complete"
STREAM_CHUNKS: Final[str] = "hajer.stream.chunks"
LIMITATIONS: Final[str] = "hajer.limitations"
