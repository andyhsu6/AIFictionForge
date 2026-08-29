import {
  CheckOutlined,
  CheckCircleOutlined,
  ApiOutlined,
  BulbOutlined,
  CloseOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  HistoryOutlined,
  LoadingOutlined,
  MenuFoldOutlined,
  PlusOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  StopOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  UserOutlined,
} from '@ant-design/icons';
import {
  Button,
  Collapse,
  Drawer,
  Dropdown,
  Input,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
  message,
  theme,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { projectAgentApi } from '../../services/api';
import { eventBus, EventNames } from '../../store/eventBus';
import type {
  AgentConversation,
  AgentExecutionStep,
  AgentMessage,
  AgentToolCall,
} from '../../types';
import MarkdownRenderer from '../MarkdownRenderer';

const { Text, Title } = Typography;
const { TextArea } = Input;

const EXPANDED_KEY = 'project-agent-expanded';
const WIDTH_KEY = 'project-agent-width';
const AUTO_APPROVE_KEY = 'project-agent-auto-approve';

function AssistantLogo({ size }: { size: number }) {
  return (
    <img
      src="/logo.svg"
      alt="灵创创作助手"
      draggable={false}
      style={{ width: size, height: size, display: 'block', objectFit: 'contain' }}
    />
  );
}

interface ProjectAgentPanelProps {
  projectId: string;
  mobile: boolean;
  mobileOpen: boolean;
  onMobileClose: () => void;
  onExpandedChange?: (expanded: boolean, width: number) => void;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

// tool_calls 是后端 TEXT 列存下的 JSON 字符串（数组或 { tool_calls: [...] } 包裹形式）。
// 解析失败或形状不符时返回 0，保证旧消息（无该字段）不受影响。
function countToolCalls(raw?: string): number {
  if (!raw) return 0;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed.length;
    if (parsed && typeof parsed === 'object') {
      const list = (parsed as Record<string, unknown>).tool_calls;
      if (Array.isArray(list)) return list.length;
    }
  } catch {
    // 忽略解析失败
  }
  return 0;
}

function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    title: '标题', description: '简介', theme: '主题', genre: '类型',
    target_words: '目标字数', status: '状态', content: '内容', summary: '摘要',
    name: '名称', age: '年龄', gender: '性别', role_type: '角色类型',
    personality: '性格', background: '背景', appearance: '外貌', traits: '特征',
    world_time_period: '时代背景', world_location: '地点',
    world_atmosphere: '世界氛围', world_rules: '世界规则',
    chapter_count: '章节数', narrative_perspective: '叙事视角',
    character_count: '角色数',
  };
  return labels[field] || field;
}

function timestampMs(value?: string): number | undefined {
  if (!value) return undefined;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function elapsedSeconds(start?: string, end?: string, fallbackEnd?: number): number {
  const startMs = timestampMs(start);
  const endMs = timestampMs(end) ?? fallbackEnd;
  if (startMs === undefined || endMs === undefined) return 0;
  const rawDuration = endMs - startMs;
  const hasTimezone = Boolean(start && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(start));
  if (hasTimezone) return Math.max(0, Math.floor(rawDuration / 1000));

  // SQLite 的 CURRENT_TIMESTAMP 是 UTC，而部分显式更新时间是本地时间。
  // 对无时区时间戳同时尝试本地偏移，选取合理的较短非负耗时。
  const offsetDuration = rawDuration + new Date().getTimezoneOffset() * 60_000;
  const candidates = [rawDuration, offsetDuration].filter(value => value >= 0);
  const duration = candidates.length ? Math.min(...candidates) : 0;
  return Math.floor(duration / 1000);
}

export default function ProjectAgentPanel({
  projectId,
  mobile,
  mobileOpen,
  onMobileClose,
  onExpandedChange,
}: ProjectAgentPanelProps) {
  const { token } = theme.useToken();
  const location = useLocation();
  const [expanded, setExpanded] = useState(() => localStorage.getItem(EXPANDED_KEY) !== 'false');
  const [panelWidth, setPanelWidth] = useState(() => {
    const value = Number(localStorage.getItem(WIDTH_KEY));
    return Number.isFinite(value) && value >= 340 && value <= 560 ? value : 410;
  });
  const [conversations, setConversations] = useState<AgentConversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string>();
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [toolCalls, setToolCalls] = useState<AgentToolCall[]>([]);
  const [executionSteps, setExecutionSteps] = useState<AgentExecutionStep[]>([]);
  const [expandedProcessIds, setExpandedProcessIds] = useState<Set<string>>(() => new Set());
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [input, setInput] = useState('');
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [sending, setSending] = useState(false);
  const [decidingId, setDecidingId] = useState<string>();
  const [approvingAllMessageId, setApprovingAllMessageId] = useState<string>();
  const [autoApprove, setAutoApprove] = useState(() => localStorage.getItem(AUTO_APPROVE_KEY) === 'true');
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController>();
  const autoApprovalAttemptedRef = useRef<Set<string>>(new Set());

  const notifyLayout = useCallback((nextExpanded: boolean, nextWidth = panelWidth) => {
    onExpandedChange?.(nextExpanded, nextWidth);
  }, [onExpandedChange, panelWidth]);

  const changeExpanded = useCallback((next: boolean) => {
    setExpanded(next);
    localStorage.setItem(EXPANDED_KEY, String(next));
    notifyLayout(next);
  }, [notifyLayout]);

  const notifyToolResources = useCallback((toolCall: AgentToolCall, resources: string[]) => {
    if (!resources.length) return;
    if (resources.includes('tasks')) {
      const taskId = typeof toolCall.result?.entity_id === 'string'
        ? toolCall.result.entity_id
        : undefined;
      eventBus.emit(EventNames.BACKGROUND_TASK_CREATED, {
        projectId,
        taskId,
        resources: resources.filter(resource => resource !== 'tasks'),
      });
    } else {
      eventBus.emit(EventNames.AGENT_DATA_CHANGED, { projectId, resources });
    }
  }, [projectId]);

  const loadConversation = useCallback(async (conversationId: string) => {
    setLoadingHistory(true);
    try {
      const detail = await projectAgentApi.getConversation(projectId, conversationId);
      setActiveConversationId(detail.id);
      setMessages(detail.messages);
      setToolCalls(detail.tool_calls);
      setExecutionSteps(detail.execution_steps || []);
      setExpandedProcessIds(new Set(
        (detail.execution_steps || [])
          .filter(step => step.status === 'waiting_confirmation' && step.assistant_message_id)
          .map(step => step.assistant_message_id as string)
      ));
    } catch (error) {
      console.error('加载灵创创作助手对话失败:', error);
    } finally {
      setLoadingHistory(false);
    }
  }, [projectId]);

  const loadConversations = useCallback(async (selectLatest = false) => {
    try {
      const items = await projectAgentApi.listConversations(projectId);
      setConversations(items);
      if (selectLatest && items[0]) await loadConversation(items[0].id);
    } catch (error) {
      console.error('加载灵创创作助手会话列表失败:', error);
    }
  }, [loadConversation, projectId]);

  useEffect(() => {
    setActiveConversationId(undefined);
    setMessages([]);
    setToolCalls([]);
    setExecutionSteps([]);
    setExpandedProcessIds(new Set());
    autoApprovalAttemptedRef.current.clear();
    void loadConversations(true);
    return () => abortRef.current?.abort();
  }, [loadConversations, projectId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, toolCalls, executionSteps, sending]);

  useEffect(() => {
    if (!mobile) notifyLayout(expanded);
  }, [expanded, mobile, notifyLayout]);

  useEffect(() => {
    const waitingMessageIds = executionSteps
      .filter(step => step.status === 'waiting_confirmation' && step.assistant_message_id)
      .map(step => step.assistant_message_id as string);
    if (!waitingMessageIds.length) return;
    setExpandedProcessIds(current => {
      if (waitingMessageIds.every(id => current.has(id))) return current;
      const next = new Set(current);
      waitingMessageIds.forEach(id => next.add(id));
      return next;
    });
  }, [executionSteps]);

  useEffect(() => {
    if (!executionSteps.some(step => step.status === 'running')) return;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [executionSteps]);

  const newConversation = async () => {
    try {
      const conversation = await projectAgentApi.createConversation(projectId);
      setConversations(items => [conversation, ...items]);
      setActiveConversationId(conversation.id);
      setMessages([]);
      setToolCalls([]);
      setExecutionSteps([]);
      setExpandedProcessIds(new Set());
    } catch (error) {
      console.error('新建灵创创作助手对话失败:', error);
    }
  };

  const removeConversation = async () => {
    if (!activeConversationId) return;
    try {
      await projectAgentApi.deleteConversation(projectId, activeConversationId);
      setActiveConversationId(undefined);
      setMessages([]);
      setToolCalls([]);
      setExecutionSteps([]);
      setExpandedProcessIds(new Set());
      await loadConversations(true);
    } catch (error) {
      console.error('删除灵创创作助手对话失败:', error);
    }
  };

  const send = async () => {
    const content = input.trim();
    if (!content || sending) return;
    setInput('');
    setSending(true);
    const now = new Date().toISOString();
    const userId = `local-user-${Date.now()}`;
    const assistantId = `local-assistant-${Date.now()}`;
    setMessages(items => [
      ...items,
      { id: userId, conversation_id: activeConversationId || '', role: 'user', content, created_at: now },
      { id: assistantId, conversation_id: activeConversationId || '', role: 'assistant', content: '', created_at: now },
    ]);
    let streamConversationId = activeConversationId;
    let streamAssistantId = assistantId;
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await projectAgentApi.chatStream(projectId, {
        conversation_id: activeConversationId,
        message: content,
        page_context: { route: location.pathname, page: location.pathname.split('/').pop() },
        auto_approve: autoApprove,
      }, {
        onConversation: data => {
          streamConversationId = data.conversation_id;
          setActiveConversationId(data.conversation_id);
        },
        onStepStart: step => setExecutionSteps(items => [
          ...items.filter(item => item.id !== step.id),
          { ...step, assistant_message_id: step.assistant_message_id || assistantId },
        ]),
        onStepUpdate: step => {
          setExecutionSteps(items => items.map(item => item.id === step.id
            ? { ...step, assistant_message_id: step.assistant_message_id || assistantId }
            : item));
          const toolCall = step.detail?.tool_call as AgentToolCall | undefined;
          if (toolCall) {
            setToolCalls(items => [...items.filter(item => item.id !== toolCall.id), toolCall]);
          }
        },
        onFinalStart: data => {
          streamAssistantId = data.message_id;
          setMessages(items => items.map(item => (
            item.id === assistantId ? { ...item, id: data.message_id } : item
          )));
          setExecutionSteps(items => items.map(step => (
            step.assistant_message_id === assistantId
              ? { ...step, assistant_message_id: data.message_id }
              : step
          )));
          setExpandedProcessIds(current => {
            if (!current.has(assistantId)) return current;
            const next = new Set(current);
            next.delete(assistantId);
            next.add(data.message_id);
            return next;
          });
        },
        onFinalChunk: chunk => setMessages(items => items.map(item => (
          item.id === streamAssistantId || item.id === assistantId
            ? { ...item, content: item.content + chunk }
            : item
        ))),
        onChunk: chunk => setMessages(items => items.map(item => (
          item.id === streamAssistantId || item.id === assistantId
            ? { ...item, content: item.content + chunk }
            : item
        ))),
        onToolProposed: toolCall => setToolCalls(items => [...items, toolCall]),
        onToolExecuted: data => {
          setToolCalls(items => [
            ...items.filter(item => item.id !== data.tool_call.id),
            data.tool_call,
          ]);
          notifyToolResources(data.tool_call, data.resources || []);
        },
        onError: error => message.error(error),
      }, controller.signal);
      await loadConversations();
      if (streamConversationId) await loadConversation(streamConversationId);
    } catch (error) {
      const aborted = (error as Error).name === 'AbortError';
      setExecutionSteps(items => items.map(step => (
        step.assistant_message_id === assistantId && step.status === 'running'
          ? {
              ...step,
              status: aborted ? 'cancelled' : 'failed',
              content: aborted ? '本次执行已由用户停止。' : '本次执行因请求失败而中止。',
              updated_at: new Date().toISOString(),
            }
          : step
      )));
      if (aborted) {
        setMessages(items => items.map(item => (
          (item.id === streamAssistantId || item.id === assistantId) && !item.content
            ? { ...item, content: '已停止生成。' }
            : item
        )));
      } else {
        const detail = (error as Error).message || '灵创创作助手请求失败';
        message.error(detail);
        setMessages(items => items.map(item => (
          (item.id === streamAssistantId || item.id === assistantId) && !item.content
            ? { ...item, content: `请求失败：${detail}` }
            : item
        )));
      }
      if (streamConversationId) {
        try {
          // 等待服务端完成中断收尾，再以持久化消息和步骤替换本地占位内容。
          await new Promise(resolve => window.setTimeout(resolve, 100));
          await loadConversations();
          await loadConversation(streamConversationId);
        } catch (reloadError) {
          console.error('重新加载中断对话失败:', reloadError);
        }
      }
    } finally {
      setSending(false);
      abortRef.current = undefined;
    }
  };

  const decideTool = async (toolCall: AgentToolCall, confirm: boolean) => {
    setDecidingId(toolCall.id);
    try {
      const result = confirm
        ? await projectAgentApi.confirmToolCall(projectId, toolCall.id)
        : await projectAgentApi.rejectToolCall(projectId, toolCall.id);
      message.success(result.message);
      notifyToolResources(result.tool_call, result.resources);
      if (activeConversationId) await loadConversation(activeConversationId);
      await loadConversations();
    } catch (error) {
      message.error((error as Error).message || '处理修改失败');
      console.error('处理灵创创作助手修改失败:', error);
      if (activeConversationId) await loadConversation(activeConversationId);
    } finally {
      setDecidingId(undefined);
    }
  };

  const approveAllTools = useCallback(async (toolCallsToApprove: AgentToolCall[], messageId: string) => {
    if (!toolCallsToApprove.length || decidingId || approvingAllMessageId) return;
    setApprovingAllMessageId(messageId);
    let completed = 0;
    const failures: string[] = [];
    try {
      for (const toolCall of toolCallsToApprove) {
        try {
          const result = await projectAgentApi.confirmToolCall(projectId, toolCall.id);
          notifyToolResources(result.tool_call, result.resources);
          completed += 1;
        } catch (error) {
          failures.push((error as Error).message || toolCall.tool_name);
        }
      }
      if (activeConversationId) await loadConversation(activeConversationId);
      await loadConversations();
      if (completed && !failures.length) {
        message.success(`已批准并执行 ${completed} 项修改`);
      } else if (completed) {
        message.warning(`${completed} 项修改已执行，${failures.length} 项未执行`);
      } else if (failures.length) {
        message.error('没有修改被执行，请检查预览后重试');
      }
    } finally {
      setApprovingAllMessageId(undefined);
    }
  }, [activeConversationId, approvingAllMessageId, decidingId, loadConversation, loadConversations, notifyToolResources, projectId]);

  const toggleAutoApprove = (enabled: boolean) => {
    setAutoApprove(enabled);
    localStorage.setItem(AUTO_APPROVE_KEY, String(enabled));
    message.info(enabled ? '已开启自动批准修改' : '已切换为手动批准修改');
  };

  // 开启自动批准后，处理已经在历史对话中等待确认的修改。
  useEffect(() => {
    if (!autoApprove || sending || decidingId || approvingAllMessageId) return;
    const waiting = toolCalls.filter(toolCall => (
      toolCall.status === 'waiting_confirmation' && !autoApprovalAttemptedRef.current.has(toolCall.id)
    ));
    if (!waiting.length) return;
    waiting.forEach(toolCall => autoApprovalAttemptedRef.current.add(toolCall.id));
    void approveAllTools(waiting, 'auto-approve');
  }, [approveAllTools, autoApprove, approvingAllMessageId, decidingId, sending, toolCalls]);

  const startResize = (event: React.MouseEvent) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const move = (moveEvent: MouseEvent) => {
      const next = Math.min(560, Math.max(340, startWidth + startX - moveEvent.clientX));
      setPanelWidth(next);
      notifyLayout(true, next);
    };
    const stop = (upEvent: MouseEvent) => {
      const next = Math.min(560, Math.max(340, startWidth + startX - upEvent.clientX));
      setPanelWidth(next);
      localStorage.setItem(WIDTH_KEY, String(next));
      notifyLayout(true, next);
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', stop);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', stop);
  };

  const conversationMenu = useMemo(() => ({
    items: conversations.length
      ? conversations.map(item => ({ key: item.id, label: item.title }))
      : [{ key: 'empty', label: '暂无历史对话', disabled: true }],
    onClick: ({ key }: { key: string }) => key !== 'empty' && void loadConversation(key),
  }), [conversations, loadConversation]);

  const renderStepIcon = (step: AgentExecutionStep) => {
    if (step.status === 'running') return <LoadingOutlined spin style={{ color: token.colorPrimary }} />;
    if (step.status === 'failed') return <CloseCircleOutlined style={{ color: token.colorError }} />;
    if (step.status === 'cancelled') return <StopOutlined style={{ color: token.colorTextSecondary }} />;
    if (step.status === 'rejected') return <CloseCircleOutlined style={{ color: token.colorTextSecondary }} />;
    if (step.status === 'waiting_confirmation') return <ToolOutlined style={{ color: token.colorWarning }} />;
    if (step.category === 'analysis') return <BulbOutlined style={{ color: token.colorPrimary }} />;
    if (step.category === 'mcp') return <ApiOutlined style={{ color: token.colorInfo }} />;
    if (step.category === 'skill') return <ThunderboltOutlined style={{ color: token.colorWarning }} />;
    return <CheckCircleOutlined style={{ color: token.colorSuccess }} />;
  };

  const categoryLabel = (category: string) => ({
    analysis: '思考摘要',
    project: '项目工具',
    mcp: 'MCP',
    skill: 'Skill',
  }[category] || '工具');

  const renderChangePreview = (toolCall?: AgentToolCall) => {
    if (!toolCall?.preview) return null;
    return (
      <div style={{ marginTop: 10 }}>
        <Text strong style={{ fontSize: 12 }}>{toolCall.preview.label}</Text>
        {Object.entries(toolCall.preview.changes || {}).map(([field, change]) => (
          <div key={field} style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>{fieldLabel(field)}</Text>
            <div style={{
              marginTop: 4, padding: 8, borderRadius: 6, fontSize: 12,
              background: token.colorErrorBg, textDecoration: 'line-through',
              whiteSpace: 'pre-wrap', maxHeight: 120, overflow: 'auto',
            }}>{formatValue(change.before)}</div>
            <div style={{
              marginTop: 4, padding: 8, borderRadius: 6, fontSize: 12,
              background: token.colorSuccessBg, whiteSpace: 'pre-wrap',
              maxHeight: 160, overflow: 'auto',
            }}>{formatValue(change.after)}</div>
          </div>
        ))}
        {toolCall.status === 'waiting_confirmation' && (
          <Space style={{ marginTop: 12 }}>
            <Button
              type="primary" size="small" icon={<CheckOutlined />}
              loading={decidingId === toolCall.id}
              disabled={Boolean(approvingAllMessageId)}
              onClick={() => void decideTool(toolCall, true)}
            >确认修改</Button>
            <Button
              size="small" icon={<CloseOutlined />} disabled={Boolean(decidingId || approvingAllMessageId)}
              onClick={() => void decideTool(toolCall, false)}
            >取消</Button>
          </Space>
        )}
      </div>
    );
  };

  const renderToolMessage = (item: AgentMessage) => {
    let toolName = '工具';
    let resultText = item.content;
    try {
      const parsed = JSON.parse(item.content) as { tool?: unknown; error?: unknown; result?: unknown };
      if (parsed && typeof parsed === 'object') {
        if (typeof parsed.tool === 'string' && parsed.tool) toolName = parsed.tool;
        if (parsed.error) {
          resultText = `错误：${typeof parsed.error === 'string' ? parsed.error : JSON.stringify(parsed.error)}`;
        } else {
          resultText = formatValue(parsed.result);
        }
      }
    } catch {
      // content 不是合法 JSON 时回退为原始文本
    }
    return (
      <div key={item.id} style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <div style={{
            width: 24, height: 24, borderRadius: '50%', flexShrink: 0,
            display: 'grid', placeItems: 'center',
            background: token.colorBgContainer,
            border: `1px solid ${token.colorBorderSecondary}`,
          }}>
            <AssistantLogo size={20} />
          </div>
          <div style={{ maxWidth: 'calc(100% - 42px)', minWidth: 0, flex: 1 }}>
            <details style={{ fontSize: 12 }}>
              <summary style={{ cursor: 'pointer', color: token.colorTextSecondary, userSelect: 'none' }}>
                ⚙️ {toolName} 调用
              </summary>
              <pre style={{
                margin: '6px 0 0', padding: 8, borderRadius: 6,
                background: token.colorFillQuaternary, whiteSpace: 'pre-wrap',
                wordBreak: 'break-word', maxHeight: 220, overflow: 'auto',
                color: token.colorTextSecondary,
              }}>{resultText}</pre>
            </details>
          </div>
        </div>
      </div>
    );
  };

  const renderProcess = (steps: AgentExecutionStep[], messageId: string) => {
    if (!steps.length) return null;
    const ordered = [...steps].sort((a, b) => a.sequence - b.sequence);
    const running = ordered.some(step => step.status === 'running');
    const waiting = ordered.some(step => step.status === 'waiting_confirmation');
    const failed = ordered.some(step => step.status === 'failed');
    const cancelled = ordered.some(step => step.status === 'cancelled');
    const firstStep = ordered.reduce<AgentExecutionStep | undefined>((earliest, step) => {
      const value = timestampMs(step.created_at);
      const earliestValue = timestampMs(earliest?.created_at);
      return value !== undefined && (earliestValue === undefined || value < earliestValue) ? step : earliest;
    }, undefined);
    const lastStep = ordered.reduce<AgentExecutionStep | undefined>((latest, step) => {
      const value = timestampMs(step.updated_at) ?? timestampMs(step.created_at);
      const latestValue = timestampMs(latest?.updated_at) ?? timestampMs(latest?.created_at);
      return value !== undefined && (latestValue === undefined || value > latestValue) ? step : latest;
    }, undefined);
    const processElapsed = elapsedSeconds(
      firstStep?.created_at,
      running ? undefined : (lastStep?.updated_at || lastStep?.created_at),
      nowMs
    );
    const processKey = `process-${messageId}`;
    const waitingToolCalls = toolCalls.filter(toolCall => (
      toolCall.status === 'waiting_confirmation'
      && ordered.some(step => step.tool_call_id === toolCall.id)
    ));
    const statusTag = running
      ? <Tag icon={<LoadingOutlined spin />} color="processing">执行中</Tag>
      : waiting
        ? <Tag color="orange">等待确认</Tag>
        : failed
          ? <Tag color="error">部分失败</Tag>
          : cancelled
            ? <Tag>已停止</Tag>
          : <Tag color="success">已完成</Tag>;

    return (
      <div style={{ width: '100%', marginBottom: 10 }}>
        <Collapse
          size="small"
          bordered={false}
          activeKey={expandedProcessIds.has(messageId) ? [processKey] : []}
          onChange={keys => {
            const open = (Array.isArray(keys) ? keys : [keys]).includes(processKey);
            setExpandedProcessIds(current => {
              const next = new Set(current);
              if (open) next.add(messageId);
              else next.delete(messageId);
              return next;
            });
          }}
          items={[{
            key: processKey,
            label: (
              <Space size={6} wrap>
                <BulbOutlined />
                <Text style={{ fontSize: 12 }}>思考与调用过程</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>{ordered.length} 步</Text>
                <Text type="secondary" style={{ fontSize: 11 }}>{processElapsed}S</Text>
                {statusTag}
              </Space>
            ),
            children: (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {waitingToolCalls.length > 1 && !autoApprove && (
                  <Button
                    type="primary"
                    size="small"
                    icon={<CheckOutlined />}
                    loading={approvingAllMessageId === messageId}
                    disabled={Boolean(decidingId || approvingAllMessageId)}
                    onClick={() => void approveAllTools(waitingToolCalls, messageId)}
                  >
                    一键批准全部修改（{waitingToolCalls.length} 项）
                  </Button>
                )}
                {ordered.map(step => {
                  const toolCall = step.tool_call_id
                    ? toolCalls.find(item => item.id === step.tool_call_id)
                    : undefined;
                  const hasDetail = Boolean(step.detail && Object.keys(step.detail).some(key => key !== 'tool_call'));
                  return (
                    <div key={step.id} style={{
                      borderLeft: `2px solid ${step.status === 'failed' ? token.colorError : step.status === 'waiting_confirmation' ? token.colorWarning : token.colorBorder}`,
                      paddingLeft: 10,
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        {renderStepIcon(step)}
                        <Tag style={{ margin: 0, fontSize: 10 }}>{categoryLabel(step.category)}</Tag>
                        <Text strong style={{ fontSize: 12, flex: 1 }}>{step.title}</Text>
                        <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                          {elapsedSeconds(step.created_at, step.status === 'running' ? undefined : step.updated_at, nowMs)}S
                        </Text>
                      </div>
                      {step.content && (
                        <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 5 }}>
                          {step.content}
                        </Text>
                      )}
                      {hasDetail && (
                        <details style={{ marginTop: 7, fontSize: 12 }}>
                          <summary style={{ cursor: 'pointer', color: token.colorTextSecondary }}>查看参数与结果</summary>
                          <pre style={{
                            margin: '6px 0 0', padding: 8, borderRadius: 6,
                            background: token.colorFillQuaternary, whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word', maxHeight: 220, overflow: 'auto',
                          }}>{formatValue(Object.fromEntries(
                            Object.entries(step.detail || {}).filter(([key]) => key !== 'tool_call')
                          ))}</pre>
                        </details>
                      )}
                      {renderChangePreview(toolCall)}
                    </div>
                  );
                })}
              </div>
            ),
          }]}
        />
      </div>
    );
  };

  const body = (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <div style={{
        height: 52, padding: '0 12px', borderBottom: `1px solid ${token.colorBorderSecondary}`,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
      }}>
        <Space size={8}>
          <AssistantLogo size={28} />
          <Title level={5} style={{ margin: 0 }}>灵创创作助手</Title>
        </Space>
        <Space size={2}>
          <Dropdown menu={conversationMenu} trigger={['click']}>
            <Tooltip title="历史对话"><Button type="text" icon={<HistoryOutlined />} /></Tooltip>
          </Dropdown>
          <Tooltip title="新对话"><Button type="text" icon={<PlusOutlined />} onClick={() => void newConversation()} /></Tooltip>
          {activeConversationId && (
            <Popconfirm title="删除当前对话？" onConfirm={() => void removeConversation()}>
              <Tooltip title="删除对话"><Button type="text" danger icon={<DeleteOutlined />} /></Tooltip>
            </Popconfirm>
          )}
          {mobile ? (
            <Button type="text" icon={<CloseOutlined />} onClick={onMobileClose} />
          ) : (
            <Tooltip title="收起"><Button type="text" icon={<MenuFoldOutlined />} onClick={() => changeExpanded(false)} /></Tooltip>
          )}
        </Space>
      </div>

      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 12 }}>
        {loadingHistory ? (
          <div style={{ height: '100%', display: 'grid', placeItems: 'center' }}><Spin /></div>
        ) : messages.length === 0 ? (
          <div style={{
            minHeight: '100%', display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: 24,
          }}>
            <AssistantLogo size={72} />
            <div style={{ marginTop: 14 }}>
              <Text strong>可以查询和修改当前项目</Text><br />
              <Text type="secondary" style={{ fontSize: 12 }}>例如：“把第三条大纲标题改得更有悬念”</Text>
            </div>
          </div>
        ) : messages.filter(item => item.role === 'user' || item.role === 'assistant' || item.role === 'tool').map((item, index, visibleMessages) => {
          if (item.role === 'tool') return renderToolMessage(item);
          const previousUserMessage = item.role === 'assistant'
            ? visibleMessages.slice(0, index).reverse().find(messageItem => messageItem.role === 'user')
            : undefined;
          const relatedSteps = item.role === 'assistant'
            ? executionSteps.filter(step => (
              step.assistant_message_id === item.id
              || (!step.assistant_message_id && step.user_message_id === previousUserMessage?.id)
            ))
            : [];
          const toolCallCount = item.role === 'assistant' ? countToolCalls(item.tool_calls) : 0;
          return (
            <div key={item.id} style={{ marginBottom: 14 }}>
              <div style={{
                display: 'flex', gap: 8,
                flexDirection: item.role === 'user' ? 'row-reverse' : 'row',
              }}>
                <div style={{
                  width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
                  display: 'grid', placeItems: 'center',
                  background: item.role === 'user' ? token.colorPrimary : token.colorBgContainer,
                  color: item.role === 'user' ? token.colorWhite : token.colorText,
                  border: item.role === 'assistant' ? `1px solid ${token.colorBorderSecondary}` : undefined,
                }}>
                  {item.role === 'user'
                    ? <UserOutlined />
                    : <AssistantLogo size={24} />}
                </div>
                <div style={{ maxWidth: 'calc(100% - 42px)', flex: item.role === 'assistant' ? 1 : undefined, minWidth: 0 }}>
                  {item.role === 'assistant' && renderProcess(relatedSteps, item.id)}
                  <div style={{
                    padding: '8px 11px', borderRadius: 10,
                    background: item.role === 'user' ? token.colorPrimary : token.colorFillQuaternary,
                    color: item.role === 'user' ? token.colorWhite : token.colorText,
                    whiteSpace: item.role === 'user' ? 'pre-wrap' : undefined,
                  }}>
                    {item.role === 'assistant'
                      ? (item.content
                          ? <MarkdownRenderer content={item.content} compact />
                          : (toolCallCount > 0
                              ? <Text type="secondary" style={{ fontSize: 12 }}>🔧 准备调用 {toolCallCount} 个工具...</Text>
                              : <Spin size="small" />))
                      : item.content}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>

      <div style={{ padding: 10, borderTop: `1px solid ${token.colorBorderSecondary}`, flexShrink: 0 }}>
        <TextArea
          value={input}
          onChange={event => setInput(event.target.value)}
          onPressEnter={event => {
            if (!event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
          placeholder="询问或修改当前项目……"
          autoSize={{ minRows: 2, maxRows: 5 }}
          disabled={sending}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8 }}>
        <Space size={6}>
          <SafetyCertificateOutlined style={{ color: autoApprove ? token.colorSuccess : token.colorTextSecondary }} />
          <Text type="secondary" style={{ fontSize: 11 }}>
            {autoApprove ? '自动批准修改' : '手动批准修改'}
          </Text>
          <Switch size="small" checked={autoApprove} onChange={toggleAutoApprove} />
        </Space>
          {sending ? (
            <Button size="small" icon={<StopOutlined />} onClick={() => abortRef.current?.abort()}>停止</Button>
          ) : (
            <Button type="primary" size="small" icon={<SendOutlined />} disabled={!input.trim()} onClick={() => void send()}>发送</Button>
          )}
        </div>
      </div>
    </div>
  );

  if (mobile) {
    return (
      <Drawer
        placement="right"
        open={mobileOpen}
        onClose={onMobileClose}
        width="92vw"
        closable={false}
        styles={{ body: { padding: 0, height: '100%' } }}
      >
        {body}
      </Drawer>
    );
  }

  if (!expanded) {
    return (
      <div style={{
        width: 48, flex: '0 0 48px', borderLeft: `1px solid ${token.colorBorderSecondary}`,
        display: 'flex', justifyContent: 'center', paddingTop: 10, background: token.colorBgContainer,
      }}>
        <Tooltip title="展开灵创创作助手" placement="left">
          <Button type="text" icon={<AssistantLogo size={20} />} onClick={() => changeExpanded(true)} />
        </Tooltip>
      </div>
    );
  }

  return (
    <div style={{
      width: panelWidth, flex: `0 0 ${panelWidth}px`, height: '100%', position: 'relative',
      borderLeft: `1px solid ${token.colorBorderSecondary}`, background: token.colorBgContainer,
    }}>
      <div
        onMouseDown={startResize}
        style={{ position: 'absolute', left: -4, top: 0, bottom: 0, width: 8, cursor: 'col-resize', zIndex: 2 }}
      />
      {body}
    </div>
  );
}
