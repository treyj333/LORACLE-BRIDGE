import { DateTime } from 'luxon'
import { BaseModel, column, belongsTo, SnakeCaseNamingStrategy } from '@adonisjs/lucid/orm'
import type { BelongsTo } from '@adonisjs/lucid/types/relations'
import ChatSession from './chat_session.js'

export default class MeshMessage extends BaseModel {
  static namingStrategy = new SnakeCaseNamingStrategy()

  @column({ isPrimary: true })
  declare id: number

  @column()
  declare node_id: string

  @column()
  declare direction: 'incoming' | 'outgoing'

  @column()
  declare content: string

  @column()
  declare status: 'received' | 'processing' | 'completed' | 'sending' | 'sent' | 'failed'

  @column()
  declare model_used: string | null

  @column()
  declare rag_used: boolean

  @column()
  declare processing_ms: number | null

  @column()
  declare chunk_count: number | null

  @column()
  declare error_message: string | null

  @column()
  declare chat_session_id: number | null

  @belongsTo(() => ChatSession, { foreignKey: 'chat_session_id', localKey: 'id' })
  declare chatSession: BelongsTo<typeof ChatSession>

  @column.dateTime({ autoCreate: true })
  declare created_at: DateTime

  @column.dateTime({ autoCreate: true, autoUpdate: true })
  declare updated_at: DateTime
}
