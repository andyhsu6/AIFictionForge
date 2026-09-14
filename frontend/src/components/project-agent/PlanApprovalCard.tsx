import { CheckOutlined, CloseOutlined, DeleteOutlined } from '@ant-design/icons';
import { App, Button, Checkbox, Space, Typography, theme } from 'antd';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { projectAgentApi } from '../../services/api';
import { parsePlanPayload, toggleStepSelection } from './planCardModel';
import type { AgentToolCall } from '../../types';

const { Text } = Typography;

interface PlanApprovalCardProps {
  projectId: string;
  toolCall: AgentToolCall;
  /** 批准/拒绝成功后回调（面板用它刷新会话） */
  onDecided: () => void;
}

/**
 * 一次性批准整份计划。刻意做成「整份计划只有一个批准入口」：
 * 逐步确认已被计划执行器取代（架构 §6），再留一个单步确认按钮就会退化成旧体验。
 * 步骤目标在执行期才由 preview 校验，批准时不可验证 ⇒ 卡片必须如实标注（§1 修 F2）。
 *
 * 空 payload（无 `steps` 键 / steps 为空 / 超过批准闸门步数）⇒ 渲染空容器：
 * 批准入口消失，而不是给出一张「看起来能批准、点下去后端报错」的残卡。
 * 该情形下计划行仍留在过程区，`planEmptyTitle` 供 Task 7 的进度条在卡缺失时兜底。
 */
export default function PlanApprovalCard({ projectId, toolCall, onDecided }: PlanApprovalCardProps) {
  const { message } = App.useApp();
  const { t } = useTranslation('projectAgentPanel');
  const { token } = theme.useToken();
  const payload = useMemo(() => parsePlanPayload(toolCall), [toolCall]);
  const allStepIds = useMemo(() => (payload ? payload.steps.map(step => step.id) : []), [payload]);
  const [selected, setSelected] = useState<string[]>(allStepIds);
  const [approving, setApproving] = useState(false);
  const [rejecting, setRejecting] = useState(false);

  if (!payload) return null;
  const overwriteActions = new Set(
    t('planOverwriteActions').split(',').map(item => item.trim()).filter(Boolean),
  );

  const approve = async () => {
    setApproving(true);
    try {
      await projectAgentApi.approvePlan(projectId, toolCall.id, { selected_step_ids: selected });
      message.success(t('planApprovedToast'));
      onDecided();
    } catch (error) {
      message.error(t('planApproveFailed', { message: (error as Error).message }));
    } finally {
      setApproving(false);
    }
  };

  const reject = async () => {
    setRejecting(true);
    try {
      await projectAgentApi.rejectToolCall(projectId, toolCall.id);
      onDecided();
    } catch (error) {
      message.error(t('processChangeFailed', { message: (error as Error).message }));
    } finally {
      setRejecting(false);
    }
  };

  return (
    <div
      data-testid="plan-approval-card"
      style={{
        border: `1px solid ${token.colorWarningBorder}`,
        borderRadius: 10,
        padding: 10,
        marginTop: 10,
        background: token.colorWarningBg,
      }}
    >
      <Text strong style={{ fontSize: 12 }}>{t('planCardTitle')}</Text>
      {payload.objective && (
        <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 4 }}>
          {t('planObjective', { objective: payload.objective })}
        </Text>
      )}
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {payload.steps.map((step, index) => {
          const checked = selected.includes(step.id);
          return (
            <div key={step.id} style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
              <Checkbox
                data-testid={`plan-step-toggle-${step.id}`}
                checked={checked}
                style={{ marginTop: 2 }}
                disabled={approving || rejecting}
                onChange={() => {
                  // 第三参传全量步骤 id：结果按原计划顺序，不随点选顺序漂移。
                  const next = toggleStepSelection(selected, step.id, allStepIds);
                  if (next.length === selected.length && selected.includes(step.id)) {
                    message.info(t('planStepUntoggle'));
                    return;
                  }
                  setSelected(next);
                }}
              />
              <div style={{ minWidth: 0, flex: 1 }}>
                <Text style={{ fontSize: 12 }}>
                  {index + 1}. {step.action}
                </Text>
                {step.note && (
                  <Text type="secondary" style={{ display: 'block', fontSize: 11, whiteSpace: 'pre-wrap' }}>
                    {step.note}
                  </Text>
                )}
                {step.action !== null && overwriteActions.has(step.action) && (
                  <Text type="warning" style={{ display: 'block', fontSize: 11 }}>
                    {t('planOverwriteWarning')}
                  </Text>
                )}
              </div>
              {!checked && <DeleteOutlined style={{ fontSize: 11, color: token.colorTextSecondary }} />}
            </div>
          );
        })}
      </div>
      <Text type="secondary" style={{ display: 'block', fontSize: 11, marginTop: 8 }}>
        {t('planUnverifiableWarning')}
      </Text>
      {/* G1（架构 §3③，修 H6）：批准前必须披露队列占用后果——计划可跑数十分钟，
          期间用户的手工生成任务排在它后面。文案走 locale，禁止硬编码。 */}
      <Text type="secondary" data-testid="plan-queue-notice" style={{ display: 'block', fontSize: 11, marginTop: 4 }}>
        {t('planQueueNotice')}
      </Text>
      <Space style={{ marginTop: 10 }}>
        <Button
          data-testid="plan-approve"
          type="primary"
          size="small"
          icon={<CheckOutlined />}
          loading={approving}
          disabled={Boolean(rejecting) || selected.length === 0}
          onClick={() => void approve()}
        >{approving ? t('planApproving') : t('planApprove')}</Button>
        <Button
          data-testid="plan-reject"
          size="small"
          icon={<CloseOutlined />}
          loading={rejecting}
          disabled={approving}
          onClick={() => void reject()}
        >{t('planReject')}</Button>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {t('planStepProgress', { current: selected.length, total: payload.steps.length })}
        </Text>
      </Space>
    </div>
  );
}
