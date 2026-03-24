import MeshNode from '#models/mesh_node'
import MeshMessage from '#models/mesh_message'
import ChatSession from '#models/chat_session'
import KVStore from '#models/kv_store'
import { ChatService } from './chat_service.js'
import { OllamaService } from './ollama_service.js'
import { inject } from '@adonisjs/core'
import logger from '@adonisjs/core/services/logger'
import transmit from '@adonisjs/transmit/services/main'
import { BROADCAST_CHANNELS } from '../../constants/broadcast.js'
import { MESH_COMMANDS, MESH_HELP_TEXT, DEFAULT_MAX_RESPONSE_LENGTH } from '../../constants/meshtastic.js'
import { MeshLLMJob } from '#jobs/mesh_llm_job'
import type { MeshConfig, MeshStatus } from '../../types/meshtastic.js'

@inject()
export class MeshtasticService {
  constructor(
    private chatService: ChatService,
    private ollamaService: OllamaService
  ) {}

  async processIncoming(nodeId: string, content: string, longName?: string, shortName?: string) {
    logger.info(`[MeshtasticService] Incoming message from ${nodeId}: "${content.slice(0, 100)}"`)

    const node = await this.findOrCreateNode(nodeId, longName, shortName)

    if (node.is_blocked) {
      logger.info(`[MeshtasticService] Node ${nodeId} is blocked, ignoring message`)
      return { blocked: true }
    }

    // Update node stats
    node.message_count += 1
    node.last_seen_at = (await import('luxon')).DateTime.now()
    if (longName) node.long_name = longName
    if (shortName) node.short_name = shortName
    await node.save()

    // Parse command prefixes
    const parsed = this.parseCommand(content)

    // Handle special commands
    if (parsed.command === MESH_COMMANDS.HELP) {
      return this.createDirectResponse(nodeId, MESH_HELP_TEXT)
    }

    if (parsed.command === MESH_COMMANDS.STATUS) {
      return this.handleStatusCommand(nodeId)
    }

    if (parsed.command === MESH_COMMANDS.MODEL) {
      return this.handleModelCommand(nodeId, node, parsed.argument)
    }

    // Determine RAG setting
    let useRag = false
    if (parsed.command === MESH_COMMANDS.RAG) {
      useRag = true
    } else if (parsed.command === MESH_COMMANDS.NO_RAG) {
      useRag = false
    } else {
      // Use per-node override or global default
      if (node.rag_enabled !== null) {
        useRag = node.rag_enabled
      } else {
        const globalRag = await KVStore.getValue('meshtastic.ragEnabled')
        useRag = globalRag === true
      }
    }

    // Find or create chat session for this node
    const session = await this.findOrCreateSession(nodeId, node.long_name)

    // Save incoming message
    const incomingMessage = await MeshMessage.create({
      node_id: nodeId,
      direction: 'incoming',
      content: parsed.query,
      status: 'received',
      rag_used: useRag,
      chat_session_id: session.id,
    })

    // Also save to chat messages for UI visibility
    await this.chatService.addMessage(session.id, 'user', parsed.query)

    // Dispatch LLM job
    await MeshLLMJob.dispatch({
      meshMessageId: incomingMessage.id,
      nodeId,
      query: parsed.query,
      useRag,
      sessionId: session.id,
      modelOverride: node.default_model || undefined,
    })

    // Update message status
    incomingMessage.status = 'processing'
    await incomingMessage.save()

    // Broadcast to UI
    this.broadcastMessage(incomingMessage)

    return { queued: true, messageId: incomingMessage.id }
  }

  async getOutgoingMessages(nodeId: string) {
    const messages = await MeshMessage.query()
      .where('node_id', nodeId)
      .where('direction', 'outgoing')
      .where('status', 'completed')
      .orderBy('created_at', 'asc')

    return messages.map((msg) => ({
      id: msg.id,
      content: msg.content,
      nodeId: msg.node_id,
      status: msg.status,
    }))
  }

  async markMessageSent(messageId: number) {
    const message = await MeshMessage.find(messageId)
    if (!message) return null

    message.status = 'sent'
    await message.save()

    this.broadcastMessage(message)
    return message
  }

  async markMessageSending(messageId: number, chunkCount: number) {
    const message = await MeshMessage.find(messageId)
    if (!message) return null

    message.status = 'sending'
    message.chunk_count = chunkCount
    await message.save()

    return message
  }

  async getStatus(): Promise<MeshStatus> {
    const nodeCount = await MeshNode.query().count('* as total')
    const totalMessages = await MeshMessage.query().count('* as total')
    const pendingResponses = await MeshMessage.query()
      .where('direction', 'outgoing')
      .whereIn('status', ['completed', 'sending'])
      .count('* as total')

    return {
      bridgeConnected: false, // Sidecar reports this via status endpoint
      nodeCount: Number(nodeCount[0].$extras.total),
      totalMessages: Number(totalMessages[0].$extras.total),
      pendingResponses: Number(pendingResponses[0].$extras.total),
    }
  }

  async getConfig(): Promise<MeshConfig> {
    const [
      enabled,
      connectionType,
      serialPort,
      tcpHost,
      tcpPort,
      defaultModel,
      ragEnabled,
      maxResponseLength,
      compressionEnabled,
      interChunkDelay,
    ] = await Promise.all([
      KVStore.getValue('meshtastic.enabled'),
      KVStore.getValue('meshtastic.connectionType'),
      KVStore.getValue('meshtastic.serialPort'),
      KVStore.getValue('meshtastic.tcpHost'),
      KVStore.getValue('meshtastic.tcpPort'),
      KVStore.getValue('meshtastic.defaultModel'),
      KVStore.getValue('meshtastic.ragEnabled'),
      KVStore.getValue('meshtastic.maxResponseLength'),
      KVStore.getValue('meshtastic.compressionEnabled'),
      KVStore.getValue('meshtastic.interChunkDelay'),
    ])

    return {
      enabled: enabled === true,
      connectionType: (connectionType as 'serial' | 'tcp') || 'serial',
      serialPort: serialPort || '/dev/ttyUSB0',
      tcpHost: tcpHost || '192.168.1.1',
      tcpPort: tcpPort || '4403',
      defaultModel: defaultModel || '',
      ragEnabled: ragEnabled === true,
      maxResponseLength: maxResponseLength ? parseInt(maxResponseLength, 10) : DEFAULT_MAX_RESPONSE_LENGTH,
      compressionEnabled: compressionEnabled !== false,
      interChunkDelay: interChunkDelay ? parseInt(interChunkDelay, 10) : 2000,
    }
  }

  async updateConfig(updates: Partial<MeshConfig>) {
    const keyMap: Record<string, { kvKey: keyof typeof import('../../types/kv_store.js').KV_STORE_SCHEMA; serialize: (v: any) => string }> = {
      enabled: { kvKey: 'meshtastic.enabled', serialize: String },
      connectionType: { kvKey: 'meshtastic.connectionType', serialize: String },
      serialPort: { kvKey: 'meshtastic.serialPort', serialize: String },
      tcpHost: { kvKey: 'meshtastic.tcpHost', serialize: String },
      tcpPort: { kvKey: 'meshtastic.tcpPort', serialize: String },
      defaultModel: { kvKey: 'meshtastic.defaultModel', serialize: String },
      ragEnabled: { kvKey: 'meshtastic.ragEnabled', serialize: String },
      maxResponseLength: { kvKey: 'meshtastic.maxResponseLength', serialize: String },
      compressionEnabled: { kvKey: 'meshtastic.compressionEnabled', serialize: String },
      interChunkDelay: { kvKey: 'meshtastic.interChunkDelay', serialize: String },
    }

    for (const [field, value] of Object.entries(updates)) {
      const mapping = keyMap[field]
      if (mapping && value !== undefined) {
        await KVStore.setValue(mapping.kvKey as any, mapping.serialize(value) as any)
      }
    }

    return this.getConfig()
  }

  async listNodes() {
    const nodes = await MeshNode.query().orderBy('last_seen_at', 'desc')
    return nodes.map((node) => ({
      id: node.id,
      nodeId: node.node_id,
      longName: node.long_name,
      shortName: node.short_name,
      lastSeenAt: node.last_seen_at?.toISO() || null,
      messageCount: node.message_count,
      isBlocked: node.is_blocked,
      defaultModel: node.default_model,
      ragEnabled: node.rag_enabled,
    }))
  }

  async updateNode(nodeId: string, updates: { isBlocked?: boolean; defaultModel?: string | null; ragEnabled?: boolean | null }) {
    const node = await MeshNode.findBy('node_id', nodeId)
    if (!node) return null

    if (updates.isBlocked !== undefined) node.is_blocked = updates.isBlocked
    if (updates.defaultModel !== undefined) node.default_model = updates.defaultModel
    if (updates.ragEnabled !== undefined) node.rag_enabled = updates.ragEnabled

    await node.save()
    return node
  }

  async listMessages(page: number = 1, limit: number = 50) {
    const messages = await MeshMessage.query()
      .orderBy('created_at', 'desc')
      .paginate(page, limit)

    return {
      data: messages.all().map((msg) => ({
        id: msg.id,
        nodeId: msg.node_id,
        direction: msg.direction,
        content: msg.content,
        status: msg.status,
        modelUsed: msg.model_used,
        ragUsed: msg.rag_used,
        processingMs: msg.processing_ms,
        chunkCount: msg.chunk_count,
        errorMessage: msg.error_message,
        createdAt: msg.created_at?.toISO() || null,
      })),
      meta: {
        total: messages.total,
        perPage: messages.perPage,
        currentPage: messages.currentPage,
        lastPage: messages.lastPage,
      },
    }
  }

  // --- Private helpers ---

  private async findOrCreateNode(nodeId: string, longName?: string, shortName?: string) {
    let node = await MeshNode.findBy('node_id', nodeId)
    if (!node) {
      node = await MeshNode.create({
        node_id: nodeId,
        long_name: longName || null,
        short_name: shortName || null,
        message_count: 0,
        is_blocked: false,
      })
    }
    return node
  }

  private async findOrCreateSession(nodeId: string, longName: string | null): Promise<ChatSession> {
    const titlePrefix = `Mesh: ${nodeId}`
    const existing = await ChatSession.query()
      .whereLike('title', `${titlePrefix}%`)
      .orderBy('updated_at', 'desc')
      .first()

    if (existing) return existing

    const title = longName ? `Mesh: ${nodeId} (${longName})` : titlePrefix
    const result = await this.chatService.createSession(title)
    return await ChatSession.findOrFail(parseInt(result.id))
  }

  private parseCommand(content: string): { command: string | null; query: string; argument: string } {
    const trimmed = content.trim()

    for (const [, cmd] of Object.entries(MESH_COMMANDS)) {
      if (trimmed.toLowerCase().startsWith(cmd + ' ')) {
        const rest = trimmed.slice(cmd.length + 1).trim()
        return { command: cmd, query: rest, argument: rest }
      }
      if (trimmed.toLowerCase() === cmd) {
        return { command: cmd, query: '', argument: '' }
      }
    }

    return { command: null, query: trimmed, argument: '' }
  }

  private async createDirectResponse(nodeId: string, content: string) {
    const message = await MeshMessage.create({
      node_id: nodeId,
      direction: 'outgoing',
      content,
      status: 'completed',
    })

    this.broadcastMessage(message)
    return { direct: true, messageId: message.id }
  }

  private async handleStatusCommand(nodeId: string) {
    try {
      const models = await this.ollamaService.getModels()
      const modelList = models?.map((m) => m.name).join(', ') || 'none'
      const config = await this.getConfig()
      const status = `N.O.M.A.D. Status: Model: ${config.defaultModel || 'auto'}, RAG: ${config.ragEnabled ? 'on' : 'off'}, Installed models: ${modelList}`
      return this.createDirectResponse(nodeId, status)
    } catch {
      return this.createDirectResponse(nodeId, 'N.O.M.A.D. Status: AI service unavailable')
    }
  }

  private async handleModelCommand(nodeId: string, node: MeshNode, modelName: string) {
    if (!modelName) {
      const models = await this.ollamaService.getModels()
      const modelList = models?.map((m) => m.name).join(', ') || 'none'
      return this.createDirectResponse(nodeId, `Available models: ${modelList}`)
    }

    node.default_model = modelName
    await node.save()
    return this.createDirectResponse(nodeId, `Model set to: ${modelName}`)
  }

  private broadcastMessage(message: MeshMessage) {
    transmit.broadcast(BROADCAST_CHANNELS.MESHTASTIC_MESSAGE, {
      id: message.id,
      nodeId: message.node_id,
      direction: message.direction,
      content: message.content.slice(0, 200),
      status: message.status,
      timestamp: new Date().toISOString(),
    })
  }
}
