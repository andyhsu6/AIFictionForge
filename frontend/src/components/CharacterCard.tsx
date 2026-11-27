import { Button, Card, Tag, Typography, Popconfirm, theme } from 'antd';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { EditOutlined, DeleteOutlined, UserOutlined, BankOutlined, ExportOutlined, ReadOutlined } from '@ant-design/icons';
import { characterCardStyles } from './CardStyles';
import ProfileSourceModal from './ProfileSourceModal';
import type { Character } from '../types';

const { Text, Paragraph } = Typography;

interface CharacterCardProps {
  character: Character;
  onEdit?: (character: Character) => void;
  onDelete: (id: string) => void;
  onExport?: () => void;
}

export const CharacterCard: React.FC<CharacterCardProps> = ({ character, onEdit, onDelete, onExport }) => {
  const { token } = theme.useToken();
  const { t } = useTranslation('characterCard');
  const [sourcesOpen, setSourcesOpen] = useState(false);

  const getRoleTypeColor = (roleType?: string) => {
    const roleColors: Record<string, string> = {
      'protagonist': 'blue',
      'supporting': 'green',
      'antagonist': 'red',
    };
    return roleColors[roleType || ''] || 'default';
  };

  const getRoleTypeLabel = (roleType?: string) => {
    const roleLabels: Record<string, string> = {
      'protagonist': t('roleProtagonist'),
      'supporting': t('roleSupporting'),
      'antagonist': t('roleAntagonist'),
    };
    return roleLabels[roleType || ''] || t('roleOther');
  };

  const isOrganization = character.is_organization;
  const charStatus = character.status || 'active';
  const isInactive = charStatus !== 'active';
  const profileFacts = character.profile_facts ?? [];
  const organizationMembersText = character.organization_members
    ? (typeof character.organization_members === 'string'
      ? character.organization_members
      : JSON.stringify(character.organization_members))
    : '';
  const renderSourceButton = () => {
    if (profileFacts.length === 0) return null;
    return (
      <Button
        type="text"
        size="small"
        aria-label={t('sourceLabel')}
        icon={<ReadOutlined />}
        style={{ height: 22, lineHeight: 1, padding: 0 }}
        onClick={() => setSourcesOpen(true)}
      />
    );
  };

  const getStatusTag = () => {
    const statusConfig: Record<string, { color: string; label: string }> = {
      deceased: { color: token.colorTextBase, label: t('statusDeceased') },
      missing: { color: token.colorWarning, label: t('statusMissing') },
      retired: { color: token.colorTextTertiary, label: t('statusRetired') },
      destroyed: { color: token.colorTextBase, label: t('statusDestroyed') },
    };
    const config = statusConfig[charStatus];
    if (!config) return null;
    return <Tag color={config.color} style={{ marginInlineEnd: 0, flexShrink: 0 }}>{config.label}</Tag>;
  };

  return (
    <Card
      hoverable
      style={{
        ...(isOrganization ? characterCardStyles.organizationCard : characterCardStyles.characterCard),
        ...(isInactive ? { opacity: 0.6, filter: 'grayscale(40%)' } : {}),
      }}
      styles={{
        body: {
          flex: 1,
          minHeight: 0,
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          padding: '24px 20px 16px'
        },
        actions: {
          borderRadius: '0 0 12px 12px',
          flexShrink: 0
        }
      }}
      actions={[
        ...(onEdit ? [<EditOutlined key="edit" onClick={() => onEdit(character)} />] : []),
        ...(onExport ? [<ExportOutlined key="export" onClick={onExport} />] : []),
        ...(profileFacts.length > 0 ? [<span key="source">{renderSourceButton()}</span>] : []),
        <Popconfirm
          key="delete"
          title={t('deleteConfirm', { type: isOrganization ? t('organization') : t('character') })}
          onConfirm={() => onDelete(character.id)}
          okText={t('confirm')}
          cancelText={t('cancel')}
        >
          <DeleteOutlined />
        </Popconfirm>,
      ]}
    >
      <ProfileSourceModal
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
        title={character.name}
        facts={profileFacts}
        updatedAt={character.updated_at}
        factsUpdatedAt={character.profile_facts_updated_at}
      />
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexShrink: 0, minWidth: 0 }}>
        <div style={{ flexShrink: 0, paddingTop: 2, display: 'flex', alignItems: 'center' }}>
          {isOrganization ? (
            <BankOutlined style={{ fontSize: 28, color: token.colorSuccess }} />
          ) : (
            <UserOutlined style={{ fontSize: 28, color: token.colorPrimary }} />
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          <Text strong ellipsis={{ tooltip: character.name }} style={{ fontSize: 15, lineHeight: '22px' }}>
            {character.name}
          </Text>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minHeight: 22 }}>
            {isOrganization ? (
              <Tag color="green" style={{ marginInlineEnd: 0 }}>{t('organization')}</Tag>
            ) : (
              character.role_type && (
                <Tag color={getRoleTypeColor(character.role_type)} style={{ marginInlineEnd: 0 }}>
                  {getRoleTypeLabel(character.role_type)}
                </Tag>
              )
            )}
            {getStatusTag()}
          </div>
        </div>
      </div>
      <div style={{ ...characterCardStyles.descriptionBlock, width: '100%' }}>
            {/* 角色特有字段 */}
            {!isOrganization && (
              <>
                {character.age && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('age')}</Text>
                    <Text style={{ flex: 1, minWidth: 0 }} ellipsis={{ tooltip: character.age }}>
                      {character.age}
                    </Text>
                  </div>
                )}
                {character.gender && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('gender')}</Text>
                    <Text style={{ flex: 1, minWidth: 0 }} ellipsis={{ tooltip: character.gender }}>
                      {character.gender}
                    </Text>
                  </div>
                )}
                {character.personality && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('personality')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.personality }}
                    >
                      {character.personality}
                    </Text>
                  </div>
                )}
                {character.appearance && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('appearance')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.appearance }}
                    >
                      {character.appearance}
                    </Text>
                  </div>
                )}
                {character.relationships && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('relationships')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.relationships }}
                    >
                      {character.relationships}
                    </Text>
                  </div>
                )}
              </>
            )}

            {/* 组织特有字段 */}
            {isOrganization && (
              <>
                {character.organization_type && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('organizationType')}</Text>
                    <Tag color="cyan">{character.organization_type}</Tag>
                  </div>
                )}
                {character.power_level !== undefined && character.power_level !== null && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('powerLevel')}</Text>
                    <Tag color={character.power_level >= 70 ? 'red' : character.power_level >= 50 ? 'orange' : 'default'}>
                      {character.power_level}
                    </Tag>
                  </div>
                )}
                {character.location && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('location')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.location }}
                    >
                      {character.location}
                    </Text>
                  </div>
                )}
                {character.color && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('representativeColor')}</Text>
                    <Text style={{ flex: 1, minWidth: 0 }} ellipsis={{ tooltip: character.color }}>
                      {character.color}
                    </Text>
                  </div>
                )}
                {character.motto && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('motto')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.motto }}
                    >
                      {character.motto}
                    </Text>
                  </div>
                )}
                {character.organization_purpose && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('purpose')}</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.organization_purpose }}
                    >
                      {character.organization_purpose}
                    </Text>
                  </div>
                )}
                {organizationMembersText && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>{t('members')}</Text>
                    <Text style={{ flex: 1, minWidth: 0, fontSize: 12 }} ellipsis={{ tooltip: organizationMembersText }}>
                      {organizationMembersText}
                    </Text>
                  </div>
                )}
              </>
            )}

            {/* 通用字段 - 背景信息截断显示 */}
            {character.background && (
              <div style={{ marginTop: 8 }}>
                <Paragraph
                  type="secondary"
                  style={{ fontSize: 12, marginBottom: 0 }}
                  ellipsis={{ tooltip: character.background, rows: 3 }}
                >
                  {character.background}
                </Paragraph>
              </div>
            )}
      </div>
    </Card>
  );
};
