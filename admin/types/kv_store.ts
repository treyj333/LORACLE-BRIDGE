
export const KV_STORE_SCHEMA = {
  'chat.suggestionsEnabled':    'boolean',
  'chat.lastModel':             'string',
  'rag.docsEmbedded':           'boolean',
  'system.updateAvailable':     'boolean',
  'system.latestVersion':       'string',
  'system.earlyAccess':         'boolean',
  'ui.hasVisitedEasySetup':     'boolean',
  'ui.theme':                   'string',
  'ai.assistantCustomName':     'string',
  'gpu.type':                   'string',
  'meshtastic.enabled':         'boolean',
  'meshtastic.connectionType':  'string',
  'meshtastic.serialPort':      'string',
  'meshtastic.tcpHost':         'string',
  'meshtastic.tcpPort':         'string',
  'meshtastic.defaultModel':    'string',
  'meshtastic.ragEnabled':      'boolean',
  'meshtastic.maxResponseLength': 'string',
  'meshtastic.compressionEnabled': 'boolean',
  'meshtastic.interChunkDelay': 'string',
} as const

type KVTagToType<T extends string> = T extends 'boolean' ? boolean : string

export type KVStoreKey = keyof typeof KV_STORE_SCHEMA
export type KVStoreValue<K extends KVStoreKey> = KVTagToType<(typeof KV_STORE_SCHEMA)[K]>
