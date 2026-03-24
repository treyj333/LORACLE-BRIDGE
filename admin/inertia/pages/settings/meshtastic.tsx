import { useState, useEffect } from 'react'
import { Head } from '@inertiajs/react'
import SettingsLayout from '~/layouts/SettingsLayout'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTransmit } from 'react-adonis-transmit'
import api from '~/lib/api'
import StyledButton from '~/components/StyledButton'
import Alert from '~/components/Alert'
import {
  IconAntenna,
  IconRefresh,
  IconBan,
  IconCheck,
  IconArrowRight,
  IconArrowLeft,
  IconRobot,
} from '@tabler/icons-react'
import { BROADCAST_CHANNELS } from '../../../constants/broadcast'
import type { MeshConfig, MeshNodeInfo, MeshMessageEntry } from '../../../types/meshtastic'

export default function MeshtasticPage() {
  const queryClient = useQueryClient()
  const { subscribe } = useTransmit()

  // Fetch config
  const { data: config, isLoading: configLoading } = useQuery({
    queryKey: ['meshtastic', 'config'],
    queryFn: () => api.getMeshtasticConfig(),
  })

  // Fetch status
  const { data: status } = useQuery({
    queryKey: ['meshtastic', 'status'],
    queryFn: () => api.getMeshtasticStatus(),
    refetchInterval: 10000,
  })

  // Fetch nodes
  const { data: nodes } = useQuery({
    queryKey: ['meshtastic', 'nodes'],
    queryFn: () => api.getMeshtasticNodes(),
    refetchInterval: 15000,
  })

  // Fetch messages
  const { data: messagesData } = useQuery({
    queryKey: ['meshtastic', 'messages'],
    queryFn: () => api.getMeshtasticMessages(1, 50),
    refetchInterval: 5000,
  })

  // Fetch installed models for dropdown
  const { data: installedModels } = useQuery({
    queryKey: ['ollama', 'installed-models'],
    queryFn: async () => {
      const response = await api.getInstalledModels()
      return response || []
    },
  })

  // Subscribe to real-time mesh messages
  useEffect(() => {
    const sub = subscribe(BROADCAST_CHANNELS.MESHTASTIC_MESSAGE)
    if (sub) {
      sub.onMessage(() => {
        queryClient.invalidateQueries({ queryKey: ['meshtastic', 'messages'] })
        queryClient.invalidateQueries({ queryKey: ['meshtastic', 'nodes'] })
      })
    }
  }, [])

  // Config state
  const [localConfig, setLocalConfig] = useState<Partial<MeshConfig>>({})

  useEffect(() => {
    if (config) {
      setLocalConfig(config)
    }
  }, [config])

  // Save config mutation
  const saveConfig = useMutation({
    mutationFn: (updates: Partial<MeshConfig>) => api.updateMeshtasticConfig(updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['meshtastic', 'config'] })
    },
  })

  // Update node mutation
  const updateNode = useMutation({
    mutationFn: ({ nodeId, data }: { nodeId: string; data: { isBlocked?: boolean } }) =>
      api.updateMeshtasticNode(nodeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['meshtastic', 'nodes'] })
    },
  })

  const handleConfigChange = (field: keyof MeshConfig, value: any) => {
    setLocalConfig((prev) => ({ ...prev, [field]: value }))
  }

  const handleSaveConfig = () => {
    saveConfig.mutate(localConfig)
  }

  if (configLoading) {
    return (
      <SettingsLayout>
        <Head title="Meshtastic - Settings" />
        <div className="flex-1 p-8">
          <div className="animate-pulse text-content-secondary">Loading configuration...</div>
        </div>
      </SettingsLayout>
    )
  }

  return (
    <SettingsLayout>
      <Head title="Meshtastic - Settings" />
      <div className="flex-1 p-8 space-y-8 max-w-4xl">
        <div>
          <h1 className="text-2xl font-bold text-content-primary flex items-center gap-2">
            <IconAntenna size={28} />
            Meshtastic Bridge
          </h1>
          <p className="text-content-secondary mt-1">
            Connect a Meshtastic radio to provide AI responses over the LoRa mesh network.
          </p>
        </div>

        {/* Status Banner */}
        {status && (
          <div className="grid grid-cols-4 gap-4">
            <StatusBadge
              label="Bridge"
              value={status.bridgeConnected ? 'Connected' : 'Disconnected'}
              ok={status.bridgeConnected}
            />
            <StatusBadge label="Nodes" value={String(status.nodeCount)} ok={status.nodeCount > 0} />
            <StatusBadge label="Messages" value={String(status.totalMessages)} ok />
            <StatusBadge label="Pending" value={String(status.pendingResponses)} ok={status.pendingResponses === 0} />
          </div>
        )}

        {/* Connection Settings */}
        <Section title="Connection">
          <div className="space-y-4">
            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={localConfig.enabled ?? false}
                onChange={(e) => handleConfigChange('enabled', e.target.checked)}
                className="rounded border-border-primary"
              />
              <span className="text-content-primary font-medium">Enable Meshtastic Bridge</span>
            </label>

            <div>
              <label className="block text-sm font-medium text-content-secondary mb-2">Connection Type</label>
              <div className="flex gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="connectionType"
                    value="serial"
                    checked={localConfig.connectionType === 'serial'}
                    onChange={() => handleConfigChange('connectionType', 'serial')}
                  />
                  <span className="text-content-primary">Serial (USB)</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="connectionType"
                    value="tcp"
                    checked={localConfig.connectionType === 'tcp'}
                    onChange={() => handleConfigChange('connectionType', 'tcp')}
                  />
                  <span className="text-content-primary">TCP (Network)</span>
                </label>
              </div>
            </div>

            {localConfig.connectionType === 'serial' && (
              <InputField
                label="Serial Port"
                value={localConfig.serialPort ?? ''}
                onChange={(v) => handleConfigChange('serialPort', v)}
                placeholder="/dev/ttyUSB0"
              />
            )}

            {localConfig.connectionType === 'tcp' && (
              <div className="grid grid-cols-2 gap-4">
                <InputField
                  label="Host"
                  value={localConfig.tcpHost ?? ''}
                  onChange={(v) => handleConfigChange('tcpHost', v)}
                  placeholder="192.168.1.1"
                />
                <InputField
                  label="Port"
                  value={localConfig.tcpPort ?? ''}
                  onChange={(v) => handleConfigChange('tcpPort', v)}
                  placeholder="4403"
                />
              </div>
            )}
          </div>
        </Section>

        {/* LLM Settings */}
        <Section title="LLM Settings">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-content-secondary mb-1">Default Model</label>
              <select
                value={localConfig.defaultModel ?? ''}
                onChange={(e) => handleConfigChange('defaultModel', e.target.value)}
                className="w-full rounded-md border border-border-primary bg-surface-primary text-content-primary px-3 py-2"
              >
                <option value="">Auto (smallest installed)</option>
                {(installedModels as any[])?.map((m: any) => (
                  <option key={m.name} value={m.name}>
                    {m.name}
                  </option>
                ))}
              </select>
            </div>

            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={localConfig.ragEnabled ?? false}
                onChange={(e) => handleConfigChange('ragEnabled', e.target.checked)}
                className="rounded border-border-primary"
              />
              <span className="text-content-primary">Enable Knowledge Base (RAG) by default</span>
            </label>

            <InputField
              label="Max Response Length (characters)"
              value={String(localConfig.maxResponseLength ?? 500)}
              onChange={(v) => handleConfigChange('maxResponseLength', parseInt(v) || 500)}
              type="number"
            />

            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={localConfig.compressionEnabled ?? true}
                onChange={(e) => handleConfigChange('compressionEnabled', e.target.checked)}
                className="rounded border-border-primary"
              />
              <span className="text-content-primary">Enable compression (reduces chunk count)</span>
            </label>

            <InputField
              label="Inter-chunk Delay (ms)"
              value={String(localConfig.interChunkDelay ?? 2000)}
              onChange={(v) => handleConfigChange('interChunkDelay', parseInt(v) || 2000)}
              type="number"
            />
          </div>
        </Section>

        <StyledButton onClick={handleSaveConfig} disabled={saveConfig.isPending}>
          {saveConfig.isPending ? 'Saving...' : 'Save Configuration'}
        </StyledButton>

        {saveConfig.isSuccess && (
          <Alert type="success" message="Configuration saved successfully." />
        )}

        {/* Node Management */}
        <Section title="Mesh Nodes">
          {nodes && nodes.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-primary text-left text-content-secondary">
                    <th className="pb-2 pr-4">Node ID</th>
                    <th className="pb-2 pr-4">Name</th>
                    <th className="pb-2 pr-4">Last Seen</th>
                    <th className="pb-2 pr-4">Messages</th>
                    <th className="pb-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((node: MeshNodeInfo) => (
                    <tr key={node.nodeId} className="border-b border-border-secondary">
                      <td className="py-2 pr-4 font-mono text-content-primary">{node.nodeId}</td>
                      <td className="py-2 pr-4 text-content-primary">
                        {node.longName || node.shortName || '-'}
                      </td>
                      <td className="py-2 pr-4 text-content-secondary">
                        {node.lastSeenAt ? new Date(node.lastSeenAt).toLocaleString() : 'Never'}
                      </td>
                      <td className="py-2 pr-4 text-content-secondary">{node.messageCount}</td>
                      <td className="py-2">
                        <button
                          onClick={() =>
                            updateNode.mutate({
                              nodeId: node.nodeId,
                              data: { isBlocked: !node.isBlocked },
                            })
                          }
                          className={`text-xs px-2 py-1 rounded ${
                            node.isBlocked
                              ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                              : 'bg-surface-tertiary text-content-secondary hover:text-content-primary'
                          }`}
                        >
                          {node.isBlocked ? (
                            <span className="flex items-center gap-1"><IconBan size={12} /> Blocked</span>
                          ) : (
                            <span className="flex items-center gap-1"><IconCheck size={12} /> Active</span>
                          )}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-content-secondary text-sm">
              No mesh nodes have been seen yet. Connect a Meshtastic radio and send a message to get started.
            </p>
          )}
        </Section>

        {/* Activity Feed */}
        <Section title="Activity Feed">
          {messagesData?.data && messagesData.data.length > 0 ? (
            <div className="space-y-2 max-h-96 overflow-y-auto">
              {messagesData.data.map((msg: MeshMessageEntry) => (
                <div
                  key={msg.id}
                  className="flex items-start gap-3 p-3 rounded-lg bg-surface-primary border border-border-secondary"
                >
                  <div className="mt-0.5">
                    {msg.direction === 'incoming' ? (
                      <IconArrowRight size={16} className="text-blue-500" />
                    ) : (
                      <IconArrowLeft size={16} className="text-green-500" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-xs text-content-secondary">
                      <span className="font-mono">{msg.nodeId}</span>
                      <span>{msg.direction === 'incoming' ? 'received' : 'sent'}</span>
                      <span
                        className={`px-1.5 py-0.5 rounded text-xs ${
                          msg.status === 'sent' || msg.status === 'completed'
                            ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                            : msg.status === 'failed'
                              ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                              : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400'
                        }`}
                      >
                        {msg.status}
                      </span>
                      {msg.modelUsed && (
                        <span className="flex items-center gap-0.5">
                          <IconRobot size={10} /> {msg.modelUsed}
                        </span>
                      )}
                      {msg.ragUsed && <span className="text-purple-500">RAG</span>}
                    </div>
                    <p className="text-sm text-content-primary mt-1 truncate">{msg.content}</p>
                    <div className="text-xs text-content-tertiary mt-1">
                      {msg.createdAt ? new Date(msg.createdAt).toLocaleString() : ''}
                      {msg.processingMs ? ` | ${(msg.processingMs / 1000).toFixed(1)}s` : ''}
                      {msg.chunkCount ? ` | ${msg.chunkCount} chunks` : ''}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-content-secondary text-sm">No messages yet.</p>
          )}
        </Section>
      </div>
    </SettingsLayout>
  )
}

// Helper components

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface-primary rounded-xl border border-border-primary p-6">
      <h2 className="text-lg font-semibold text-content-primary mb-4">{title}</h2>
      {children}
    </div>
  )
}

function StatusBadge({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="bg-surface-primary rounded-lg border border-border-primary p-4 text-center">
      <div className="text-xs text-content-secondary uppercase tracking-wide">{label}</div>
      <div className={`text-lg font-bold mt-1 ${ok ? 'text-green-600 dark:text-green-400' : 'text-content-secondary'}`}>
        {value}
      </div>
    </div>
  )
}

function InputField({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  type?: string
}) {
  return (
    <div>
      <label className="block text-sm font-medium text-content-secondary mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-border-primary bg-surface-primary text-content-primary px-3 py-2"
      />
    </div>
  )
}
