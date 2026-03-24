export const MAX_LORA_PAYLOAD = 228
export const PROTOCOL_HEADER_SIZE = 8
export const EFFECTIVE_PAYLOAD = MAX_LORA_PAYLOAD - PROTOCOL_HEADER_SIZE

export const DEFAULT_MAX_RESPONSE_LENGTH = 500
export const DEFAULT_INTER_CHUNK_DELAY = 2000

export const MESH_COMMANDS = {
  RAG: '!rag',
  NO_RAG: '!norag',
  STATUS: '!status',
  HELP: '!help',
  MODEL: '!model',
} as const

export const MESH_HELP_TEXT =
  'Commands: !rag <query> (use knowledge base), !norag <query> (skip knowledge base), !status (system info), !model <name> (switch model), !help (this message)'

export const MESH_SYSTEM_PROMPT = (maxChars: number) =>
  `You are a helpful AI assistant responding via a low-bandwidth mesh radio network. Keep your response under ${maxChars} characters. Use plain text only — no markdown, no code blocks, no tables. Be direct and concise. Prioritize the most important information first.`

export const MESH_LLM_QUEUE = 'mesh-llm-requests'
export const MESH_LLM_TIMEOUT_MS = 120_000
