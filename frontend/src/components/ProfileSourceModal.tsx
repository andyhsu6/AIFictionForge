import { Button, Modal, Space, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { CharacterProfileFact } from '../types';

const { Text } = Typography;
type ProfileField = 'personality' | 'background' | 'appearance';
const PROFILE_FIELDS: ProfileField[] = ['personality', 'background', 'appearance'];

interface ProfileSourceModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  facts: CharacterProfileFact[];
  updatedAt?: string | null;
  factsUpdatedAt?: string | null;
}

export default function ProfileSourceModal({
  open,
  onClose,
  title,
  facts,
  updatedAt,
  factsUpdatedAt,
}: ProfileSourceModalProps) {
  const { t } = useTranslation('characterCard');
  const updatedTime = updatedAt ? Date.parse(updatedAt) : NaN;
  const factsUpdatedTime = factsUpdatedAt ? Date.parse(factsUpdatedAt) : NaN;
  const hasEditedProfile = Number.isFinite(updatedTime)
    && Number.isFinite(factsUpdatedTime)
    && updatedTime - factsUpdatedTime > 2000;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={`${t('sourceTitle')} - ${title}`}
      width="min(640px, calc(100vw - 32px))"
      styles={{ body: { maxHeight: '60vh', overflowY: 'auto' } }}
      footer={<Button onClick={onClose}>{t('confirm')}</Button>}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {hasEditedProfile && <Text type="secondary">{t('sourceEditedWarning')}</Text>}
        {PROFILE_FIELDS.map(field => {
          const fieldFacts = facts.filter(fact => fact.field === field);
          if (fieldFacts.length === 0) return null;
          return (
            <div key={field}>
              <Text strong style={{ display: 'block', marginBottom: 6 }}>{t(field)}</Text>
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                {fieldFacts.map((fact, index) => (
                  <div key={`${fact.claim}-${fact.chapter_number}-${index}`}>
                    <Text strong>{fact.claim}</Text>
                    {fact.quote && (
                      <Text style={{ display: 'block', marginTop: 2 }}>
                        {t('quoteLabel')}：「{fact.quote}」
                      </Text>
                    )}
                    {fact.chapter_number != null && (
                      <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                        {t('chapterNumber', { number: fact.chapter_number })}
                      </Text>
                    )}
                  </div>
                ))}
              </Space>
            </div>
          );
        })}
      </Space>
    </Modal>
  );
}
