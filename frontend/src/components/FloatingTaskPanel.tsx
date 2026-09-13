import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Card, List, Button, Space, Badge, Tag, Progress, Popconfirm, Empty, theme, Tooltip } from 'antd';
import {
  ClockCircleOutlined,
  LoadingOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ReloadOutlined,
  DeleteOutlined,
  UpOutlined,
  DownOutlined,
  ClearOutlined,
} from '@ant-design/icons';
import { getProjectTasks, getTaskStatus, cancelTask, cancelBatchTask, deleteTask, clearProjectTasks, type TaskStatus } from '../services/backgroundTaskService';
import { mapTaskStatusMessage, getErrorDiagnostic, isModelGateError } from '../services/errorMapper';
import { eventBus, EventNames } from '../store/eventBus';

interface FloatingTaskPanelProps {
  projectId: string;
  autoRefreshInterval?: number; // 自动刷新间隔（毫秒），默认3000
  rightOffset?: number;
}

/**
 * 悬浮任务框组件
 * 显示在页面右下角，支持收起/展开
 */
export const FloatingTaskPanel: React.FC<FloatingTaskPanelProps> = ({
  projectId,
  autoRefreshInterval = 3000,
  rightOffset = 23,
}) => {
  const { message } = App.useApp();
  const { t } = useTranslation('floatingTaskPanel');
  const [taskList, setTaskList] = useState<TaskStatus[]>([]);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState(true); // 默认收起
  const userCollapsedRef = useRef(false); // 用户手动收起标记
  const taskStatusRef = useRef<Map<string, TaskStatus['status']>>(new Map());
  const watchedTaskIdsRef = useRef<Set<string>>(new Set());
  const loadRequestIdRef = useRef(0);
  const { token } = theme.useToken();

  // 加载任务列表
  const loadTasks = useCallback(async () => {
    if (!projectId) return;
    const requestId = ++loadRequestIdRef.current;
    setLoading(true);
    try {
      const result = await getProjectTasks(projectId);
      if (requestId !== loadRequestIdRef.current) return;
      const nextTasks = result.items || [];
      const visibleTaskIds = new Set(nextTasks.map(task => task.id));
      // 列表有数量上限。对智能体刚创建但未出现在列表中的任务按 ID 查询，
      // 避免大量活动任务把它挤出后无法感知完成状态。
      const missingWatchedTasks = await Promise.all(
        [...watchedTaskIdsRef.current]
          .filter(taskId => !visibleTaskIds.has(taskId))
          .map(taskId => getTaskStatus(taskId).catch(() => null)),
      );
      if (requestId !== loadRequestIdRef.current) return;

      const observedTasks = [
        ...nextTasks,
        ...missingWatchedTasks.filter((task): task is TaskStatus => task !== null),
      ];
      observedTasks.forEach(task => {
        const previousStatus = taskStatusRef.current.get(task.id);
        const wasActive = previousStatus === 'running' || previousStatus === 'pending';
        const isSettled = task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled';
        const isWatchedTask = watchedTaskIdsRef.current.has(task.id);
        if (isSettled && (wasActive || isWatchedTask)) {
          eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
            projectId: task.project_id,
            taskId: task.id,
            resources: task.affected_resources || [],
            task,
          });
          watchedTaskIdsRef.current.delete(task.id);
        }
      });
      taskStatusRef.current = new Map(observedTasks.map(task => [task.id, task.status]));
      setTaskList(nextTasks);
    } catch (error) {
      console.error('加载任务列表失败:', error);
    } finally {
      if (requestId === loadRequestIdRef.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    taskStatusRef.current.clear();
    watchedTaskIdsRef.current.clear();
    loadRequestIdRef.current += 1;
  }, [projectId]);

  // 初始加载
  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  // 监听后台任务创建事件，立即刷新列表并展开浮窗
  useEffect(() => {
    const handleTaskCreated = (payload?: unknown) => {
      if (payload && typeof payload === 'object') {
        const eventPayload = payload as { projectId?: unknown; taskId?: unknown };
        if (typeof eventPayload.projectId === 'string' && eventPayload.projectId !== projectId) return;
        const taskId = eventPayload.taskId;
        if (typeof taskId === 'string') watchedTaskIdsRef.current.add(taskId);
      }
      void loadTasks();
      // 创建新任务时自动展开（重置用户手动收起标记）
      userCollapsedRef.current = false;
      setCollapsed(false);
    };
    eventBus.on(EventNames.BACKGROUND_TASK_CREATED, handleTaskCreated);
    // 兼容尚未迁移的页面内任务创建通知。
    eventBus.on('background-task-created', handleTaskCreated);
    return () => {
      eventBus.off(EventNames.BACKGROUND_TASK_CREATED, handleTaskCreated);
      eventBus.off('background-task-created', handleTaskCreated);
    };
  }, [loadTasks, projectId]);

  // 有活跃任务时自动展开（仅当用户没有手动收起时）
  useEffect(() => {
    const hasActiveTasks = taskList.some(
      (t) => t.status === 'running' || t.status === 'pending'
    );
    if (hasActiveTasks && !userCollapsedRef.current) {
      setCollapsed(false);
    }
  }, [taskList]);

  // 活跃任务高频刷新；空闲时保留低频刷新，以发现跨标签页或外部创建的任务。
  useEffect(() => {
    const hasActiveTasks = taskList.some(
      (t) => t.status === 'running' || t.status === 'pending'
    );
    
    const interval = hasActiveTasks ? autoRefreshInterval : Math.max(autoRefreshInterval * 5, 15000);
    const timer = setInterval(loadTasks, interval);
    return () => clearInterval(timer);
  }, [taskList, autoRefreshInterval, loadTasks]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void loadTasks();
    };
    window.addEventListener('focus', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      window.removeEventListener('focus', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [loadTasks]);

  // 取消任务
  const handleCancelTask = async (task: TaskStatus) => {
    try {
      if (task.task_type === 'chapter_batch') {
        await cancelBatchTask(task.id);
      } else {
        await cancelTask(task.id);
      }
      loadTasks();
    } catch (error) {
      console.error('取消任务失败:', error);
    }
  };

  // 删除任务记录
  const handleDeleteTask = async (taskId: string) => {
    try {
      await deleteTask(taskId);
      loadTasks();
    } catch (error) {
      console.error('删除任务记录失败:', error);
    }
  };

  // 一键清理已结束的任务记录
  const handleClearTasks = async () => {
    try {
      const result = await clearProjectTasks(projectId);
      message.success(t('clearedTasks', { count: result.deleted_count }));
      loadTasks();
    } catch (error) {
      console.error('清理任务记录失败:', error);
      message.error(t('clearFailed'));
    }
  };

  // 获取任务状态标签
  const getTaskStatusTag = (status: TaskStatus['status']) => {
    switch (status) {
      case 'pending':
        return <Tag icon={<ClockCircleOutlined />} color="default">{t('statusPending')}</Tag>;
      case 'running':
        return <Tag icon={<LoadingOutlined />} color="processing">{t('statusRunning')}</Tag>;
      case 'completed':
        return <Tag icon={<CheckCircleOutlined />} color="success">{t('statusCompleted')}</Tag>;
      case 'failed':
        return <Tag icon={<CloseCircleOutlined />} color="error">{t('statusFailed')}</Tag>;
      case 'cancelled':
        return <Tag icon={<CloseCircleOutlined />} color="default">{t('statusCancelled')}</Tag>;
      default:
        return <Tag>{status}</Tag>;
    }
  };

  // 获取任务类型标签
  const getTaskTypeLabel = (taskType: string) => {
    switch (taskType) {
      case 'outline_new':
        return t('typeOutlineNew');
      case 'outline_continue':
        return t('typeOutlineContinue');
      case 'outline_expand':
        return t('typeOutlineExpand');
      case 'outline_batch_expand':
        return t('typeOutlineBatchExpand');
      case 'chapter_generate':
        return t('typeChapterGenerate');
      case 'chapter_batch':
        return t('typeChapterBatch');
      case 'wizard':
        return t('typeWizard');
      case 'chapter_analysis':
        return t('typeChapterAnalysis');
      case 'chapter_regenerate':
        return t('typeChapterRegenerate');
      case 'chapter_partial_regenerate':
        return t('typeChapterPartialRegenerate');
      case 'character_generate':
        return t('typeCharacterGenerate');
      case 'organization_generate':
        return t('typeOrganizationGenerate');
      case 'career_generate':
        return t('typeCareerGenerate');
      default:
        return taskType;
    }
  };

  const activeTasks = taskList.filter((t) => t.status === 'running' || t.status === 'pending');
  const hasActiveTasks = activeTasks.length > 0;

  // 没有任务时不显示浮窗
  if (taskList.length === 0) return null;

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 10,
        right: rightOffset,
        width: collapsed ? 260 : 400,
        maxHeight: collapsed ? 60 : 500,
        zIndex: 1000,
        boxShadow: token.boxShadowSecondary,
        borderRadius: token.borderRadiusLG,
        overflow: 'hidden',
        transition: 'all 0.3s ease',
      }}
    >
      <Card
        size="small"
        title={
          <Space>
            <ClockCircleOutlined />
            <span>{t('title')}</span>
            {hasActiveTasks && <Badge count={activeTasks.length} />}
          </Space>
        }
        extra={
          <Space>
            <Tooltip title={t('refresh')}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={loadTasks}
                loading={loading}
              />
            </Tooltip>
            {taskList.some(t => t.can_delete && (
              t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled'
            )) && (
              <Popconfirm
                title={t('clearConfirm')}
                onConfirm={handleClearTasks}
                okText={t('confirm')}
                cancelText={t('cancel')}
              >
                <Tooltip title={t('clearTooltip')}>
                  <Button
                    type="text"
                    size="small"
                    icon={<ClearOutlined />}
                  />
                </Tooltip>
              </Popconfirm>
            )}
            <Button
              type="text"
              size="small"
              icon={collapsed ? <UpOutlined /> : <DownOutlined />}
              onClick={() => {
                const newCollapsed = !collapsed;
                setCollapsed(newCollapsed);
                // 记录用户手动收起，防止自动展开覆盖
                userCollapsedRef.current = newCollapsed;
              }}
            />
          </Space>
        }
        styles={{
          body: {
            padding: collapsed ? 0 : 12,
            maxHeight: collapsed ? 0 : 400,
            overflowY: 'auto',
            transition: 'all 0.3s ease',
          },
        }}
      >
        {!collapsed && (
          <>
            {taskList.length === 0 ? (
              <Empty description={t('empty')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <List
                size="small"
                dataSource={taskList}
                renderItem={(task: TaskStatus) => (
                  <List.Item
                    key={task.id}
                    style={{
                      padding: '8px 0',
                      borderBottom: `1px solid ${token.colorBorderSecondary}`,
                    }}
                  >
                    <div style={{ width: '100%' }}>
                      <div style={{ marginBottom: 4 }}>
                        <Space size={4} wrap>
                          {getTaskStatusTag(task.status)}
                          <Tag color="blue">{getTaskTypeLabel(task.task_type)}</Tag>
                        </Space>
                      </div>

                      {task.status_message && (() => {
                        // Task 14a: show the localized status; the raw backend
                        // status_message is a diagnostic, surfaced only in DEV.
                        const display = mapTaskStatusMessage(task);
                        const diagnostic = getErrorDiagnostic({ detail: task.status_message });
                        return (
                          <div
                            style={{
                              fontSize: 12,
                              color: token.colorTextSecondary,
                              marginBottom: 4,
                            }}
                          >
                            {display}
                            {import.meta.env.DEV && diagnostic && diagnostic !== display && (
                              <span style={{ color: token.colorTextTertiary }}> · {diagnostic}</span>
                            )}
                          </div>
                        );
                      })()}

                      {(task.status === 'running' || task.status === 'pending') && (
                        <Progress
                          percent={task.progress}
                          size="small"
                          status={task.status === 'running' ? 'active' : 'normal'}
                          style={{ marginBottom: 4 }}
                        />
                      )}

                      {task.status === 'failed' && isModelGateError(task.status_code) && (
                        // #55 3b: a background task cannot pop the gate form, so the
                        // row carries the route to where the model is fixed. The
                        // localized status text above comes from status_code alone.
                        <div style={{ marginBottom: 4 }}>
                          <Button
                            type="link"
                            size="small"
                            style={{ padding: 0, height: 'auto', fontSize: 12 }}
                            href="/settings"
                          >
                            {t('gateGoToSettings')}
                          </Button>
                        </div>
                      )}

                      {task.error_message && import.meta.env.DEV && (
                        // Task 14a: error_message is the raw backend
                        // diagnostic (interpolated via {{message}});
                        // DEV-only, same gate as the status_message
                        // diagnostic above. The localized status message
                        // already covers the production display.
                        <div
                          style={{
                            fontSize: 12,
                            color: token.colorError,
                            marginBottom: 4,
                          }}
                        >
                          {t('errorMessage', { message: task.error_message })}
                        </div>
                      )}

                      <div style={{ marginTop: 8 }}>
                        <Space size={4}>
                          {task.can_cancel && (task.status === 'running' || task.status === 'pending') && (
                            <Popconfirm
                              title={t('cancelTaskConfirm')}
                              onConfirm={() => handleCancelTask(task)}
                              okText={t('confirm')}
                              cancelText={t('cancel')}
                            >
                              <Button size="small" danger>
                                {t('cancel')}
                              </Button>
                            </Popconfirm>
                          )}
                          {task.can_delete && (task.status === 'completed' ||
                            task.status === 'failed' ||
                            task.status === 'cancelled') && (
                              <Popconfirm
                                title={t('deleteTaskConfirm')}
                                onConfirm={() => handleDeleteTask(task.id)}
                                okText={t('confirm')}
                                cancelText={t('cancel')}
                              >
                                <Button size="small" icon={<DeleteOutlined />}>
                                  {t('delete')}
                                </Button>
                              </Popconfirm>
                            )}
                        </Space>
                      </div>
                    </div>
                  </List.Item>
                )}
              />
            )}
          </>
        )}
      </Card>
    </div>
  );
};

export default FloatingTaskPanel;
