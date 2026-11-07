import { useState, useEffect, useCallback } from 'react';
import {
  Button,
  Modal,
  Form,
  Input,
  App,
  Card,
  Space,
  Tag,
  Popconfirm,
  Empty,
  Typography,
  Row,
  Col,
  theme,
  Alert,
  Checkbox,
  Descriptions,
  InputNumber,
  Spin,
  Switch,
} from 'antd';
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  StarOutlined,
  StarFilled,
  ReloadOutlined,
} from '@ant-design/icons';
import { useStore } from '../store';
import { projectApi, writingStyleApi } from '../services/api';
import type {
  StyleProfileReextractResult,
  WritingStyle,
  WritingStyleCreate,
  WritingStyleUpdate,
} from '../types';
import { useTranslation } from 'react-i18next';

const { TextArea } = Input;
const { Text, Paragraph } = Typography;

// issue #254 P1：画像行类型（profile / profile_edited）
const isProfileType = (styleType: string) =>
  styleType === 'profile' || styleType === 'profile_edited';

const parseProfileJson = (raw?: string | null): Record<string, unknown> | null => {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
};

const asNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const formatDecimal = (value: unknown): string => {
  const num = asNumber(value);
  return num === null ? '-' : String(Math.round(num * 10) / 10);
};

const formatPercent = (value: unknown): string => {
  const num = asNumber(value);
  return num === null ? '-' : `${Math.round(num * 1000) / 10}%`;
};

export default function WritingStyles() {
  const { message } = App.useApp();
  const { t } = useTranslation('writingStyles');
  const { currentProject, updateProject: updateProjectInStore } = useStore();
  const [styles, setStyles] = useState<WritingStyle[]>([]);
  const [loading, setLoading] = useState(false);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [editingStyle, setEditingStyle] = useState<WritingStyle | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  // issue #254 P1：重新抽取画像 diff 弹窗
  const [reextractOpen, setReextractOpen] = useState(false);
  const [reextractLoading, setReextractLoading] = useState(false);
  const [reextractApplying, setReextractApplying] = useState(false);
  const [reextractResult, setReextractResult] = useState<StyleProfileReextractResult | null>(null);
  const [overwriteEdited, setOverwriteEdited] = useState(false);
  const [setDefault, setSetDefault] = useState(false);

  // issue #254 P1：项目级原文语感锚点设置
  const [anchorEnabled, setAnchorEnabled] = useState(currentProject?.style_anchor_enabled ?? true);
  const [anchorChars, setAnchorChars] = useState(currentProject?.style_anchor_chars ?? 800);
  const [anchorCount, setAnchorCount] = useState(currentProject?.style_anchor_count ?? 3);
  const [anchorSaving, setAnchorSaving] = useState(false);

  const { token } = theme.useToken();

  const isMobile = window.innerWidth <= 768;
  
  // 卡片网格配置
  const gridConfig = {
    gutter: isMobile ? 8 : 16, // 卡片之间的间距
    xs: 24,
    sm: 24,
    md: 12,
    lg: 8,
    xl: 6,
  };

  // 加载风格列表 - 如果有项目则加载项目风格（包含默认标记），否则加载用户风格
  useEffect(() => {
    loadStyles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.id]);

  // 项目切换或服务端返回新值后同步锚点表单
  useEffect(() => {
    setAnchorEnabled(currentProject?.style_anchor_enabled ?? true);
    setAnchorChars(currentProject?.style_anchor_chars ?? 800);
    setAnchorCount(currentProject?.style_anchor_count ?? 3);
  }, [
    currentProject?.id,
    currentProject?.style_anchor_enabled,
    currentProject?.style_anchor_chars,
    currentProject?.style_anchor_count,
  ]);

  const loadStyles = useCallback(async () => {
    try {
      setLoading(true);
      // 如果有当前项目，使用项目API获取（包含is_default标记）
      // 否则使用用户API获取（所有风格的is_default都是false）
      const response = currentProject?.id
        ? await writingStyleApi.getProjectStyles(currentProject.id)
        : await writingStyleApi.getUserStyles();
      
      // 排序：默认风格优先显示
      const sortedStyles = (response.styles || []).sort((a, b) => {
        // 默认风格排在最前面
        if (a.is_default && !b.is_default) return -1;
        if (!a.is_default && b.is_default) return 1;
        // 其他按原有顺序（order_index）
        return 0;
      });
      
      setStyles(sortedStyles);
    } catch {
      message.error(t('toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [currentProject?.id, message, t]);

  const handleCreate = async (values: { name: string; description?: string; prompt_content: string }) => {
    try {
      const createData: WritingStyleCreate = {
        name: values.name,
        style_type: 'custom',
        description: values.description,
        prompt_content: values.prompt_content,
      };

      await writingStyleApi.createStyle(createData);
      message.success(t('toast.createSuccess'));
      setIsCreateModalOpen(false);
      createForm.resetFields();
      await loadStyles();
    } catch {
      message.error(t('toast.createFailed'));
    }
  };

  const handleEdit = (style: WritingStyle) => {
    setEditingStyle(style);
    editForm.setFieldsValue({
      name: style.name,
      description: style.description,
      prompt_content: style.prompt_content,
    });
    setIsEditModalOpen(true);
  };

  const handleUpdate = async (values: WritingStyleUpdate) => {
    if (!editingStyle) return;

    try {
      await writingStyleApi.updateStyle(editingStyle.id, values);
      message.success(t('toast.updateSuccess'));
      setIsEditModalOpen(false);
      editForm.resetFields();
      setEditingStyle(null);
      await loadStyles();
    } catch {
      message.error(t('toast.updateFailed'));
    }
  };

  const handleDelete = async (styleId: number) => {
    try {
      await writingStyleApi.deleteStyle(styleId);
      message.success(t('toast.deleteSuccess'));
      await loadStyles();
    } catch {
      message.error(t('toast.deleteFailed'));
    }
  };

  const handleSetDefault = async (styleId: number) => {
    if (!currentProject?.id) {
      message.warning(t('toast.selectProjectFirst'));
      return;
    }
    
    try {
      await writingStyleApi.setDefaultStyle(styleId, currentProject.id);
      message.success(t('toast.setDefaultSuccess'));
      await loadStyles();
    } catch {
      message.error(t('toast.setDefaultFailed'));
    }
  };

  const showCreateModal = () => {
    createForm.resetFields();
    setIsCreateModalOpen(true);
  };

  // issue #254 P1：保存项目级锚点设置（随 PUT /projects/{id} 一起更新）
  const handleSaveAnchor = async () => {
    if (!currentProject?.id) return;
    setAnchorSaving(true);
    try {
      const updated = await projectApi.updateProject(currentProject.id, {
        style_anchor_enabled: anchorEnabled,
        style_anchor_chars: anchorChars,
        style_anchor_count: anchorCount,
      });
      // 本地同步（PUT 返回的完整项目对象会带上锚点字段）
      updateProjectInStore(currentProject.id, updated);
      message.success(t('anchor.saved'));
    } catch {
      message.error(t('anchor.saveFailed'));
    } finally {
      setAnchorSaving(false);
    }
  };

  // issue #254 P1：先 dry_run 预览 diff，再决定是否应用
  const handleOpenReextract = async () => {
    if (!currentProject?.id) return;
    setReextractOpen(true);
    setReextractResult(null);
    setOverwriteEdited(false);
    setSetDefault(false);
    setReextractLoading(true);
    try {
      const result = await writingStyleApi.reextractProfile(currentProject.id, { dry_run: true });
      setReextractResult(result);
    } catch {
      message.error(t('profile.reextractFailed'));
      setReextractOpen(false);
    } finally {
      setReextractLoading(false);
    }
  };

  const handleApplyReextract = async () => {
    if (!currentProject?.id) return;
    setReextractApplying(true);
    try {
      await writingStyleApi.reextractProfile(currentProject.id, {
        dry_run: false,
        overwrite_edited: overwriteEdited,
        set_default: setDefault,
      });
      message.success(t('profile.applySuccess'));
      setReextractOpen(false);
      await loadStyles();
    } catch {
      message.error(t('profile.applyFailed'));
    } finally {
      setReextractApplying(false);
    }
  };

  const getStyleTypeColor = (styleType: string) => {
    if (isProfileType(styleType)) return 'cyan';
    return styleType === 'preset' ? 'blue' : 'purple';
  };

  const getStyleTypeLabel = (styleType: string) => {
    if (styleType === 'preset') return t('type.preset');
    if (styleType === 'profile') return t('type.profile');
    if (styleType === 'profile_edited') return t('type.profileEdited');
    return t('type.custom');
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{
        position: 'sticky',
        top: 0,
        zIndex: 10,
        backgroundColor: token.colorBgContainer,
        padding: isMobile ? '12px 0' : '16px 0',
        marginBottom: isMobile ? 12 : 16,
        borderBottom: `1px solid ${token.colorBorderSecondary}`,
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
      }}>
        <h2 style={{ margin: 0, fontSize: isMobile ? 18 : 24 }}>
          <EditOutlined style={{ marginRight: 8 }} />
          {t('page.title')}
        </h2>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={showCreateModal}
        >
          {t('page.createButton')}
        </Button>
      </div>

      {currentProject && (
        <Card size="small" style={{ marginBottom: isMobile ? 12 : 16 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap size={8}>
              <Text strong>{t('anchor.title')}</Text>
              <Switch checked={anchorEnabled} onChange={setAnchorEnabled} size="small" />
              <Text type="secondary" style={{ fontSize: 12 }}>{t('anchor.enabled')}</Text>
            </Space>
            <Space wrap size={12}>
              <Space size={6}>
                <Text type="secondary" style={{ fontSize: 12 }}>{t('anchor.chars')}</Text>
                <InputNumber
                  min={150}
                  max={2000}
                  step={50}
                  value={anchorChars}
                  disabled={!anchorEnabled}
                  onChange={(value) => setAnchorChars(value ?? 800)}
                  style={{ width: 96 }}
                />
              </Space>
              <Space size={6}>
                <Text type="secondary" style={{ fontSize: 12 }}>{t('anchor.count')}</Text>
                <InputNumber
                  min={2}
                  max={4}
                  value={anchorCount}
                  disabled={!anchorEnabled}
                  onChange={(value) => setAnchorCount(value ?? 3)}
                  style={{ width: 72 }}
                />
              </Space>
              <Button size="small" loading={anchorSaving} onClick={handleSaveAnchor}>
                {t('anchor.save')}
              </Button>
            </Space>
          </Space>
        </Card>
      )}

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {styles.length === 0 ? (
          <Empty description={t('empty.noStyles')} />
        ) : (
          <Row
            gutter={[0, gridConfig.gutter]}
            style={{ marginLeft: 0, marginRight: 0 }}
          >
            {styles.map((rawStyle) => {
              const tPreset = t as (key: string, opts?: Record<string, unknown>) => string;
              const style = rawStyle.preset_id
                ? {
                    ...rawStyle,
                    name: tPreset(`preset.${rawStyle.preset_id}.name`, { defaultValue: rawStyle.name }),
                    description: tPreset(`preset.${rawStyle.preset_id}.description`, { defaultValue: rawStyle.description }),
                    prompt_content: tPreset(`preset.${rawStyle.preset_id}.requirements`, { defaultValue: rawStyle.prompt_content }),
                  }
                : rawStyle;
              const isProfileStyle = style.style_type === 'profile' || style.style_type === 'profile_edited';
              const parsedProfile = isProfileStyle ? parseProfileJson(style.profile_json) : null;
              const profileMetrics = (parsedProfile?.metrics ?? {}) as Record<string, unknown>;
              const profileConfidence = parsedProfile ? asNumber(parsedProfile.confidence) : null;
              return (
              <Col
                xs={gridConfig.xs}
                sm={gridConfig.sm}
                md={gridConfig.md}
                lg={gridConfig.lg}
                xl={gridConfig.xl}
                key={style.id}
                style={{
                  paddingLeft: 0,
                  paddingRight: gridConfig.gutter / 2,
                  marginBottom: gridConfig.gutter
                }}
              >
                <Card
                  hoverable
                  style={{
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    borderRadius: 12,
                    border: style.is_default ? `2px solid ${token.colorPrimary}` : `1px solid ${token.colorBorderSecondary}`,
                  }}
                  styles={{
                    body: {
                      flex: 1,
                      display: 'flex',
                      flexDirection: 'column',
                      padding: '16px',
                    },
                  }}
                  actions={[
                    <span
                      key="default"
                      onClick={() => !style.is_default && handleSetDefault(style.id)}
                      style={{ cursor: style.is_default ? 'default' : 'pointer' }}
                    >
                      {style.is_default ? (
                        <StarFilled style={{ color: token.colorWarning, fontSize: 18 }} />
                      ) : (
                        <StarOutlined style={{ fontSize: 18 }} />
                      )}
                    </span>,
                    <EditOutlined
                      key="edit"
                      onClick={() => style.user_id !== null && handleEdit(style)}
                      style={{
                        fontSize: 18,
                        cursor: style.user_id === null ? 'not-allowed' : 'pointer',
                        color: style.user_id === null ? token.colorTextQuaternary : undefined
                      }}
                    />,
                    <Popconfirm
                      key="delete"
                      title={t('deleteConfirm.title')}
                      description={style.is_default ? t('deleteConfirm.defaultWarning') : undefined}
                      onConfirm={() => handleDelete(style.id)}
                      okText={t('deleteConfirm.ok')}
                      cancelText={t('deleteConfirm.cancel')}
                      disabled={style.user_id === null}
                    >
                      <DeleteOutlined
                        style={{
                          fontSize: 18,
                          color: style.user_id === null ? token.colorTextQuaternary : undefined,
                          cursor: style.user_id === null ? 'not-allowed' : 'pointer'
                        }}
                      />
                    </Popconfirm>,
                  ]}
                >
                  <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
                    <Space style={{ marginBottom: 12 }} wrap>
                      <Text strong style={{ fontSize: 16 }}>{style.name}</Text>
                      <Tag color={getStyleTypeColor(style.style_type)}>
                        {getStyleTypeLabel(style.style_type)}
                      </Tag>
                      {style.is_default && <Tag color="gold">{t('tag.default')}</Tag>}
                    </Space>
                    
                    {style.description && (
                      <Paragraph
                        type="secondary"
                        style={{ fontSize: 13, marginBottom: 12 }}
                        ellipsis={{ rows: 2, tooltip: style.description }}
                      >
                        {style.description}
                      </Paragraph>
                    )}
                    
                    <Paragraph
                      type="secondary"
                      style={{
                        fontSize: 12,
                        marginBottom: 0,
                        backgroundColor: token.colorFillAlter,
                        padding: 8,
                        borderRadius: 4,
                        flex: 1,
                        minHeight: 60,
                      }}
                      ellipsis={{ rows: 3, tooltip: style.prompt_content }}
                    >
                      {style.prompt_content}
                    </Paragraph>

                    {isProfileStyle && (
                      <div style={{ marginTop: 8 }}>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t('profileHint')}
                        </Text>

                        {parsedProfile && (
                          <Descriptions
                            column={1}
                            size="small"
                            colon={false}
                            style={{ marginTop: 8 }}
                            labelStyle={{ fontSize: 12, paddingBottom: 2 }}
                            contentStyle={{ fontSize: 12, paddingBottom: 2 }}
                          >
                            <Descriptions.Item label={t('profile.metrics.avgSentence')}>
                              {formatDecimal(profileMetrics.avg_sentence_chars)}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('profile.metrics.dialogueRatio')}>
                              {formatPercent(profileMetrics.dialogue_ratio)}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('profile.metrics.shortSentenceRatio')}>
                              {formatPercent(profileMetrics.short_sentence_ratio)}
                            </Descriptions.Item>
                            <Descriptions.Item label={t('profile.metrics.paragraphMedian')}>
                              {formatDecimal(profileMetrics.paragraph_chars_p50)}
                            </Descriptions.Item>
                            {style.description && (
                              <Descriptions.Item label={t('profile.sample')}>
                                <span style={{ wordBreak: 'break-word' }}>{style.description}</span>
                              </Descriptions.Item>
                            )}
                            {profileConfidence !== null && (
                              <Descriptions.Item label={t('profile.confidence')}>
                                {formatPercent(profileConfidence)}
                              </Descriptions.Item>
                            )}
                          </Descriptions>
                        )}

                        {currentProject && (
                          <Button
                            size="small"
                            icon={<ReloadOutlined />}
                            style={{ marginTop: 8 }}
                            onClick={handleOpenReextract}
                          >
                            {t('profile.reextract')}
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                </Card>
              </Col>
              );
            })}
          </Row>
        )}
      </div>

      {/* 创建自定义风格 Modal */}
      <Modal
        title={t('createModal.title')}
        open={isCreateModalOpen}
        onCancel={() => {
          setIsCreateModalOpen(false);
          createForm.resetFields();
        }}
        footer={null}
        centered
        width={isMobile ? 'calc(100vw - 32px)' : 600}
        style={isMobile ? { maxWidth: 'calc(100vw - 32px)', margin: '0 16px' } : undefined}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={handleCreate}
          style={{ marginTop: 16 }}
        >
          <Form.Item
            label={t('form.name')}
            name="name"
            rules={[{ required: true, message: t('form.nameRequired') }]}
          >
            <Input placeholder={t('form.namePlaceholderCreate')} />
          </Form.Item>
          
          <Form.Item label={t('form.description')} name="description">
            <TextArea rows={2} placeholder={t('form.descriptionPlaceholder')} />
          </Form.Item>
          
          <Form.Item
            label={t('form.promptContent')}
            name="prompt_content"
            rules={[{ required: true, message: t('form.promptRequired') }]}
          >
            <TextArea
              rows={6}
              placeholder={t('form.promptPlaceholderCreate')}
            />
          </Form.Item>
          
          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsCreateModalOpen(false);
                createForm.resetFields();
              }}>
                {t('buttons.cancel')}
              </Button>
              <Button type="primary" htmlType="submit" loading={loading}>
                {t('buttons.create')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑风格 Modal */}
      <Modal
        title={t('editModal.title')}
        open={isEditModalOpen}
        onCancel={() => {
          setIsEditModalOpen(false);
          editForm.resetFields();
          setEditingStyle(null);
        }}
        footer={null}
        centered
        width={isMobile ? 'calc(100vw - 32px)' : 600}
        style={isMobile ? { maxWidth: 'calc(100vw - 32px)', margin: '0 16px' } : undefined}
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdate} style={{ marginTop: 16 }}>
          <Form.Item
            label={t('form.name')}
            name="name"
            rules={[{ required: true, message: t('form.nameRequired') }]}
          >
            <Input placeholder={t('form.namePlaceholderEdit')} />
          </Form.Item>
          
          <Form.Item label={t('form.description')} name="description">
            <TextArea rows={2} placeholder={t('form.descriptionPlaceholder')} />
          </Form.Item>
          
          <Form.Item
            label={t('form.promptContent')}
            name="prompt_content"
            rules={[{ required: true, message: t('form.promptRequired') }]}
          >
            <TextArea 
              rows={6} 
              placeholder={t('form.promptPlaceholderEdit')}
            />
          </Form.Item>
          
          <Form.Item>
            <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
              <Button onClick={() => {
                setIsEditModalOpen(false);
                editForm.resetFields();
                setEditingStyle(null);
              }}>
                {t('buttons.cancel')}
              </Button>
              <Button type="primary" htmlType="submit" loading={loading}>
                {t('buttons.save')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 重新抽取画像 diff Modal（issue #254 P1） */}
      <Modal
        title={t('profile.diffTitle')}
        open={reextractOpen}
        onCancel={() => setReextractOpen(false)}
        centered
        width={isMobile ? 'calc(100vw - 32px)' : 900}
        style={isMobile ? { maxWidth: 'calc(100vw - 32px)', margin: '0 16px' } : undefined}
        footer={
          reextractResult?.skipped
            ? [
                <Button key="close" onClick={() => setReextractOpen(false)}>
                  {t('profile.cancel')}
                </Button>,
              ]
            : [
                <Button key="cancel" onClick={() => setReextractOpen(false)}>
                  {t('profile.cancel')}
                </Button>,
                <Button
                  key="apply"
                  type="primary"
                  loading={reextractApplying}
                  disabled={reextractLoading || !reextractResult}
                  onClick={handleApplyReextract}
                >
                  {t('profile.apply')}
                </Button>,
              ]
        }
      >
        {reextractLoading ? (
          <div style={{ textAlign: 'center', padding: '32px 0' }}>
            <Spin />
          </div>
        ) : reextractResult?.skipped ? (
          <Alert type="warning" showIcon message={t('profile.skipped')} />
        ) : reextractResult ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {reextractResult.degraded && (
              <Alert type="info" showIcon message={t('profile.degraded')} />
            )}
            {reextractResult.kept_reason === 'user_edited' && (
              <Alert type="warning" showIcon message={t('profile.keptByUser')} />
            )}

            <Space wrap size={8}>
              {reextractResult.description && (
                <Text type="secondary">{reextractResult.description}</Text>
              )}
              {typeof reextractResult.confidence === 'number' && (
                <Tag color="cyan">{`${t('profile.confidence')}: ${formatPercent(reextractResult.confidence)}`}</Tag>
              )}
            </Space>

            <Row gutter={[16, 16]}>
              <Col xs={24} md={12}>
                <Text strong>{t('profile.oldPrompt')}</Text>
                <Paragraph
                  style={{
                    whiteSpace: 'pre-wrap',
                    maxHeight: 320,
                    overflowY: 'auto',
                    backgroundColor: token.colorFillAlter,
                    padding: 8,
                    borderRadius: 4,
                    fontSize: 12,
                    marginTop: 4,
                    marginBottom: 0,
                  }}
                >
                  {reextractResult.old_prompt_content || '-'}
                </Paragraph>
              </Col>
              <Col xs={24} md={12}>
                <Text strong>{t('profile.newPrompt')}</Text>
                <Paragraph
                  style={{
                    whiteSpace: 'pre-wrap',
                    maxHeight: 320,
                    overflowY: 'auto',
                    backgroundColor: token.colorFillAlter,
                    padding: 8,
                    borderRadius: 4,
                    fontSize: 12,
                    marginTop: 4,
                    marginBottom: 0,
                  }}
                >
                  {reextractResult.new_prompt_content || '-'}
                </Paragraph>
              </Col>
            </Row>

            <Space direction="vertical" size={4}>
              <Checkbox
                checked={overwriteEdited}
                onChange={(event) => setOverwriteEdited(event.target.checked)}
              >
                {t('profile.overwriteEdited')}
              </Checkbox>
              <Checkbox
                checked={setDefault}
                onChange={(event) => setSetDefault(event.target.checked)}
              >
                {t('profile.setDefault')}
              </Checkbox>
            </Space>
          </Space>
        ) : null}
      </Modal>
    </div>
  );
}