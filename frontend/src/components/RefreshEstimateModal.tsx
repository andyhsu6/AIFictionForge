import { useEffect, useState } from 'react';
import { Alert, App, Button, Form, InputNumber, Modal, Radio, Space, Statistic, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { getTaskStatus } from '../services/backgroundTaskService';
import type { TaskStatus } from '../services/backgroundTaskService';
import { mapTaskStatusMessage } from '../services/errorMapper';
import { profileRefreshApi } from '../services/profileRefreshService';
import type { RefreshApplyResult, RefreshEstimate, RefreshMode, RefreshPlan } from '../services/profileRefreshService';
import { eventBus, EventNames } from '../store/eventBus';
import { useCharacterSync } from '../store/hooks';
import RefreshDiffModal from './RefreshDiffModal';

interface RefreshEstimateModalProps {
  open: boolean;
  projectId: string;
  onClose: () => void;
}

export default function RefreshEstimateModal({ open, projectId, onClose }: RefreshEstimateModalProps) {
  const { message } = App.useApp();
  const { t } = useTranslation('profileRefresh');
  const [mode, setMode] = useState<RefreshMode>('incremental');
  const [fromChapter, setFromChapter] = useState(1);
  const [estimate, setEstimate] = useState<RefreshEstimate | null>(null);
  const [loadingEstimate, setLoadingEstimate] = useState(false);
  const [previewTaskId, setPreviewTaskId] = useState<string | null>(null);
  const [previewStatus, setPreviewStatus] = useState<TaskStatus['status'] | null>(null);
  const [previewProgress, setPreviewProgress] = useState<{ batches_done?: number; batches_total?: number } | null>(null);
  const [plan, setPlan] = useState<RefreshPlan | null>(null);
  const [applyResult, setApplyResult] = useState<RefreshApplyResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const { refreshCharacters } = useCharacterSync();

  useEffect(() => {
    if (!open) {
      setEstimate(null);
      setLoadingEstimate(false);
      return;
    }
    let active = true;
    setEstimate(null);
    setLoadingEstimate(true);
    const timer = setTimeout(() => {
      profileRefreshApi.estimate(projectId, mode, mode === 'full' ? fromChapter : 1)
        .then(result => { if (active) setEstimate(result); })
        .catch(() => { if (active) setEstimate(null); })
        .finally(() => { if (active) setLoadingEstimate(false); });
    }, 350);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [open, projectId, mode, fromChapter]);

  useEffect(() => {
    if (!previewTaskId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const task = await getTaskStatus(previewTaskId);
        if (!active) return;
        setPreviewStatus(task.status);
        setPreviewProgress(task.progress_details as (NonNullable<TaskStatus['progress_details']> & {
          batches_done?: number;
          batches_total?: number;
        }) | null);
        if (task.status === 'completed') {
          const taskPlan = task.task_result?.plan as RefreshPlan | undefined;
          if (!taskPlan) {
            message.error(t('planMissing'));
            setPreviewTaskId(null);
            setPreviewing(false);
            return;
          }
          setPlan(taskPlan);
          setPreviewing(false);
        } else if (task.status === 'failed' || task.status === 'cancelled') {
          message.error(mapTaskStatusMessage(task));
          setPreviewTaskId(null);
          setPreviewing(false);
        } else {
          timer = setTimeout(poll, 1800);
        }
      } catch {
        if (active) timer = setTimeout(poll, 2500);
      }
    };
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [previewTaskId, message, t]);

  const handleClose = () => {
    setMode('incremental');
    setFromChapter(1);
    setPlan(null);
    setApplyResult(null);
    setEstimate(null);
    setPreviewTaskId(null);
    setPreviewStatus(null);
    setPreviewProgress(null);
    setPreviewing(false);
    onClose();
  };

  const startPreview = async () => {
    setPreviewing(true);
    try {
      const task = await profileRefreshApi.preview(projectId, mode, mode === 'full' ? fromChapter : 1);
      const taskId = task.id || task.task_id;
      if (!taskId) throw new Error('Preview task response has no task identifier');
      setPreviewTaskId(taskId);
      setPreviewStatus('pending');
      setPreviewProgress(null);
      eventBus.emit(EventNames.BACKGROUND_TASK_CREATED, { projectId, taskId });
    } catch {
      setPreviewing(false);
    }
  };

  const applyPlan = async () => {
    if (!plan || !previewTaskId) return;
    setApplying(true);
    try {
      const result = await profileRefreshApi.apply(projectId, previewTaskId);
      setApplyResult(result);
      await refreshCharacters();
    } catch {
      // The API client already displays localized HTTP errors.
    } finally {
      setApplying(false);
    }
  };

  const estimateContent = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Radio.Group value={mode} onChange={event => setMode(event.target.value)}>
        <Space direction="vertical">
          <Radio value="incremental">{t('mode.incremental')}</Radio>
          <Radio value="full">{t('mode.full')}</Radio>
        </Space>
      </Radio.Group>
      {mode === 'full' && (
        <Form layout="inline">
          <Form.Item label={t('fromChapter', { chapter: fromChapter })} style={{ marginBottom: 0 }}>
            <InputNumber min={1} precision={0} value={fromChapter} onChange={value => setFromChapter(value || 1)} />
          </Form.Item>
        </Form>
      )}
      {loadingEstimate ? <Typography.Text type="secondary">{t('loadingEstimate')}</Typography.Text> : estimate ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 12 }}>
          <Statistic title={t('metrics.candidates')} value={estimate.candidates} />
          <Statistic title={t('metrics.batches')} value={estimate.chunks} />
          <Statistic title={t('metrics.calls')} value={`${estimate.calls_min}–${estimate.calls_max}`} />
        </div>
      ) : <Alert type="error" showIcon message={t('estimateFailed')} />}
      {estimate?.candidates === 0 && (
        <Alert type="info" showIcon message={t('emptyCandidates')} description={t('incrementalEmptyHint')} />
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <Alert type="info" showIcon message={t('notes.full')} />
        <Alert type="info" showIcon message={t('notes.scope')} />
        <Alert type="warning" showIcon message={t('notes.calls')} />
      </div>
    </div>
  );

  return (
    <>
      <Modal
        title={t('estimateTitle')}
        open={open && !plan}
        onCancel={handleClose}
        footer={(
          <Space>
            <Button onClick={handleClose}>{t('cancel')}</Button>
            <Button type="primary" loading={previewing} disabled={!estimate || estimate.candidates === 0 || loadingEstimate} onClick={startPreview}>
              {t('createPreview')}
            </Button>
          </Space>
        )}
        width={560}
      >
        {previewing ? (
          <Alert
            type="info"
            showIcon
            message={previewProgress?.batches_done !== undefined && previewProgress.batches_total !== undefined
              ? t('taskBatchProgress', { current: previewProgress.batches_done, total: previewProgress.batches_total })
              : t('previewProgress', {
                status: t(previewStatus === 'running' ? 'taskStatus.running' : 'taskStatus.pending'),
              })}
          />
        ) : estimateContent}
      </Modal>
      {plan && (
        <RefreshDiffModal
          open
          plan={plan}
          applying={applying}
          result={applyResult}
          onCancel={handleClose}
          onApply={applyPlan}
        />
      )}
    </>
  );
}
