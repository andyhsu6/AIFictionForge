import { Card, Space, Tag, Typography, Popconfirm, theme } from 'antd';
import { EditOutlined, DeleteOutlined, UserOutlined, BankOutlined, ExportOutlined } from '@ant-design/icons';
import { characterCardStyles } from './CardStyles';
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
      'protagonist': '主角',
      'supporting': '配角',
      'antagonist': '反派',
    };
    return roleLabels[roleType || ''] || '其他';
  };

  const isOrganization = character.is_organization;
  const charStatus = character.status || 'active';
  const isInactive = charStatus !== 'active';

  const getStatusTag = () => {
    const statusConfig: Record<string, { color: string; label: string }> = {
      deceased: { color: token.colorTextBase, label: '💀 已死亡' },
      missing: { color: token.colorWarning, label: '❓ 已失踪' },
      retired: { color: token.colorTextTertiary, label: '📤 已退场' },
      destroyed: { color: token.colorTextBase, label: '💀 已覆灭' },
    };
    const config = statusConfig[charStatus];
    if (!config) return null;
    return <Tag color={config.color} style={{ marginLeft: 4 }}>{config.label}</Tag>;
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
          overflow: 'auto',
          display: 'flex',
          flexDirection: 'column'
        },
        actions: {
          borderRadius: '0 0 12px 12px'
        }
      }}
      actions={[
        ...(onEdit ? [<EditOutlined key="edit" onClick={() => onEdit(character)} />] : []),
        ...(onExport ? [<ExportOutlined key="export" onClick={onExport} />] : []),
        <Popconfirm
          key="delete"
          title={`确定删除这个${isOrganization ? '组织' : '角色'}吗？`}
          onConfirm={() => onDelete(character.id)}
          okText="确定"
          cancelText="取消"
        >
          <DeleteOutlined />
        </Popconfirm>,
      ]}
    >
      <Card.Meta
        avatar={
          isOrganization ? (
            <BankOutlined style={{ fontSize: 32, color: token.colorSuccess }} />
          ) : (
            <UserOutlined style={{ fontSize: 32, color: token.colorPrimary }} />
          )
        }
        title={
          <Space>
            <span style={characterCardStyles.nameEllipsis}>{character.name}</span>
            {isOrganization ? (
              <Tag color="green">组织</Tag>
            ) : (
              character.role_type && (
                <Tag color={getRoleTypeColor(character.role_type)}>
                  {getRoleTypeLabel(character.role_type)}
                </Tag>
              )
            )}
            {getStatusTag()}
          </Space>
        }
        description={
          <div style={characterCardStyles.descriptionBlock}>
            {/* 角色特有字段 */}
            {!isOrganization && (
              <>
                {character.age && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>年龄：</Text>
                    <Text style={{ flex: 1 }}>{character.age}</Text>
                  </div>
                )}
                {character.gender && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>性别：</Text>
                    <Text style={{ flex: 1 }}>{character.gender}</Text>
                  </div>
                )}
                {character.personality && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>性格：</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.personality }}
                    >
                      {character.personality}
                    </Text>
                  </div>
                )}
                {character.relationships && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>关系：</Text>
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
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>类型：</Text>
                    <Tag color="cyan">{character.organization_type}</Tag>
                  </div>
                )}
                {character.power_level !== undefined && character.power_level !== null && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>势力等级：</Text>
                    <Tag color={character.power_level >= 70 ? 'red' : character.power_level >= 50 ? 'orange' : 'default'}>
                      {character.power_level}
                    </Tag>
                  </div>
                )}
                {character.location && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>所在地：</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.location }}
                    >
                      {character.location}
                    </Text>
                  </div>
                )}
                {character.color && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>代表颜色：</Text>
                    <Text style={{ flex: 1, minWidth: 0 }}>{character.color}</Text>
                  </div>
                )}
                {character.motto && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>格言：</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.motto }}
                    >
                      {character.motto}
                    </Text>
                  </div>
                )}
                {character.organization_purpose && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>目的：</Text>
                    <Text
                      style={{ flex: 1, minWidth: 0 }}
                      ellipsis={{ tooltip: character.organization_purpose }}
                    >
                      {character.organization_purpose}
                    </Text>
                  </div>
                )}
                {character.organization_members && (
                  <div style={{ marginBottom: 8, display: 'flex', alignItems: 'flex-start' }}>
                    <Text type="secondary" style={{ flexShrink: 0 }}>成员：</Text>
                    <Text style={{ flex: 1, minWidth: 0, fontSize: 12, lineHeight: 1.6, wordBreak: 'break-all' }}>
                      {typeof character.organization_members === 'string'
                        ? character.organization_members
                        : JSON.stringify(character.organization_members)}
                    </Text>
                  </div>
                )}
              </>
            )}

            {/* 通用字段 - 背景信息截断显示 */}
            {character.background && (
              <div style={{ marginTop: 12 }}>
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
        }
      />
    </Card>
  );
};