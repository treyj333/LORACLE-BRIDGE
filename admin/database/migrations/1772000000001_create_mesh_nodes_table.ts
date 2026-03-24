import { BaseSchema } from '@adonisjs/lucid/schema'

export default class extends BaseSchema {
  protected tableName = 'mesh_nodes'

  async up() {
    this.schema.createTable(this.tableName, (table) => {
      table.increments('id')
      table.string('node_id', 20).notNullable().unique()
      table.string('long_name', 100).nullable()
      table.string('short_name', 10).nullable()
      table.timestamp('last_seen_at').nullable()
      table.integer('message_count').defaultTo(0)
      table.boolean('is_blocked').defaultTo(false)
      table.string('default_model', 100).nullable()
      table.boolean('rag_enabled').nullable()
      table.timestamp('created_at')
      table.timestamp('updated_at')
    })
  }

  async down() {
    this.schema.dropTable(this.tableName)
  }
}
