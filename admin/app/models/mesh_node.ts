import { DateTime } from 'luxon'
import { BaseModel, column, SnakeCaseNamingStrategy } from '@adonisjs/lucid/orm'

export default class MeshNode extends BaseModel {
  static namingStrategy = new SnakeCaseNamingStrategy()

  @column({ isPrimary: true })
  declare id: number

  @column()
  declare node_id: string

  @column()
  declare long_name: string | null

  @column()
  declare short_name: string | null

  @column.dateTime()
  declare last_seen_at: DateTime | null

  @column()
  declare message_count: number

  @column()
  declare is_blocked: boolean

  @column()
  declare default_model: string | null

  @column()
  declare rag_enabled: boolean | null

  @column.dateTime({ autoCreate: true })
  declare created_at: DateTime

  @column.dateTime({ autoCreate: true, autoUpdate: true })
  declare updated_at: DateTime
}
