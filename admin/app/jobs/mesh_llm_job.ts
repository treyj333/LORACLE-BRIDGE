import { Job } from 'bullmq'
import { QueueService } from '#services/queue_service'
import MeshMessage from '#models/mesh_message'
import ChatMessage from '#models/chat_message'
import KVStore from '#models/kv_store'
import { OllamaService } from '#services/ollama_service'
import { DockerService } from '#services/docker_service'
import { RagService } from '#services/rag_service'
import { ChatService } from '#services/chat_service'
import logger from '@adonisjs/core/services/logger'
import transmit from '@adonisjs/transmit/services/main'
import { BROADCAST_CHANNELS } from '../../constants/broadcast.js'
import { MESH_SYSTEM_PROMPT, MESH_LLM_QUEUE, MESH_LLM_TIMEOUT_MS, DEFAULT_MAX_RESPONSE_LENGTH } from '../../constants/meshtastic.js'
import { RAG_CONTEXT_LIMITS, SYSTEM_PROMPTS } from '../../constants/ollama.js'

export interface MeshLLMJobParams {
  meshMessageId: number
  nodeId: string
  query: string
  useRag: boolean
  sessionId: number
  modelOverride?: string
}

export class MeshLLMJob {
  static get queue() {
    return MESH_LLM_QUEUE
  }

  static get key() {
    return 'mesh-llm-request'
  }

  async handle(job: Job) {
    const { meshMessageId, nodeId, query, useRag, sessionId, modelOverride } = job.data as MeshLLMJobParams
    const startTime = Date.now()

    logger.info(`[MeshLLMJob] Processing mesh LLM request for node ${nodeId}: "${query.slice(0, 100)}"`)

    const ollamaService = new OllamaService()
    const dockerService = new DockerService()
    const ragService = new RagService(dockerService, ollamaService)
    const chatService = new ChatService(ollamaService)

    try {
      // Determine model to use
      const model = await this.resolveModel(ollamaService, modelOverride)
      if (!model) {
        await this.failMessage(meshMessageId, 'AI service unavailable. No models installed.', nodeId)
        return
      }

      // Get max response length from config
      const maxLengthStr = await KVStore.getValue('meshtastic.maxResponseLength')
      const maxResponseLength = maxLengthStr ? parseInt(maxLengthStr, 10) : DEFAULT_MAX_RESPONSE_LENGTH

      // Build conversation context from recent chat history
      const messages: { role: 'system' | 'user' | 'assistant'; content: string }[] = []

      // Add mesh-specific system prompt
      messages.push({
        role: 'system',
        content: MESH_SYSTEM_PROMPT(maxResponseLength),
      })

      // Load recent messages from this session for context
      const recentMessages = await ChatMessage.query()
        .where('session_id', sessionId)
        .orderBy('created_at', 'desc')
        .limit(6)

      // Reverse to chronological order and add to messages (skip system messages)
      const chronological = recentMessages.reverse()
      for (const msg of chronological) {
        if (msg.role !== 'system') {
          messages.push({ role: msg.role, content: msg.content })
        }
      }

      // Optionally inject RAG context
      if (useRag) {
        try {
          const relevantDocs = await ragService.searchSimilarDocuments(query, 5, 0.3)
          if (relevantDocs.length > 0) {
            const { maxResults, maxTokens } = this.getContextLimitsForModel(model)
            let trimmedDocs = relevantDocs.slice(0, maxResults)

            if (maxTokens > 0) {
              const charCap = maxTokens * 4
              let totalChars = 0
              trimmedDocs = trimmedDocs.filter((doc, idx) => {
                totalChars += doc.text.length
                return idx === 0 || totalChars <= charCap
              })
            }

            const contextText = trimmedDocs
              .map((doc, idx) => `[Context ${idx + 1}] (${(doc.score * 100).toFixed(0)}%)\n${doc.text}`)
              .join('\n\n')

            // Insert RAG context after system prompt
            messages.splice(1, 0, {
              role: 'system',
              content: SYSTEM_PROMPTS.rag_context(contextText),
            })

            logger.info(`[MeshLLMJob] Injected ${trimmedDocs.length} RAG results for mesh query`)
          }
        } catch (ragError) {
          logger.warn(`[MeshLLMJob] RAG search failed, proceeding without: ${ragError instanceof Error ? ragError.message : ragError}`)
        }
      }

      // Call Ollama (non-streaming)
      const timeoutPromise = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('LLM request timed out')), MESH_LLM_TIMEOUT_MS)
      )

      const chatPromise = ollamaService.chat({
        model,
        messages,
        stream: false,
      })

      const result = await Promise.race([chatPromise, timeoutPromise])

      let responseContent = result.message?.content?.trim() || 'No response generated.'

      // Truncate if necessary
      if (responseContent.length > maxResponseLength) {
        responseContent = responseContent.slice(0, maxResponseLength - 15) + ' ...[truncated]'
      }

      const processingMs = Date.now() - startTime

      // Save outgoing mesh message
      const outgoing = await MeshMessage.create({
        node_id: nodeId,
        direction: 'outgoing',
        content: responseContent,
        status: 'completed',
        model_used: model,
        rag_used: useRag,
        processing_ms: processingMs,
        chat_session_id: sessionId,
      })

      // Save to chat messages for UI
      await chatService.addMessage(sessionId, 'assistant', responseContent)

      // Update incoming message status
      const incoming = await MeshMessage.find(meshMessageId)
      if (incoming) {
        incoming.status = 'completed'
        incoming.model_used = model
        incoming.processing_ms = processingMs
        await incoming.save()
      }

      // Broadcast
      transmit.broadcast(BROADCAST_CHANNELS.MESHTASTIC_MESSAGE, {
        id: outgoing.id,
        nodeId,
        direction: 'outgoing',
        content: responseContent.slice(0, 200),
        status: 'completed',
        timestamp: new Date().toISOString(),
      })

      logger.info(`[MeshLLMJob] Completed mesh response for ${nodeId} in ${processingMs}ms (${responseContent.length} chars)`)

      return { success: true, messageId: outgoing.id, processingMs }
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : String(error)
      logger.error(`[MeshLLMJob] Failed to process mesh LLM request: ${errorMsg}`)

      const friendlyError = errorMsg.includes('timed out')
        ? 'Request timed out. Try a simpler question.'
        : 'AI service unavailable. Please try later.'

      await this.failMessage(meshMessageId, friendlyError, nodeId, sessionId)
      throw error
    }
  }

  private async resolveModel(ollamaService: OllamaService, override?: string): Promise<string | null> {
    try {
      const models = await ollamaService.getModels()
      if (!models || models.length === 0) return null

      if (override) {
        const found = models.find((m) => m.name === override)
        if (found) return found.name
      }

      // Check global default
      const defaultModel = await KVStore.getValue('meshtastic.defaultModel')
      if (defaultModel) {
        const found = models.find((m) => m.name === defaultModel)
        if (found) return found.name
      }

      // Fallback to smallest model (best for mesh's concise responses)
      const smallest = models.reduce((prev, current) => (prev.size < current.size ? prev : current))
      return smallest.name
    } catch {
      return null
    }
  }

  private getContextLimitsForModel(modelName: string): { maxResults: number; maxTokens: number } {
    const sizeMatch = modelName.match(/(\d+\.?\d*)[bB]/)
    const paramBillions = sizeMatch ? parseFloat(sizeMatch[1]) : 8

    for (const tier of RAG_CONTEXT_LIMITS) {
      if (paramBillions <= tier.maxParams) {
        return { maxResults: tier.maxResults, maxTokens: tier.maxTokens }
      }
    }

    return { maxResults: 5, maxTokens: 0 }
  }

  private async failMessage(meshMessageId: number, errorText: string, nodeId: string, sessionId?: number) {
    const incoming = await MeshMessage.find(meshMessageId)
    if (incoming) {
      incoming.status = 'failed'
      incoming.error_message = errorText
      await incoming.save()
    }

    // Create error response so the mesh user gets feedback
    const errorResponse = await MeshMessage.create({
      node_id: nodeId,
      direction: 'outgoing',
      content: errorText,
      status: 'completed',
      chat_session_id: sessionId || null,
    })

    transmit.broadcast(BROADCAST_CHANNELS.MESHTASTIC_MESSAGE, {
      id: errorResponse.id,
      nodeId,
      direction: 'outgoing',
      content: errorText,
      status: 'completed',
      timestamp: new Date().toISOString(),
    })
  }

  static async dispatch(params: MeshLLMJobParams) {
    const queueService = new QueueService()
    const queue = queueService.getQueue(this.queue)

    const job = await queue.add(this.key, params, {
      attempts: 3,
      backoff: {
        type: 'exponential',
        delay: 5000,
      },
      removeOnComplete: true,
      removeOnFail: false,
    })

    return { job }
  }
}
