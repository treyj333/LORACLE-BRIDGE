import { BaseSchema } from '@adonisjs/lucid/schema'

export default class extends BaseSchema {
  protected tableName = 'mesh_messages'

  async up() {
    this.schema.createTable(this.tableName, (table) => {
      table.increments('id')
      table.string('node_id', 20).notNullable().index()
      table.enum('direction', ['incoming', 'outgoing']).notNullable()
      table.text('content').notNullable()
      table
        .enum('status', ['received', 'processing', 'completed', 'sending', 'sent', 'failed'])
        .notNullable()
        .defaultTo('received')
        .index()
      table.string('model_used', 100).nullable()
      table.boolean('rag_used').defaultTo(false)
      table.integer('processing_ms').nullable()
      table.integer('chunk_count').nullable()
      table.text('error_message').nullable()
      table
        .integer('chat_session_id')
        .unsigned()
        .nullable()
        .references('id')
        .inTable('chat_sessions')
        .onDelete('SET NULL')
      table.timestamp('created_at')
      table.timestamp('updated_at')
    })
  }

  async down() {
    this.schema.dropTable(this.tableName)
  }
}
