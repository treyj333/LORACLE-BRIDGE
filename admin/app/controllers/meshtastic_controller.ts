import { inject } from '@adonisjs/core'
import type { HttpContext } from '@adonisjs/core/http'
import { MeshtasticService } from '#services/meshtastic_service'
import { incomingMessageSchema, updateNodeSchema, updateConfigSchema } from '#validators/meshtastic'

@inject()
export default class MeshtasticController {
  constructor(private meshtasticService: MeshtasticService) {}

  async handleIncoming({ request, response }: HttpContext) {
    try {
      const data = await request.validateUsing(incomingMessageSchema)
      const result = await this.meshtasticService.processIncoming(
        data.nodeId,
        data.content,
        data.longName,
        data.shortName
      )
      return response.status(200).json(result)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to process incoming message',
      })
    }
  }

  async getOutgoing({ params, response }: HttpContext) {
    try {
      const nodeId = params.nodeId
      const messages = await this.meshtasticService.getOutgoingMessages(nodeId)
      return response.status(200).json(messages)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to get outgoing messages',
      })
    }
  }

  async markSent({ params, response }: HttpContext) {
    try {
      const messageId = parseInt(params.id)
      const result = await this.meshtasticService.markMessageSent(messageId)
      if (!result) {
        return response.status(404).json({ error: 'Message not found' })
      }
      return response.status(200).json({ success: true })
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to mark message as sent',
      })
    }
  }

  async markSending({ params, request, response }: HttpContext) {
    try {
      const messageId = parseInt(params.id)
      const chunkCount = request.input('chunkCount', 1)
      const result = await this.meshtasticService.markMessageSending(messageId, chunkCount)
      if (!result) {
        return response.status(404).json({ error: 'Message not found' })
      }
      return response.status(200).json({ success: true })
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to update message status',
      })
    }
  }

  async status({ response }: HttpContext) {
    try {
      const result = await this.meshtasticService.getStatus()
      return response.status(200).json(result)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to get status',
      })
    }
  }

  async listNodes({ response }: HttpContext) {
    try {
      const nodes = await this.meshtasticService.listNodes()
      return response.status(200).json(nodes)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to list nodes',
      })
    }
  }

  async updateNode({ params, request, response }: HttpContext) {
    try {
      const nodeId = params.nodeId
      const data = await request.validateUsing(updateNodeSchema)
      const result = await this.meshtasticService.updateNode(nodeId, data)
      if (!result) {
        return response.status(404).json({ error: 'Node not found' })
      }
      return response.status(200).json({ success: true })
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to update node',
      })
    }
  }

  async listMessages({ request, response }: HttpContext) {
    try {
      const page = request.input('page', 1)
      const limit = request.input('limit', 50)
      const result = await this.meshtasticService.listMessages(page, limit)
      return response.status(200).json(result)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to list messages',
      })
    }
  }

  async getConfig({ response }: HttpContext) {
    try {
      const config = await this.meshtasticService.getConfig()
      return response.status(200).json(config)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to get configuration',
      })
    }
  }

  async updateConfig({ request, response }: HttpContext) {
    try {
      const data = await request.validateUsing(updateConfigSchema)
      const config = await this.meshtasticService.updateConfig(data)
      return response.status(200).json(config)
    } catch (error) {
      return response.status(500).json({
        error: error instanceof Error ? error.message : 'Failed to update configuration',
      })
    }
  }
}
