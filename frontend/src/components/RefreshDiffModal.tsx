import { useEffect, useState } from 'react';
import { Alert, Button, Collapse, Drawer, Modal, Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { RefreshApplyResult, RefreshEntity, RefreshPlan } from '../services/profileRefreshService';

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

  const content = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Alert type="info" showIcon message={t('scopeNotice')} style={{ position: 'sticky', top: 0, zIndex: 2 }} />
      {result ? (
        <Alert
          type="success"
          showIcon
          message={t('applyResult', { applied: result.applied, skipped: result.stale.length })}
          description={t('fieldsWritten', { count: result.fields_written })}
        />
      ) : (
        <>
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
                  const value = field.action === 'preserved'
                    ? field.old || t('emptyValue')
                    : field.action === 'same'
                      ? field.old || t('emptyValue')
                      : [field.old || t('emptyValue'), field.new || t('emptyValue')].join(' → ');
                  const isExpanded = expanded[key] ?? false;
                  return (
                    <div key={key} style={{ minWidth: 0 }}>
                      <Space size={6} wrap>
                        <Typography.Text type="secondary">{t(`fields.${field.field}`, { defaultValue: field.field })}</Typography.Text>
                        <Tag color={ACTION_COLORS[field.action]}>{t(`actions.${field.action}`)}</Tag>
                      </Space>
                      <div
                        onClick={() => setExpanded(current => ({ ...current, [key]: !isExpanded }))}
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
                        {value}
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
