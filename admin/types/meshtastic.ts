export interface MeshIncomingPayload {
  nodeId: string
  content: string
  longName?: string
  shortName?: string
}

export interface MeshOutgoingMessage {
  id: number
  content: string
  nodeId: string
  status: string
}

export interface MeshNodeInfo {
  id: number
  nodeId: string
  longName: string | null
  shortName: string | null
  lastSeenAt: string | null
  messageCount: number
  isBlocked: boolean
  defaultModel: string | null
  ragEnabled: boolean | null
}

export interface MeshConfig {
  enabled: boolean
  connectionType: 'serial' | 'tcp'
  serialPort: string
  tcpHost: string
  tcpPort: string
  defaultModel: string
  ragEnabled: boolean
  maxResponseLength: number
  compressionEnabled: boolean
  interChunkDelay: number
}

export interface MeshStatus {
  bridgeConnected: boolean
  nodeCount: number
  totalMessages: number
  pendingResponses: number
}

export interface MeshMessageEntry {
  id: number
  nodeId: string
  direction: 'incoming' | 'outgoing'
  content: string
  status: string
  modelUsed: string | null
  ragUsed: boolean
  processingMs: number | null
  chunkCount: number | null
  errorMessage: string | null
  createdAt: string
}
