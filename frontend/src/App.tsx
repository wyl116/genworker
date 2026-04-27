import { useEffect, useRef, useState } from "react"

type Role = "assistant" | "user"
type ViewMode = "chat" | "workers"
type WorkerConfigTab = "persona" | "duties" | "goals" | "im"

type Message = {
  role: Role
  content: string
}

type RuntimeSnapshot = {
  status: string
  runtime_profile: string
  tenant_id: string
  default_worker_id: string
  worker_loaded: boolean
  model_ready: boolean
  dependencies: Record<string, string>
  blocking_reasons?: string[]
  warnings?: string[]
}

type WorkerOverview = {
  tenant_id: string
  worker_count: number
  workers: WorkerSummary[]
  persona_reload: {
    configured: boolean
    running: boolean
    reload_count: number
    tracked_workers: number
    last_error: string
  }
}

type WorkerSummary = {
  worker_id: string
  name: string
  backend_online: boolean
  autonomous_capabilities: {
    duty_scheduling: boolean
    goal_health_checks: boolean
    sensing: boolean
  }
  scheduler: {
    registered: boolean
    active_count: number
    queue_size: number
    daily_count: number
    max_concurrent_tasks: number
    daily_task_quota: number
    goal_check_enabled: boolean
  }
  triggers: {
    duty_count: number
    resource_count: number
  }
  sensors: {
    sensor_count: number
  }
  reload_status: {
    trigger_source?: string
    changed_files?: string[]
    reloaded_at?: string
  }
  runtime: {
    worker_dir_exists: boolean
    duties_count: number
    goals_count: number
    active_task_count: number
  }
}

type WorkerConfig = {
  tenant_id: string
  worker_id: string
  worker_dir: string
  persona: WorkerDocument
  duties: WorkerDocument[]
  goals: WorkerDocument[]
  credentials: {
    filename: string
    path: string
    exists: boolean
  }
}

type WorkerDocument = {
  filename: string
  path: string
  content: string
}

type IMConfigResponse = {
  worker_id: string
  persona: {
    channels: IMChannel[]
  }
  credentials: IMCredentialsMasked
}

type IMChannel = {
  type: string
  connection_mode: string
  chat_ids: string[]
  reply_mode: string
  features: Record<string, unknown>
}

type IMCredentialsMasked = {
  feishu?: {
    app_id?: string
    app_secret?: string
  }
  slack?: {
    bot_token?: string
    app_token?: string
    signing_secret?: string
    team_id?: string
  }
}

type IMStatusResponse = {
  adapters: Array<{
    type: string
    registered: boolean
    connection_mode: string
    healthy: boolean
    chat_ids: string[]
    last_error: string
  }>
}

type ChannelDraft = {
  type: "slack" | "feishu"
  connectionMode: "socket_mode" | "websocket"
  chatIdsText: string
  replyMode: "complete" | "streaming"
  featuresText: string
}

type CredentialsDraft = {
  slack: {
    bot_token: string
    app_token: string
    signing_secret: string
    team_id: string
  }
  feishu: {
    app_id: string
    app_secret: string
  }
}

const demoPrompts = [
  "帮我总结一下今天的待办",
  "根据当前项目生成一个最小 MVP 清单",
  "检查当前运行配置是否完整",
]

const initialAssistantMessage =
  "你好，我是 genworker。当前运行在本地社区版模式，你可以直接开始对话。"

const emptyCredentialsDraft: CredentialsDraft = {
  slack: {
    bot_token: "",
    app_token: "",
    signing_secret: "",
    team_id: "",
  },
  feishu: {
    app_id: "",
    app_secret: "",
  },
}

export function App() {
  const [view, setView] = useState<ViewMode>("chat")
  const [messages, setMessages] = useState<Message[]>([
    { role: "assistant", content: initialAssistantMessage },
  ])
  const [input, setInput] = useState("")
  const [runtime, setRuntime] = useState<RuntimeSnapshot | null>(null)
  const [status, setStatus] = useState("booting")
  const [error, setError] = useState("")
  const [streaming, setStreaming] = useState(false)
  const [tenantId, setTenantId] = useState("demo")
  const [workerOverview, setWorkerOverview] = useState<WorkerOverview | null>(null)
  const [workerOverviewLoading, setWorkerOverviewLoading] = useState(false)
  const [workerOverviewError, setWorkerOverviewError] = useState("")
  const [selectedWorkerId, setSelectedWorkerId] = useState("")
  const [workerConfig, setWorkerConfig] = useState<WorkerConfig | null>(null)
  const [workerConfigLoading, setWorkerConfigLoading] = useState(false)
  const [workerConfigError, setWorkerConfigError] = useState("")
  const [workerReloadNotice, setWorkerReloadNotice] = useState("")
  const [workerReloading, setWorkerReloading] = useState(false)
  const [workerConfigTab, setWorkerConfigTab] = useState<WorkerConfigTab>("persona")
  const [imConfig, setImConfig] = useState<IMConfigResponse | null>(null)
  const [imStatus, setImStatus] = useState<IMStatusResponse | null>(null)
  const [imMaskedCredentials, setImMaskedCredentials] = useState<IMCredentialsMasked>({})
  const [channelDrafts, setChannelDrafts] = useState<ChannelDraft[]>([])
  const [credentialsDraft, setCredentialsDraft] = useState<CredentialsDraft>(emptyCredentialsDraft)
  const [imLoading, setImLoading] = useState(false)
  const [imSaving, setImSaving] = useState(false)
  const [imNotice, setImNotice] = useState("")
  const [imError, setImError] = useState("")
  const [imAvailable, setImAvailable] = useState(false)
  const threadIdRef = useRef(`chat-${Date.now()}`)

  useEffect(() => {
    void refreshRuntime()
  }, [])

  useEffect(() => {
    void refreshWorkerOverview(tenantId)
  }, [tenantId])

  useEffect(() => {
    if (!workerOverview?.workers.length) {
      setSelectedWorkerId("")
      return
    }
    const exists = workerOverview.workers.some((worker) => worker.worker_id === selectedWorkerId)
    if (!exists) {
      setSelectedWorkerId(workerOverview.workers[0].worker_id)
    }
  }, [selectedWorkerId, workerOverview])

  useEffect(() => {
    if (!selectedWorkerId) {
      setWorkerConfig(null)
      setImConfig(null)
      setImStatus(null)
      return
    }
    void refreshWorkerConfig(selectedWorkerId)
    void refreshImState(selectedWorkerId)
  }, [selectedWorkerId, tenantId])

  async function refreshRuntime() {
    try {
      const payload = await requestJson<RuntimeSnapshot>("/api/v1/debug/runtime")
      setRuntime(payload)
      setStatus(payload.status === "ready" ? "ready" : "degraded")
      setError("")
      if (payload.tenant_id && payload.tenant_id !== tenantId) {
        setTenantId(payload.tenant_id)
      }
    } catch (err) {
      setStatus("error")
      setError(String(err))
    }
  }

  async function refreshWorkerOverview(nextTenantId: string) {
    setWorkerOverviewLoading(true)
    setWorkerOverviewError("")
    try {
      const payload = await requestJson<WorkerOverview>(
        `/api/v1/worker/ops/overview?tenant_id=${encodeURIComponent(nextTenantId)}`,
      )
      setWorkerOverview(payload)
    } catch (err) {
      setWorkerOverviewError(String(err))
    } finally {
      setWorkerOverviewLoading(false)
    }
  }

  async function refreshWorkerConfig(workerId: string) {
    setWorkerConfigLoading(true)
    setWorkerConfigError("")
    try {
      const payload = await requestJson<WorkerConfig>(
        `/api/v1/worker/ops/config?tenant_id=${encodeURIComponent(tenantId)}&worker_id=${encodeURIComponent(workerId)}`,
      )
      setWorkerConfig(payload)
    } catch (err) {
      setWorkerConfigError(String(err))
    } finally {
      setWorkerConfigLoading(false)
    }
  }

  async function refreshImState(workerId: string) {
    setImLoading(true)
    setImError("")
    setImNotice("")
    try {
      const [configPayload, statusPayload] = await Promise.all([
        requestJson<IMConfigResponse>(
          `/api/v1/workers/${encodeURIComponent(workerId)}/im-config?tenant_id=${encodeURIComponent(tenantId)}`,
        ),
        requestJson<IMStatusResponse>(
          `/api/v1/workers/${encodeURIComponent(workerId)}/im-config/status?tenant_id=${encodeURIComponent(tenantId)}`,
        ),
      ])
      setImAvailable(true)
      setImConfig(configPayload)
      setImStatus(statusPayload)
      setImMaskedCredentials(configPayload.credentials || {})
      setChannelDrafts(configPayload.persona.channels.map(channelDraftFromApi))
      setCredentialsDraft(emptyCredentialsDraft)
    } catch (err) {
      setImAvailable(false)
      setImConfig(null)
      setImStatus(null)
      setImMaskedCredentials({})
      setChannelDrafts([])
      setCredentialsDraft(emptyCredentialsDraft)
      setImError(String(err))
    } finally {
      setImLoading(false)
    }
  }

  async function reloadWorker() {
    if (!selectedWorkerId || workerReloading) {
      return
    }
    setWorkerReloading(true)
    setWorkerReloadNotice("")
    setWorkerOverviewError("")
    try {
      const payload = await requestJson<{ status: string }>(
        `/api/v1/worker/ops/reload?tenant_id=${encodeURIComponent(tenantId)}&worker_id=${encodeURIComponent(selectedWorkerId)}`,
        { method: "POST" },
      )
      setWorkerReloadNotice(`worker 已重载: ${payload.status}`)
      await Promise.all([
        refreshRuntime(),
        refreshWorkerOverview(tenantId),
        refreshWorkerConfig(selectedWorkerId),
        refreshImState(selectedWorkerId),
      ])
    } catch (err) {
      setWorkerReloadNotice(String(err))
    } finally {
      setWorkerReloading(false)
    }
  }

  async function saveImConfig() {
    if (!selectedWorkerId || imSaving) {
      return
    }
    setImSaving(true)
    setImError("")
    setImNotice("")
    try {
      const payload = buildImPayload(channelDrafts, credentialsDraft)
      await requestJson(
        `/api/v1/workers/${encodeURIComponent(selectedWorkerId)}/im-config?tenant_id=${encodeURIComponent(tenantId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      )
      setImNotice("IM 配置已保存")
      await refreshImState(selectedWorkerId)
      await refreshWorkerConfig(selectedWorkerId)
    } catch (err) {
      setImError(String(err))
    } finally {
      setImSaving(false)
    }
  }

  async function reloadImConfig() {
    if (!selectedWorkerId || imSaving) {
      return
    }
    setImSaving(true)
    setImError("")
    setImNotice("")
    try {
      await requestJson(
        `/api/v1/workers/${encodeURIComponent(selectedWorkerId)}/im-config/reload?tenant_id=${encodeURIComponent(tenantId)}`,
        { method: "POST" },
      )
      setImNotice("IM 运行时已重载")
      await Promise.all([
        refreshWorkerOverview(tenantId),
        refreshWorkerConfig(selectedWorkerId),
        refreshImState(selectedWorkerId),
      ])
    } catch (err) {
      setImError(String(err))
    } finally {
      setImSaving(false)
    }
  }

  async function sendMessage(nextMessage?: string) {
    const message = (nextMessage ?? input).trim()
    if (!message || streaming) {
      return
    }
    setStreaming(true)
    setStatus("streaming")
    setError("")
    setMessages((current) => [
      ...current,
      { role: "user", content: message },
      { role: "assistant", content: "" },
    ])
    setInput("")

    const workerId = runtime?.default_worker_id || undefined
    try {
      const response = await fetch("/api/v1/chat/stream?protocol=ag-ui", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          thread_id: threadIdRef.current,
          tenant_id: runtime?.tenant_id || tenantId,
          worker_id: workerId,
        }),
      })
      if (!response.ok || !response.body) {
        throw new Error(`chat request failed: ${response.status}`)
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      while (true) {
        const result = await reader.read()
        if (result.done) {
          break
        }
        buffer += decoder.decode(result.value, { stream: true })
        const blocks = buffer.split("\n\n")
        buffer = blocks.pop() ?? ""
        for (const block of blocks) {
          const lines = block.split("\n")
          const dataLine = lines.find((line) => line.startsWith("data:"))?.slice(5).trim()
          if (!dataLine) {
            continue
          }
          const payload = JSON.parse(dataLine) as Record<string, unknown>
          const event =
            lines.find((line) => line.startsWith("event:"))?.slice(6).trim()
            || String(payload.type ?? "")
          if (!event) {
            continue
          }
          if (event === "TEXT_MESSAGE_CONTENT") {
            const delta = String(payload.delta ?? "")
            setMessages((current) => {
              const next = [...current]
              const last = next[next.length - 1]
              if (last && last.role === "assistant") {
                last.content += delta
              }
              return next
            })
          }
          if (event === "RUN_ERROR") {
            throw new Error(String(payload.message ?? "run error"))
          }
        }
      }
      setStatus("ready")
      await refreshRuntime()
    } catch (err) {
      setStatus("error")
      setError(String(err))
    } finally {
      setStreaming(false)
    }
  }

  function startNewConversation() {
    if (streaming) {
      return
    }
    threadIdRef.current = `chat-${Date.now()}`
    setMessages([{ role: "assistant", content: initialAssistantMessage }])
    setError("")
    setStatus(runtime?.status === "ready" ? "ready" : "degraded")
  }

  function clearConversation() {
    if (streaming) {
      return
    }
    setMessages([{ role: "assistant", content: initialAssistantMessage }])
    setError("")
  }

  function addChannel(type: "slack" | "feishu") {
    if (channelDrafts.some((item) => item.type === type)) {
      return
    }
    setChannelDrafts((current) => [...current, createEmptyChannelDraft(type)])
  }

  function removeChannel(type: "slack" | "feishu") {
    setChannelDrafts((current) => current.filter((item) => item.type !== type))
  }

  function updateChannelDraft(index: number, patch: Partial<ChannelDraft>) {
    setChannelDrafts((current) =>
      current.map((item, itemIndex) => {
        if (itemIndex !== index) {
          return item
        }
        const nextType = (patch.type ?? item.type) as "slack" | "feishu"
        return {
          ...item,
          ...patch,
          type: nextType,
          connectionMode: nextType === "slack" ? "socket_mode" : "websocket",
        }
      }),
    )
  }

  const selectedWorker = workerOverview?.workers.find((worker) => worker.worker_id === selectedWorkerId) || null

  return (
    <div className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Agent Runtime Console</p>
          <h1>genworker</h1>
          <p className="hero-copy">
            本地聊天入口和 worker 管理台共用一个单页壳，默认 tenant 为
            {" "}
            <strong>{tenantId}</strong>
          </p>
        </div>
        <div className={`health-pill health-${status}`}>health: {status}</div>
      </header>

      <section className="topbar">
        <div className="tab-strip">
          <button
            type="button"
            className={view === "chat" ? "is-active" : ""}
            onClick={() => setView("chat")}
          >
            对话
          </button>
          <button
            type="button"
            className={view === "workers" ? "is-active" : ""}
            onClick={() => setView("workers")}
          >
            Worker 管理
          </button>
        </div>
        <div className="topbar-actions">
          <button type="button" className="secondary-button" onClick={() => void refreshRuntime()}>
            刷新运行时
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => void refreshWorkerOverview(tenantId)}
          >
            刷新 worker
          </button>
        </div>
      </section>

      {view === "chat" ? (
        <>
          <main className="layout">
            <section className="conversation-pane">
              <div className="messages">
                {messages.map((message, index) => (
                  <article key={`${message.role}-${index}`} className={`bubble bubble-${message.role}`}>
                    <span className="bubble-role">{message.role}</span>
                    <p>{message.content || (streaming ? "..." : "")}</p>
                  </article>
                ))}
              </div>
            </section>
            <aside className="status-pane">
              <h2>运行状态</h2>
              <dl>
                <div><dt>当前 worker</dt><dd>{runtime?.default_worker_id || "-"}</dd></div>
                <div><dt>当前 tenant</dt><dd>{runtime?.tenant_id || tenantId}</dd></div>
                <div><dt>runtime</dt><dd>{runtime?.runtime_profile || "local"}</dd></div>
                <div><dt>readiness</dt><dd>{runtime?.status || status}</dd></div>
                <div><dt>model</dt><dd>{runtime?.model_ready ? "configured" : "missing"}</dd></div>
                <div><dt>redis</dt><dd>{runtime?.dependencies?.redis || "unknown"}</dd></div>
                <div><dt>mysql</dt><dd>{runtime?.dependencies?.mysql || "unknown"}</dd></div>
                <div><dt>openviking</dt><dd>{runtime?.dependencies?.openviking || "unknown"}</dd></div>
              </dl>
              {runtime?.blocking_reasons?.length ? (
                <div className="hint-panel">
                  <strong>阻塞原因</strong>
                  <ul>
                    {runtime.blocking_reasons.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {runtime?.warnings?.length ? (
                <div className="hint-panel">
                  <strong>告警</strong>
                  <ul>
                    {runtime.warnings.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {error ? <div className="error-banner">{error}</div> : null}
            </aside>
          </main>
          <section className="prompt-bar">
            {demoPrompts.map((prompt) => (
              <button key={prompt} type="button" onClick={() => void sendMessage(prompt)}>
                {prompt}
              </button>
            ))}
          </section>
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault()
              void sendMessage()
            }}
          >
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="输入消息"
              disabled={streaming}
            />
            <button type="submit" disabled={streaming}>发送</button>
          </form>
          <div className="conversation-actions">
            <button type="button" onClick={startNewConversation} disabled={streaming}>新建会话</button>
            <button type="button" onClick={clearConversation} disabled={streaming}>清空会话</button>
          </div>
        </>
      ) : (
        <main className="console-layout">
          <section className="worker-list-panel">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Workers</p>
                <h2>运行总览</h2>
              </div>
              <span className="metric-badge">
                {workerOverview?.worker_count ?? 0}
                {" "}
                workers
              </span>
            </div>
            {workerOverviewLoading ? <p className="placeholder">加载 worker 中...</p> : null}
            {workerOverviewError ? <div className="error-banner">{workerOverviewError}</div> : null}
            {workerOverview?.persona_reload ? (
              <div className="reload-summary">
                <div><span>自动重载</span><strong>{workerOverview.persona_reload.running ? "running" : "stopped"}</strong></div>
                <div><span>tracked</span><strong>{workerOverview.persona_reload.tracked_workers}</strong></div>
                <div><span>reloads</span><strong>{workerOverview.persona_reload.reload_count}</strong></div>
              </div>
            ) : null}
            <div className="worker-list">
              {workerOverview?.workers.map((worker) => (
                <button
                  key={worker.worker_id}
                  type="button"
                  className={`worker-list-item ${selectedWorkerId === worker.worker_id ? "is-selected" : ""}`}
                  onClick={() => setSelectedWorkerId(worker.worker_id)}
                >
                  <span className="worker-list-title">{worker.name || worker.worker_id}</span>
                  <span className={`status-dot ${worker.backend_online ? "online" : "offline"}`} />
                  <span className="worker-list-meta">{worker.worker_id}</span>
                </button>
              ))}
              {!workerOverview?.workers.length && !workerOverviewLoading ? (
                <p className="placeholder">当前 tenant 下没有已加载 worker。</p>
              ) : null}
            </div>
          </section>

          <section className="worker-detail-panel">
            <div className="panel-header">
              <div>
                <p className="eyebrow">Selected Worker</p>
                <h2>{selectedWorker?.name || selectedWorkerId || "未选择"}</h2>
              </div>
              <div className="topbar-actions">
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void reloadWorker()}
                  disabled={!selectedWorkerId || workerReloading}
                >
                  {workerReloading ? "重载中..." : "重载 worker"}
                </button>
              </div>
            </div>

            {selectedWorker ? (
              <div className="stats-grid">
                <article className="stat-card">
                  <span>backend</span>
                  <strong>{selectedWorker.backend_online ? "online" : "offline"}</strong>
                </article>
                <article className="stat-card">
                  <span>duties</span>
                  <strong>{selectedWorker.runtime.duties_count}</strong>
                </article>
                <article className="stat-card">
                  <span>goals</span>
                  <strong>{selectedWorker.runtime.goals_count}</strong>
                </article>
                <article className="stat-card">
                  <span>active tasks</span>
                  <strong>{selectedWorker.runtime.active_task_count}</strong>
                </article>
              </div>
            ) : null}

            {selectedWorker ? (
              <div className="summary-grid">
                <article className="summary-card">
                  <h3>自治能力</h3>
                  <dl>
                    <div><dt>Duty 调度</dt><dd>{selectedWorker.autonomous_capabilities.duty_scheduling ? "on" : "off"}</dd></div>
                    <div><dt>Goal 检查</dt><dd>{selectedWorker.autonomous_capabilities.goal_health_checks ? "on" : "off"}</dd></div>
                    <div><dt>Sensing</dt><dd>{selectedWorker.autonomous_capabilities.sensing ? "on" : "off"}</dd></div>
                  </dl>
                </article>
                <article className="summary-card">
                  <h3>调度器</h3>
                  <dl>
                    <div><dt>registered</dt><dd>{selectedWorker.scheduler.registered ? "yes" : "no"}</dd></div>
                    <div><dt>active</dt><dd>{selectedWorker.scheduler.active_count}</dd></div>
                    <div><dt>queue</dt><dd>{selectedWorker.scheduler.queue_size}</dd></div>
                    <div><dt>daily</dt><dd>{selectedWorker.scheduler.daily_count}/{selectedWorker.scheduler.daily_task_quota}</dd></div>
                  </dl>
                </article>
                <article className="summary-card">
                  <h3>Trigger / Sensor</h3>
                  <dl>
                    <div><dt>trigger resources</dt><dd>{selectedWorker.triggers.resource_count}</dd></div>
                    <div><dt>duties</dt><dd>{selectedWorker.triggers.duty_count}</dd></div>
                    <div><dt>sensors</dt><dd>{selectedWorker.sensors.sensor_count}</dd></div>
                    <div><dt>last reload</dt><dd>{selectedWorker.reload_status.reloaded_at || "-"}</dd></div>
                  </dl>
                </article>
              </div>
            ) : null}

            {workerReloadNotice ? <div className="hint-banner">{workerReloadNotice}</div> : null}
            {workerConfigError ? <div className="error-banner">{workerConfigError}</div> : null}

            <div className="subtab-strip">
              <button
                type="button"
                className={workerConfigTab === "persona" ? "is-active" : ""}
                onClick={() => setWorkerConfigTab("persona")}
              >
                PERSONA
              </button>
              <button
                type="button"
                className={workerConfigTab === "duties" ? "is-active" : ""}
                onClick={() => setWorkerConfigTab("duties")}
              >
                Duties
              </button>
              <button
                type="button"
                className={workerConfigTab === "goals" ? "is-active" : ""}
                onClick={() => setWorkerConfigTab("goals")}
              >
                Goals
              </button>
              <button
                type="button"
                className={workerConfigTab === "im" ? "is-active" : ""}
                onClick={() => setWorkerConfigTab("im")}
              >
                IM 配置
              </button>
            </div>

            {workerConfigLoading ? <p className="placeholder">加载 worker 配置中...</p> : null}

            {workerConfigTab === "persona" && workerConfig ? (
              <article className="editor-panel">
                <div className="document-header">
                  <strong>{workerConfig.persona.filename}</strong>
                  <span>{workerConfig.persona.path}</span>
                </div>
                <textarea readOnly value={workerConfig.persona.content} />
              </article>
            ) : null}

            {workerConfigTab === "duties" && workerConfig ? (
              <section className="document-stack">
                {workerConfig.duties.map((item) => (
                  <article key={item.path} className="document-card">
                    <div className="document-header">
                      <strong>{item.filename}</strong>
                      <span>{item.path}</span>
                    </div>
                    <textarea readOnly value={item.content} />
                  </article>
                ))}
                {!workerConfig.duties.length ? <p className="placeholder">该 worker 暂无 duties 文件。</p> : null}
              </section>
            ) : null}

            {workerConfigTab === "goals" && workerConfig ? (
              <section className="document-stack">
                {workerConfig.goals.map((item) => (
                  <article key={item.path} className="document-card">
                    <div className="document-header">
                      <strong>{item.filename}</strong>
                      <span>{item.path}</span>
                    </div>
                    <textarea readOnly value={item.content} />
                  </article>
                ))}
                {!workerConfig.goals.length ? <p className="placeholder">该 worker 暂无 goals 文件。</p> : null}
              </section>
            ) : null}

            {workerConfigTab === "im" ? (
              <section className="editor-panel">
                {imLoading ? <p className="placeholder">加载 IM 配置中...</p> : null}
                {!imLoading && !imAvailable ? (
                  <div className="hint-panel">
                    <strong>IM 配置暂不可用</strong>
                    <p>
                      常见原因是本地 `IM_CHANNEL_ENABLED=false`，或者该 worker 还没有可读取的
                      `PERSONA.md` / IM 配置文件。
                    </p>
                    {imError ? <p className="compact-error">{imError}</p> : null}
                  </div>
                ) : null}

                {imAvailable ? (
                  <>
                    <div className="im-toolbar">
                      <div className="topbar-actions">
                        <button type="button" className="secondary-button" onClick={() => addChannel("slack")}>
                          添加 Slack
                        </button>
                        <button type="button" className="secondary-button" onClick={() => addChannel("feishu")}>
                          添加飞书
                        </button>
                      </div>
                      <div className="topbar-actions">
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() => void reloadImConfig()}
                          disabled={imSaving}
                        >
                          运行时重载
                        </button>
                        <button type="button" onClick={() => void saveImConfig()} disabled={imSaving}>
                          {imSaving ? "保存中..." : "保存配置"}
                        </button>
                      </div>
                    </div>

                    {channelDrafts.length ? (
                      <div className="channel-stack">
                        {channelDrafts.map((channel, index) => (
                          <article key={`${channel.type}-${index}`} className="channel-card">
                            <div className="channel-card-header">
                              <strong>{channel.type}</strong>
                              <button
                                type="button"
                                className="ghost-button"
                                onClick={() => removeChannel(channel.type)}
                              >
                                移除
                              </button>
                            </div>
                            <div className="form-grid">
                              <label>
                                <span>类型</span>
                                <select
                                  value={channel.type}
                                  onChange={(event) => updateChannelDraft(index, {
                                    type: event.target.value as "slack" | "feishu",
                                  })}
                                >
                                  <option value="slack">slack</option>
                                  <option value="feishu">feishu</option>
                                </select>
                              </label>
                              <label>
                                <span>连接模式</span>
                                <input value={channel.connectionMode} readOnly />
                              </label>
                              <label>
                                <span>回复模式</span>
                                <select
                                  value={channel.replyMode}
                                  onChange={(event) => updateChannelDraft(index, {
                                    replyMode: event.target.value as "complete" | "streaming",
                                  })}
                                >
                                  <option value="complete">complete</option>
                                  <option value="streaming">streaming</option>
                                </select>
                              </label>
                              <label className="wide-field">
                                <span>chat_ids</span>
                                <input
                                  value={channel.chatIdsText}
                                  onChange={(event) => updateChannelDraft(index, { chatIdsText: event.target.value })}
                                  placeholder="逗号分隔，例如 C123,C456"
                                />
                              </label>
                              <label className="wide-field">
                                <span>features JSON</span>
                                <textarea
                                  value={channel.featuresText}
                                  onChange={(event) => updateChannelDraft(index, { featuresText: event.target.value })}
                                />
                              </label>
                            </div>
                          </article>
                        ))}
                      </div>
                    ) : (
                      <p className="placeholder">当前没有启用 IM channel，可手动新增 Slack 或飞书通道。</p>
                    )}

                    <section className="credentials-grid">
                      <article className="credential-card">
                        <h3>Slack 凭据</h3>
                        <label>
                          <span>bot_token</span>
                          <input
                            value={credentialsDraft.slack.bot_token}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              slack: { ...current.slack, bot_token: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.slack?.bot_token || "保留现有值则留空"}
                          />
                        </label>
                        <label>
                          <span>app_token</span>
                          <input
                            value={credentialsDraft.slack.app_token}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              slack: { ...current.slack, app_token: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.slack?.app_token || "保留现有值则留空"}
                          />
                        </label>
                        <label>
                          <span>signing_secret</span>
                          <input
                            value={credentialsDraft.slack.signing_secret}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              slack: { ...current.slack, signing_secret: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.slack?.signing_secret || "保留现有值则留空"}
                          />
                        </label>
                        <label>
                          <span>team_id</span>
                          <input
                            value={credentialsDraft.slack.team_id}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              slack: { ...current.slack, team_id: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.slack?.team_id || "可选"}
                          />
                        </label>
                      </article>

                      <article className="credential-card">
                        <h3>飞书凭据</h3>
                        <label>
                          <span>app_id</span>
                          <input
                            value={credentialsDraft.feishu.app_id}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              feishu: { ...current.feishu, app_id: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.feishu?.app_id || "保留现有值则留空"}
                          />
                        </label>
                        <label>
                          <span>app_secret</span>
                          <input
                            value={credentialsDraft.feishu.app_secret}
                            onChange={(event) => setCredentialsDraft((current) => ({
                              ...current,
                              feishu: { ...current.feishu, app_secret: event.target.value },
                            }))}
                            placeholder={imMaskedCredentials.feishu?.app_secret || "保留现有值则留空"}
                          />
                        </label>
                      </article>
                    </section>

                    {imStatus?.adapters?.length ? (
                      <section className="adapter-status-grid">
                        {imStatus.adapters.map((adapter) => (
                          <article key={`${adapter.type}-${adapter.connection_mode}`} className="summary-card">
                            <h3>{adapter.type}</h3>
                            <dl>
                              <div><dt>registered</dt><dd>{adapter.registered ? "yes" : "no"}</dd></div>
                              <div><dt>healthy</dt><dd>{adapter.healthy ? "yes" : "no"}</dd></div>
                              <div><dt>mode</dt><dd>{adapter.connection_mode || "-"}</dd></div>
                              <div><dt>chat_ids</dt><dd>{adapter.chat_ids.join(", ") || "-"}</dd></div>
                            </dl>
                            {adapter.last_error ? <p className="compact-error">{adapter.last_error}</p> : null}
                          </article>
                        ))}
                      </section>
                    ) : null}

                    {imNotice ? <div className="hint-banner">{imNotice}</div> : null}
                    {imError ? <div className="error-banner">{imError}</div> : null}
                  </>
                ) : null}
              </section>
            ) : null}
          </section>
        </main>
      )}
    </div>
  )
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const contentType = response.headers.get("content-type") || ""
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text()
  if (!response.ok) {
    if (typeof payload === "string" && payload.trim()) {
      throw new Error(payload)
    }
    if (payload && typeof payload === "object" && "detail" in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (Array.isArray(detail)) {
        throw new Error(detail.map(renderValidationDetail).join("; "))
      }
      throw new Error(String(detail))
    }
    throw new Error(`${response.status} ${response.statusText}`)
  }
  return payload as T
}

function renderValidationDetail(detail: unknown): string {
  if (!detail || typeof detail !== "object") {
    return String(detail)
  }
  const record = detail as { loc?: unknown[]; msg?: string }
  return `${(record.loc || []).join(".")}: ${record.msg || "invalid input"}`
}

function createEmptyChannelDraft(type: "slack" | "feishu"): ChannelDraft {
  return {
    type,
    connectionMode: type === "slack" ? "socket_mode" : "websocket",
    chatIdsText: "",
    replyMode: "complete",
    featuresText: "{}",
  }
}

function channelDraftFromApi(channel: IMChannel): ChannelDraft {
  const type = (channel.type || "slack") as "slack" | "feishu"
  return {
    type,
    connectionMode: type === "slack" ? "socket_mode" : "websocket",
    chatIdsText: (channel.chat_ids || []).join(", "),
    replyMode: channel.reply_mode === "streaming" ? "streaming" : "complete",
    featuresText: JSON.stringify(channel.features || {}, null, 2),
  }
}

function buildImPayload(channelDrafts: ChannelDraft[], credentialsDraft: CredentialsDraft) {
  const channels = channelDrafts.map((channel, index) => {
    let features: Record<string, unknown> = {}
    try {
      const parsed = JSON.parse(channel.featuresText || "{}") as unknown
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        features = parsed as Record<string, unknown>
      } else {
        throw new Error("features must be an object")
      }
    } catch (err) {
      throw new Error(`channels.${index}.features: ${String(err)}`)
    }
    return {
      type: channel.type,
      connection_mode: channel.connectionMode,
      chat_ids: channel.chatIdsText
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      reply_mode: channel.replyMode,
      features,
    }
  })

  return {
    persona: { channels },
    credentials: {
      slack: compactObject(credentialsDraft.slack),
      feishu: compactObject(credentialsDraft.feishu),
    },
  }
}

function compactObject<T extends Record<string, string>>(payload: T): Partial<T> {
  const result: Partial<T> = {}
  Object.entries(payload).forEach(([key, value]) => {
    if (String(value || "").trim()) {
      result[key as keyof T] = value
    }
  })
  return result
}
