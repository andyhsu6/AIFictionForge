import { Card, Descriptions, Empty, Typography, Button, Modal, Form, Input, message, Flex, InputNumber, Select, theme } from 'antd';
import { GlobalOutlined, EditOutlined, SyncOutlined, FormOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { useStore } from '../store';
import { worldSettingCardStyles } from '../components/CardStyles';
import { projectApi, wizardStreamApi } from '../services/api';
import { SSELoadingOverlay } from '../components/SSELoadingOverlay';
import { useTranslation } from 'react-i18next';

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

export default function WorldSetting() {
  const { t } = useTranslation('worldSetting');
  const { currentProject, setCurrentProject } = useStore();
  const [isEditModalVisible, setIsEditModalVisible] = useState(false);
  const [editForm] = Form.useForm();
  const [isSaving, setIsSaving] = useState(false);
  const [isEditProjectModalVisible, setIsEditProjectModalVisible] = useState(false);
  const [editProjectForm] = Form.useForm();
  const [isSavingProject, setIsSavingProject] = useState(false);
  const [isRegenerating, setIsRegenerating] = useState(false);
  const [regenerateProgress, setRegenerateProgress] = useState(0);
  const [regenerateMessage, setRegenerateMessage] = useState('');
  const [isPreviewModalVisible, setIsPreviewModalVisible] = useState(false);
  const [newWorldData, setNewWorldData] = useState<{
    time_period: string;
    location: string;
    atmosphere: string;
    rules: string;
  } | null>(null);
  const [isSavingPreview, setIsSavingPreview] = useState(false);
  const [modal, contextHolder] = Modal.useModal();
  const { token } = theme.useToken();

  // AI重新生成世界观
  const handleRegenerate = async () => {
    if (!currentProject) return;

    modal.confirm({
      title: t('regenerate.confirmTitle'),
      content: t('regenerate.confirmContent'),
      centered: true,
      okText: t('regenerate.confirmOk'),
      cancelText: t('regenerate.confirmCancel'),
      onOk: async () => {
        setIsRegenerating(true);
        setRegenerateProgress(0);
        setRegenerateMessage(t('regenerate.preparing'));

        try {
          await wizardStreamApi.regenerateWorldBuildingStream(
            currentProject.id,
            {},
            {
              onProgress: (msg: string, progress: number) => {
                setRegenerateProgress(progress);
                setRegenerateMessage(msg);
              },
              onChunk: (chunk: string) => {
                // 可以在这里显示生成的内容片段（可选）
                console.log('生成片段:', chunk);
              },
              onResult: (result: { time_period: string; location: string; atmosphere: string; rules: string }) => {
                // 保存新生成的数据
                const newData = {
                  time_period: result.time_period,
                  location: result.location,
                  atmosphere: result.atmosphere,
                  rules: result.rules,
                };
                setNewWorldData(newData);
              },
              onError: (errorMsg: string) => {
                console.error('重新生成失败:', errorMsg);
                message.error(errorMsg || t('regenerate.failed'));
              },
              onComplete: () => {
                setIsRegenerating(false);
                setRegenerateProgress(0);
                setRegenerateMessage('');
                // 显示预览对话框
                setIsPreviewModalVisible(true);
              }
            }
          );
        } catch (error) {
          console.error('重新生成出错:', error);
          message.error(t('regenerate.error'));
          setIsRegenerating(false);
          setRegenerateProgress(0);
          setRegenerateMessage('');
        }
      }
    });
  };

  // 确认保存重新生成的内容
  const handleConfirmSave = async () => {
    if (!currentProject || !newWorldData) return;

    setIsSavingPreview(true);
    try {
      const updatedProject = await projectApi.updateProject(currentProject.id, {
        world_time_period: newWorldData.time_period,
        world_location: newWorldData.location,
        world_atmosphere: newWorldData.atmosphere,
        world_rules: newWorldData.rules,
      });

      setCurrentProject(updatedProject);
      message.success(t('regenerate.updated'));
      setIsPreviewModalVisible(false);
      setNewWorldData(null);
    } catch (error) {
      console.error('保存失败:', error);
      message.error(t('regenerate.saveFailed'));
    } finally {
      setIsSavingPreview(false);
    }
  };

  // 取消保存，关闭预览
  const handleCancelSave = () => {
    setIsPreviewModalVisible(false);
    setNewWorldData(null);
    message.info(t('regenerate.cancelled'));
  };

  if (!currentProject) return null;

  // 检查是否有世界设定信息
  const hasWorldSetting = currentProject.world_time_period ||
    currentProject.world_location ||
    currentProject.world_atmosphere ||
    currentProject.world_rules;

  if (!hasWorldSetting) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        {/* 固定头部 */}
        <div style={{
          position: 'sticky',
          top: 0,
          zIndex: 10,
          backgroundColor: token.colorBgContainer,
          padding: '16px 0',
          marginBottom: 16,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          display: 'flex',
          alignItems: 'center'
        }}>
          <GlobalOutlined style={{ fontSize: 24, marginRight: 12, color: token.colorPrimary }} />
          <h2 style={{ margin: 0 }}>{t('page.title')}</h2>
        </div>

        {/* 可滚动内容区域 */}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <Empty
            description={t('empty.description')}
            style={{ marginTop: 60 }}
          >
            <Paragraph type="secondary">
              {t('empty.hint')}
            </Paragraph>
          </Empty>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {contextHolder}
      {/* 固定头部 */}
      <div style={{
        position: 'sticky',
        top: 0,
        zIndex: 10,
        backgroundColor: token.colorBgContainer,
        padding: '16px 0',
        marginBottom: 24,
        borderBottom: `1px solid ${token.colorBorderSecondary}`
      }}>
        <Flex
          justify="space-between"
          align="flex-start"
          gap={12}
          wrap="wrap"
        >
          <div style={{ display: 'flex', alignItems: 'center', minWidth: 'fit-content' }}>
            <GlobalOutlined style={{ fontSize: 24, marginRight: 12, color: token.colorPrimary }} />
            <h2 style={{ margin: 0, whiteSpace: 'nowrap' }}>{t('page.title')}</h2>
          </div>
          <Flex gap={8} wrap="wrap" style={{ flex: '0 1 auto' }}>
            <Button
              icon={<SyncOutlined />}
              onClick={handleRegenerate}
              disabled={isRegenerating}
              style={{
                minWidth: 'fit-content',
                flex: '1 1 auto'
              }}
            >
              <span className="button-text-mobile">{t('page.aiRegenerate')}</span>
            </Button>
            <Button
              type="primary"
              icon={<FormOutlined />}
              onClick={() => {
                editProjectForm.setFieldsValue({
                  title: currentProject.title || '',
                  description: currentProject.description || '',
                  theme: currentProject.theme || '',
                  genre: currentProject.genre || '',
                  narrative_perspective: currentProject.narrative_perspective || '',
                  target_words: currentProject.target_words || 0,
                });
                setIsEditProjectModalVisible(true);
              }}
              style={{
                minWidth: 'fit-content',
                flex: '1 1 auto'
              }}
            >
              <span className="button-text-mobile">{t('page.editBasics')}</span>
            </Button>
            <Button
              type="primary"
              icon={<EditOutlined />}
              onClick={() => {
                editForm.setFieldsValue({
                  world_time_period: currentProject.world_time_period || '',
                  world_location: currentProject.world_location || '',
                  world_atmosphere: currentProject.world_atmosphere || '',
                  world_rules: currentProject.world_rules || '',
                });
                setIsEditModalVisible(true);
              }}
              style={{
                minWidth: 'fit-content',
                flex: '1 1 auto'
              }}
            >
              <span className="button-text-mobile">{t('page.editWorld')}</span>
            </Button>
          </Flex>
        </Flex>
      </div>

      {/* 可滚动内容区域 */}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        <Card
          style={{
            ...worldSettingCardStyles.sectionCard,
            marginBottom: 16
          }}
          title={
            <span style={{ fontSize: 18, fontWeight: 500 }}>
              {t('basics.title')}
            </span>
          }
        >
          <Descriptions bordered column={1} styles={{ label: { width: 120, fontWeight: 500 } }}>
            <Descriptions.Item label={t('basics.novelName')}>{currentProject.title}</Descriptions.Item>
            {currentProject.description && (
              <Descriptions.Item label={t('basics.novelDesc')}>{currentProject.description}</Descriptions.Item>
            )}
            <Descriptions.Item label={t('basics.novelTheme')}>{currentProject.theme || t('basics.notSet')}</Descriptions.Item>
            <Descriptions.Item label={t('basics.novelGenre')}>{currentProject.genre || t('basics.notSet')}</Descriptions.Item>
            <Descriptions.Item label={t('basics.perspective')}>{currentProject.narrative_perspective || t('basics.notSet')}</Descriptions.Item>
            <Descriptions.Item label={t('basics.targetWords')}>
              {currentProject.target_words ? t('basics.wordsValue', { n: currentProject.target_words.toLocaleString() }) : t('basics.notSet')}
            </Descriptions.Item>
          </Descriptions>
        </Card>

        <Card
          style={{
            ...worldSettingCardStyles.sectionCard,
            marginBottom: 16
          }}
          title={
            <span style={{ fontSize: 18, fontWeight: 500 }}>
              <GlobalOutlined style={{ marginRight: 8 }} />
              {t('world.title')}
            </span>
          }
        >
          <div style={{ padding: '16px 0' }}>
            {currentProject.world_time_period && (
              <div style={{ marginBottom: 24 }}>
                <Title level={5} style={{ color: token.colorPrimary, marginBottom: 12 }}>
                  {t('world.timePeriod')}
                </Title>
                <Paragraph style={{
                  fontSize: 15,
                  lineHeight: 1.8,
                  padding: 16,
                  background: token.colorBgLayout,
                  borderRadius: 8,
                  borderLeft: `4px solid ${token.colorPrimary}`
                }}>
                  {currentProject.world_time_period}
                </Paragraph>
              </div>
            )}

            {currentProject.world_location && (
              <div style={{ marginBottom: 24 }}>
                <Title level={5} style={{ color: token.colorSuccess, marginBottom: 12 }}>
                  {t('world.location')}
                </Title>
                <Paragraph style={{
                  fontSize: 15,
                  lineHeight: 1.8,
                  padding: 16,
                  background: token.colorBgLayout,
                  borderRadius: 8,
                  borderLeft: `4px solid ${token.colorSuccess}`
                }}>
                  {currentProject.world_location}
                </Paragraph>
              </div>
            )}

            {currentProject.world_atmosphere && (
              <div style={{ marginBottom: 24 }}>
                <Title level={5} style={{ color: token.colorWarning, marginBottom: 12 }}>
                  {t('world.atmosphere')}
                </Title>
                <Paragraph style={{
                  fontSize: 15,
                  lineHeight: 1.8,
                  padding: 16,
                  background: token.colorBgLayout,
                  borderRadius: 8,
                  borderLeft: `4px solid ${token.colorWarning}`
                }}>
                  {currentProject.world_atmosphere}
                </Paragraph>
              </div>
            )}

            {currentProject.world_rules && (
              <div style={{ marginBottom: 0 }}>
                <Title level={5} style={{ color: token.colorError, marginBottom: 12 }}>
                  {t('world.rules')}
                </Title>
                <Paragraph style={{
                  fontSize: 15,
                  lineHeight: 1.8,
                  padding: 16,
                  background: token.colorBgLayout,
                  borderRadius: 8,
                  borderLeft: `4px solid ${token.colorError}`
                }}>
                  {currentProject.world_rules}
                </Paragraph>
              </div>
            )}
          </div>
        </Card>
      </div>

      {/* 编辑世界观模态框 */}
      <Modal
        title={t('editWorld.title')}
        open={isEditModalVisible}
        centered
        onCancel={() => {
          setIsEditModalVisible(false);
          editForm.resetFields();
        }}
        onOk={async () => {
          try {
            const values = await editForm.validateFields();
            setIsSaving(true);

            const updatedProject = await projectApi.updateProject(currentProject.id, {
              world_time_period: values.world_time_period,
              world_location: values.world_location,
              world_atmosphere: values.world_atmosphere,
              world_rules: values.world_rules,
            });

            setCurrentProject(updatedProject);
            message.success(t('editWorld.updateSuccess'));
            setIsEditModalVisible(false);
            editForm.resetFields();
          } catch (error) {
            console.error('更新世界观失败:', error);
            message.error(t('editWorld.updateFailed'));
          } finally {
            setIsSaving(false);
          }
        }}
        confirmLoading={isSaving}
        width={800}
        okText={t('buttons.save')}
        cancelText={t('buttons.cancel')}
      >
        <Form
          form={editForm}
          layout="vertical"
          style={{ marginTop: 16 }}
        >
          <Form.Item
            label={t('world.timePeriod')}
            name="world_time_period"
            rules={[{ required: true, message: t('editWorld.timeRequired') }]}
          >
            <TextArea
              rows={4}
              placeholder={t('editWorld.timePlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>

          <Form.Item
            label={t('world.location')}
            name="world_location"
            rules={[{ required: true, message: t('editWorld.locationRequired') }]}
          >
            <TextArea
              rows={4}
              placeholder={t('editWorld.locationPlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>

          <Form.Item
            label={t('world.atmosphere')}
            name="world_atmosphere"
            rules={[{ required: true, message: t('editWorld.atmosphereRequired') }]}
          >
            <TextArea
              rows={4}
              placeholder={t('editWorld.atmospherePlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>

          <Form.Item
            label={t('world.rules')}
            name="world_rules"
            rules={[{ required: true, message: t('editWorld.rulesRequired') }]}
          >
            <TextArea
              rows={4}
              placeholder={t('editWorld.rulesPlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑项目基础信息模态框 */}
      <Modal
        title={t('editBasics.title')}
        open={isEditProjectModalVisible}
        centered
        onCancel={() => {
          setIsEditProjectModalVisible(false);
          editProjectForm.resetFields();
        }}
        onOk={async () => {
          try {
            const values = await editProjectForm.validateFields();
            setIsSavingProject(true);

            const updatedProject = await projectApi.updateProject(currentProject.id, {
              title: values.title,
              description: values.description,
              theme: values.theme,
              genre: values.genre,
              narrative_perspective: values.narrative_perspective,
              target_words: values.target_words,
            });

            setCurrentProject(updatedProject);
            message.success(t('editBasics.updateSuccess'));
            setIsEditProjectModalVisible(false);
            editProjectForm.resetFields();
          } catch (error) {
            console.error('更新项目基础信息失败:', error);
            message.error(t('editBasics.updateFailed'));
          } finally {
            setIsSavingProject(false);
          }
        }}
        confirmLoading={isSavingProject}
        width={800}
        okText={t('buttons.save')}
        cancelText={t('buttons.cancel')}
      >
        <Form
          form={editProjectForm}
          layout="vertical"
          style={{ marginTop: 16 }}
        >
          <Form.Item
            label={t('basics.novelName')}
            name="title"
            rules={[
              { required: true, message: t('editBasics.nameRequired') },
              { max: 200, message: t('editBasics.nameMax') }
            ]}
          >
            <Input
              placeholder={t('editBasics.namePlaceholder')}
              showCount
              maxLength={200}
            />
          </Form.Item>

          <Form.Item
            label={t('basics.novelDesc')}
            name="description"
            rules={[
              { max: 1000, message: t('editBasics.descMax') }
            ]}
          >
            <TextArea
              rows={4}
              placeholder={t('editBasics.descPlaceholder')}
              showCount
              maxLength={1000}
            />
          </Form.Item>

          <Form.Item
            label={t('basics.novelTheme')}
            name="theme"
            rules={[
              { max: 500, message: t('editBasics.themeMax') }
            ]}
          >
            <TextArea
              rows={3}
              placeholder={t('editBasics.themePlaceholder')}
              showCount
              maxLength={500}
            />
          </Form.Item>

          <Form.Item
            label={t('basics.novelGenre')}
            name="genre"
            rules={[
              { max: 100, message: t('editBasics.genreMax') }
            ]}
          >
            <Input
              placeholder={t('editBasics.genrePlaceholder')}
              showCount
              maxLength={100}
            />
          </Form.Item>

          <Form.Item
            label={t('basics.perspective')}
            name="narrative_perspective"
          >
            <Select
              placeholder={t('editBasics.perspectivePlaceholder')}
              allowClear
              options={[
                { label: t('perspective.firstPerson'), value: '第一人称' },
                { label: t('perspective.thirdPerson'), value: '第三人称' },
                { label: t('perspective.omniscient'), value: '全知视角' }
              ]}
            />
          </Form.Item>

          <Form.Item
            label={t('basics.targetWords')}
            name="target_words"
            rules={[
              { type: 'number', min: 0, message: t('editBasics.wordsNegative') },
              { type: 'number', max: 2147483647, message: t('editBasics.wordsOutOfRange') }
            ]}
          >
            <InputNumber
              style={{ width: '100%' }}
              placeholder={t('editBasics.wordsPlaceholder')}
              min={0}
              max={2147483647}
              step={1000}
              addonAfter={t('editBasics.wordsUnit')}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* AI重新生成加载遮罩 */}
      <SSELoadingOverlay
        loading={isRegenerating}
        progress={regenerateProgress}
        message={regenerateMessage}
      />

      {/* 预览重新生成的内容模态框 */}
      <Modal
        title={t('preview.title')}
        open={isPreviewModalVisible}
        centered
        width={900}
        onOk={handleConfirmSave}
        onCancel={handleCancelSave}
        confirmLoading={isSavingPreview}
        okText={t('preview.okReplace')}
        cancelText={t('preview.cancel')}
        okButtonProps={{ danger: true }}
      >
        {newWorldData && (
          <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
            <div style={{ marginBottom: 24, padding: 16, background: token.colorWarningBg, border: `1px solid ${token.colorWarningBorder}`, borderRadius: 8 }}>
              <Typography.Text type="warning" strong>
                {t('preview.warning')}
              </Typography.Text>
            </div>

            <div style={{ marginBottom: 24 }}>
              <Title level={5} style={{ color: token.colorPrimary, marginBottom: 12 }}>
                {t('world.timePeriod')}
              </Title>
              <Paragraph style={{
                fontSize: 15,
                lineHeight: 1.8,
                padding: 16,
                background: token.colorBgLayout,
                borderRadius: 8,
                borderLeft: `4px solid ${token.colorPrimary}`
              }}>
                {newWorldData.time_period}
              </Paragraph>
            </div>

            <div style={{ marginBottom: 24 }}>
              <Title level={5} style={{ color: token.colorSuccess, marginBottom: 12 }}>
                {t('world.location')}
              </Title>
              <Paragraph style={{
                fontSize: 15,
                lineHeight: 1.8,
                padding: 16,
                background: token.colorBgLayout,
                borderRadius: 8,
                borderLeft: `4px solid ${token.colorSuccess}`
              }}>
                {newWorldData.location}
              </Paragraph>
            </div>

            <div style={{ marginBottom: 24 }}>
              <Title level={5} style={{ color: token.colorWarning, marginBottom: 12 }}>
                {t('world.atmosphere')}
              </Title>
              <Paragraph style={{
                fontSize: 15,
                lineHeight: 1.8,
                padding: 16,
                background: token.colorBgLayout,
                borderRadius: 8,
                borderLeft: `4px solid ${token.colorWarning}`
              }}>
                {newWorldData.atmosphere}
              </Paragraph>
            </div>

            <div style={{ marginBottom: 0 }}>
              <Title level={5} style={{ color: token.colorError, marginBottom: 12 }}>
                {t('world.rules')}
              </Title>
              <Paragraph style={{
                fontSize: 15,
                lineHeight: 1.8,
                padding: 16,
                background: token.colorBgLayout,
                borderRadius: 8,
                borderLeft: `4px solid ${token.colorError}`
              }}>
                {newWorldData.rules}
              </Paragraph>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}