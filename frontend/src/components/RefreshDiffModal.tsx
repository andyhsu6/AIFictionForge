import { useEffect, useState } from 'react';
import { Alert, Button, Collapse, Drawer, Modal, Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { RefreshApplyResult, RefreshEntity, RefreshPlan } from '../services/profileRefreshService';
import { summarizeFieldChange } from '../utils/profileFieldDiff';
import type { FieldChangeSummary } from '../utils/profileFieldDiff';

/**
 * 折叠态的变化摘要：
 * - delta（高相似、列表式增删）：只显示「− 删除片段 / ＋ 新增片段」；
 * - rewrite（整段重写）：只显示字数变化，避免满屏红绿碎片。
 */
function ChangeSummaryLine({ summary }: { summary: FieldChangeSummary }) {
  const { t } = useTranslation('profileRefresh');
  const removed = summary.removed.join('、');
  const added = summary.added.join('、');
  if (summary.mode === 'rewrite' || (!removed && !added)) {
    return (
      <Typography.Text type="secondary">
        {t('rewritten', { old: summary.oldLength, new: summary.newLength })}
      </Typography.Text>
    );
  }
  return (
    <>
      {removed && <span style={{ color: 'var(--color-error)' }}>− {removed}</span>}
      {removed && added && <span>{'  '}</span>}
      {added && <span style={{ color: 'var(--color-success)' }}>＋ {added}</span>}
    </>
  );
}

/** 展开态：旧/新两栏对照（桌面并排、移动端堆叠），长文自动换行。 */
function OldNewBlocks({
  oldLabel,
  newLabel,
  oldText,
  newText,
  isMobile,
}: {
  oldLabel: string;
  newLabel: string;
  oldText: string | null;
  newText: string | null;
  isMobile: boolean;
}) {
  const { t } = useTranslation('profileRefresh');
  const blocks = [
    { label: oldLabel, text: oldText || t('emptyValue'), color: 'var(--color-error)' },
    { label: newLabel, text: newText || t('emptyValue'), color: 'var(--color-success)' },
  ];
  return (
    <div style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row', gap: 12, width: '100%', minWidth: 0 }}>
      {blocks.map(block => (
        <div
          key={block.label}
          style={{ flex: 1, minWidth: 0, borderLeft: `2px solid ${block.color}`, paddingLeft: 8 }}
        >
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{block.label}</Typography.Text>
          <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginTop: 2 }}>{block.text}</div>
        </div>
      ))}
    </div>
  );
}

interface RefreshDiffModalProps {
  open: boolean;
  plan: RefreshPlan;
  applying: boolean;
  result: RefreshApplyResult | null;
  onCancel: () => void;
  onApply: () => void;
}

const ACTION_COLORS = {
  add: 'blue',
  change: 'orange',
  preserved: 'default',
  rejected: 'red',
  same: 'default',
} as const;

const REJECTION_KEYS = [
  'quote_not_found',
  'quote_invalid',
  'field_invalid',
  'claim_invalid',
  'chapter_invalid',
  'inferred_not_allowed',
  'org_personality_dropped',
] as const;

export default function RefreshDiffModal({ open, plan, applying, result, onCancel, onApply }: RefreshDiffModalProps) {
  const { t } = useTranslation('profileRefresh');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 768);

  useEffect(() => {
    const handleResize = () => setIsMobile(window.innerWidth <= 768);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    if (open) setExpanded({});
  }, [open, plan]);

  // 后端可能跳过部分批次（如上游超时/内容审核）；旧计划没有 `skipped` 字段。
  const skippedBatches = plan.skipped ?? [];
  const skippedBatchCount = plan.summary.skipped_batches || skippedBatches.length;
  const skippedChapters = Array.from(
    new Set(skippedBatches.flatMap(batch => batch.chapter_numbers ?? [])),
  ).sort((a, b) => a - b);
  const skippedReasons = Array.from(new Set(skippedBatches.map(batch => batch.code)))
    .map(code => t(`skipReasons.${code}`, { defaultValue: code }))
    .join('、');
  const hasSkippedBatches = skippedBatchCount > 0 || skippedBatches.length > 0;

  const content = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Alert type="info" showIcon message={t('scopeNotice')} style={{ position: 'sticky', top: 0, zIndex: 2 }} />
      {result ? (
        <Alert
          type="success"
          showIcon
          message={t('applyResult', { applied: result.applied, skipped: result.stale.length })}
          description={(
            <>
              <div>{t('fieldsWritten', { count: result.fields_written })}</div>
              {result.facts_merged > 0 && <div>{t('factsMerged', { count: result.facts_merged })}</div>}
            </>
          )}
        />
      ) : (
        <>
          {hasSkippedBatches && (
            <Alert
              type="warning"
              showIcon
              message={t('skippedBatches', {
                count: skippedBatchCount,
                chapters: skippedChapters.join('、') || '—',
                reasons: skippedReasons || '—',
              })}
            />
          )}
          <Space wrap size={[8, 8]}>
            <Tag color="blue">{t('summary.add', { count: plan.summary.add })}</Tag>
            <Tag color="orange">{t('summary.change', { count: plan.summary.change })}</Tag>
            <Tag>{t('summary.preserved', { count: plan.summary.preserved })}</Tag>
            <Tag color="red">{t('summary.rejected', { count: plan.summary.rejected })}</Tag>
            <Tag>{t('summary.same', { count: plan.summary.same })}</Tag>
          </Space>
          {plan.entities.length === 0 ? (
            <Typography.Text type="secondary">{t('emptyDiff')}</Typography.Text>
          ) : plan.entities.map((entity: RefreshEntity) => (
            <section key={entity.id} style={{ width: '100%', minWidth: 0, boxSizing: 'border-box', overflow: 'hidden', border: '1px solid var(--color-border-secondary)', borderRadius: 8, padding: 12 }}>
              <Typography.Text strong>{entity.name}</Typography.Text>
              <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 10 }}>
                {entity.fields.map((field, index) => {
                  const key = `${entity.id}:${index}`;
                  const isChange = field.action === 'change';
                  const isRejected = field.action === 'rejected';
                  const changeSummary = isChange ? summarizeFieldChange(field.old, field.new) : null;
                  const value = field.action === 'preserved'
                    ? field.old || t('emptyValue')
                    : field.action === 'same'
                      ? field.old || t('emptyValue')
                      : [field.old || t('emptyValue'), field.new || t('emptyValue')].join(' → ');
                  const isExpanded = expanded[key] ?? false;
                  const toggle = () => setExpanded(current => ({ ...current, [key]: !isExpanded }));
                  return (
                    <div key={key} style={{ minWidth: 0 }}>
                      <Space size={6} wrap>
                        <Typography.Text type="secondary">{t(`fields.${field.field}`, { defaultValue: field.field })}</Typography.Text>
                        <Tag color={ACTION_COLORS[field.action]}>{t(`actions.${field.action}`)}</Tag>
                      </Space>
                      <div
                        onClick={toggle}
                        style={{
                          width: '100%',
                          maxWidth: '100%',
                          boxSizing: 'border-box',
                          margin: '4px 0 0',
                          cursor: 'pointer',
                          overflowWrap: 'anywhere',
                          whiteSpace: isExpanded ? 'normal' : 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          display: 'block',
                        }}
                        title={t('expandValue')}
                      >
                        {isChange && isExpanded ? (
                          <OldNewBlocks
                            oldLabel={t('oldValue')}
                            newLabel={t('newValue')}
                            oldText={field.old}
                            newText={field.new}
                            isMobile={isMobile}
                          />
                        ) : isChange && changeSummary ? (
                          <ChangeSummaryLine summary={changeSummary} />
                        ) : isRejected && isExpanded ? (
                          <OldNewBlocks
                            oldLabel={t('currentValue')}
                            newLabel={t('discardedSuggestion')}
                            oldText={field.old}
                            newText={field.new}
                            isMobile={isMobile}
                          />
                        ) : isRejected ? (
                          <Typography.Text type="secondary">
                            {t('discardedSuggestion')}：{field.new || t('emptyValue')}
                          </Typography.Text>
                        ) : (
                          value
                        )}
                      </div>
                      {field.action === 'preserved' && field.new && (
                        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4, overflowWrap: 'anywhere' }}>
                          {t('aiSuggestion', { suggestion: field.new })}
                        </Typography.Text>
                      )}
                    </div>
                  );
                })}
              </div>
              {REJECTION_KEYS.some(key => (entity.stats[key] || 0) > 0) && (
                <Collapse
                  ghost
                  size="small"
                  style={{ marginTop: 8 }}
                  items={[{
                    key: 'reasons',
                    label: t('rejectionReasons'),
                    children: (
                      <Space wrap>
                        {REJECTION_KEYS.filter(key => (entity.stats[key] || 0) > 0).map(key => (
                          <Tag color="red" key={key}>{t(`reasons.${key}`)} {entity.stats[key]}</Tag>
                        ))}
                      </Space>
                    ),
                  }]}
                />
              )}
            </section>
          ))}
        </>
      )}
    </div>
  );

  const footer = result ? (
    <Button type="primary" onClick={onCancel}>{t('close')}</Button>
  ) : isMobile ? (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button block onClick={onCancel}>{t('cancel')}</Button>
      <Button block type="primary" loading={applying} onClick={onApply} disabled={plan.entities.length === 0}>
        {t('applyChanges')}
      </Button>
    </Space>
  ) : (
    <Space>
      <Button onClick={onCancel}>{t('cancel')}</Button>
      <Button type="primary" loading={applying} onClick={onApply} disabled={plan.entities.length === 0}>
        {t('applyChanges')}
      </Button>
    </Space>
  );

  if (isMobile) {
    return (
      <Drawer
        title={t('diffTitle')}
        open={open}
        onClose={onCancel}
        placement="bottom"
        height="100%"
        styles={{ body: { overflowY: 'auto' }, footer: { display: 'flex', justifyContent: 'flex-end' } }}
        footer={footer}
      >
        {content}
      </Drawer>
    );
  }

  return (
    <Modal
      title={t('diffTitle')}
      open={open}
      onCancel={onCancel}
      width={760}
      footer={footer}
      styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
    >
      {content}
    </Modal>
  );
}
