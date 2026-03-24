import vine from '@vinejs/vine'

export const incomingMessageSchema = vine.compile(
  vine.object({
    nodeId: vine.string().trim().minLength(1).maxLength(20),
    content: vine.string().trim().minLength(1),
    longName: vine.string().trim().maxLength(100).optional(),
    shortName: vine.string().trim().maxLength(10).optional(),
  })
)

export const updateNodeSchema = vine.compile(
  vine.object({
    isBlocked: vine.boolean().optional(),
    defaultModel: vine.string().trim().nullable().optional(),
    ragEnabled: vine.boolean().nullable().optional(),
  })
)

export const updateConfigSchema = vine.compile(
  vine.object({
    enabled: vine.boolean().optional(),
    connectionType: vine.enum(['serial', 'tcp'] as const).optional(),
    serialPort: vine.string().trim().optional(),
    tcpHost: vine.string().trim().optional(),
    tcpPort: vine.string().trim().optional(),
    defaultModel: vine.string().trim().optional(),
    ragEnabled: vine.boolean().optional(),
    maxResponseLength: vine.number().min(50).max(5000).optional(),
    compressionEnabled: vine.boolean().optional(),
    interChunkDelay: vine.number().min(500).max(30000).optional(),
  })
)
